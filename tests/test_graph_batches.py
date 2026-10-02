import threading
import time
import sqlite3
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.orchestration.batch_graph import BatchRuntime, build_batch_graph, merge_results
from app.orchestration.summary_graph import SummaryRuntime, build_summary_graph
from app.orchestration.vote_graph import build_vote_graph
from app.orchestration.policies import policy_for
from app.repositories.orchestration import RepositoryConflict


def _input(batch_id="batch", ids=("a", "b")):
    return {"batch_id": batch_id, "jobs": [{"agent_id": value, "agent_name": value} for value in ids],
            "results_by_agent": {}}


def test_parallel_batch_waits_for_all_and_orders_by_agents():
    second_done = threading.Event()
    completed = []

    def call(job):
        if job["agent_id"] == "a":
            assert second_done.wait(2), "Send did not run the second participant concurrently"
        else:
            second_done.set()
        return {"score": 10 if job["agent_id"] == "a" else 20}

    def record(job, result):
        if job["agent_id"] == "b":
            completed.append("b")
        else:
            # Make arrival order deterministic independently of thread wakeup.
            deadline = time.monotonic() + 2
            while not completed and time.monotonic() < deadline:
                time.sleep(0.001)
            completed.append("a")

    result = build_batch_graph().invoke(
        _input(), context=BatchRuntime(call, record), config={"max_concurrency": 2},
    )
    assert completed == ["b", "a"]
    assert result["status"] == "completed"
    assert [item["agent_id"] for item in result["ordered_results"]] == ["a", "b"]
    assert [item["score"] for item in result["ordered_results"]] == [10, 20]


def test_new_batch_overwrites_reducer_state_on_same_thread():
    graph = build_batch_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "same-conversation"}, "max_concurrency": 1}
    calls = []
    runtime = BatchRuntime(lambda job: calls.append(job["agent_id"]) or {"score": 42})
    graph.invoke(_input("old"), context=runtime, config=config)
    result = graph.invoke(_input("new", ("c",)), context=runtime, config=config)
    assert calls == ["a", "b", "c"]
    assert set(result["results_by_agent"]) == {"c"}
    assert result["ordered_results"][0]["batch_id"] == "new"


def test_completed_ticket_is_reused_and_stale_ticket_is_not():
    value = _input()
    value["results_by_agent"] = {
        "a": {"agent_id": "a", "batch_id": "batch", "score": 11},
        "b": {"agent_id": "b", "batch_id": "previous", "score": 999},
    }
    calls = []
    result = build_batch_graph().invoke(value, context=BatchRuntime(
        lambda job: calls.append(job["agent_id"]) or {"score": 22},
    ))
    assert calls == ["b"]
    assert [item["score"] for item in result["ordered_results"]] == [11, 22]


def test_partial_call_failure_keeps_success_and_known_failure_usage():
    seen = []

    def call(job):
        if job["agent_id"] == "b":
            error = RuntimeError("temporary failure")
            error.usage = {"completion_tokens": 3}
            raise error
        return {"score": 77, "usage": {"completion_tokens": 2}}

    result = build_batch_graph().invoke(
        _input(), context=BatchRuntime(call, lambda job, value: seen.append(value)),
        config={"max_concurrency": 2},
    )
    assert result["status"] == "error"
    assert len(seen) == 2
    assert result["results_by_agent"]["a"]["score"] == 77
    assert result["results_by_agent"]["b"]["usage"]["completion_tokens"] == 3


def test_explicit_batch_retry_reuses_success_and_retries_only_failed_participant():
    graph = build_batch_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "retry-batch"}, "max_concurrency": 1}
    counts = {"a": 0, "b": 0}

    def call(job):
        agent_id = job["agent_id"]
        counts[agent_id] += 1
        if agent_id == "b" and counts[agent_id] == 1:
            raise RuntimeError("temporary failure")
        return {"score": 50}

    first = graph.invoke(_input(), context=BatchRuntime(call), config=config)
    assert first["status"] == "error"
    second = graph.invoke(_input(), context=BatchRuntime(call), config=config)
    assert second["status"] == "completed"
    assert counts == {"a": 1, "b": 2}


@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, RepositoryConflict])
def test_critical_repository_failure_stops_serial_batch_without_calling_next_agent(error_type):
    calls, records = [], []

    def fail(job):
        calls.append(job["agent_id"])
        raise error_type("durable result could not be saved")

    with pytest.raises(error_type, match="could not be saved"):
        build_batch_graph().invoke(
            _input(), context=BatchRuntime(fail, lambda job, result: records.append(result)),
            config={"max_concurrency": 1},
        )
    assert calls == ["a"]
    assert records == []


def test_runtime_fatal_marker_stops_dispatch_without_circular_runtime_import():
    class PersistenceFailure(RuntimeError):
        _orchestration_fatal = True

    calls = []

    def fail(job):
        calls.append(job["agent_id"])
        raise PersistenceFailure("attempt is unsafe")

    with pytest.raises(PersistenceFailure):
        build_batch_graph().invoke(_input(), context=BatchRuntime(fail), config={"max_concurrency": 1})
    assert calls == ["a"]


def test_checkpoint_resume_does_not_repeat_successful_sibling():
    counts = {"a": 0, "b": 0}
    reject = {"once": True}

    def call(job):
        counts[job["agent_id"]] += 1
        return {"score": 50}

    def record(job, result):
        if job["agent_id"] == "b" and reject["once"]:
            reject["once"] = False
            raise OSError("business database unavailable")

    graph = build_batch_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "recovery"}, "max_concurrency": 1}
    runtime = BatchRuntime(call, record)
    with pytest.raises(OSError, match="database unavailable"):
        graph.invoke(_input(), context=runtime, config=config)
    result = graph.invoke(None, context=BatchRuntime(call, record), config=config)
    assert result["status"] == "completed"
    assert counts == {"a": 1, "b": 2}


def test_result_persistence_failure_stops_queued_requests():
    calls = []

    def call(job):
        calls.append(job["agent_id"])
        return {"score": 50}

    def fail_record(job, result):
        raise OSError("result ticket write failed")

    with pytest.raises(OSError, match="ticket write failed"):
        build_batch_graph().invoke(
            _input(), context=BatchRuntime(call, fail_record), config={"max_concurrency": 1},
        )
    assert calls == ["a"]


def test_shared_limiter_bounds_calls_across_graphs():
    limiter = threading.BoundedSemaphore(1)
    lock = threading.Lock()
    active = maximum = 0

    def call(job):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.005)
        with lock:
            active -= 1
        return {"score": 1}

    def run(batch_id):
        return build_batch_graph().invoke(
            _input(batch_id), context=BatchRuntime(call, limiter=limiter),
            config={"max_concurrency": 2},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ("one", "two")))
    assert maximum == 1
    assert all(result["status"] == "completed" for result in results)


def test_duplicate_jobs_and_conflicting_reducer_results_are_rejected():
    with pytest.raises(ValueError, match="重复角色"):
        build_batch_graph().invoke(_input(ids=("a", "a")), context=BatchRuntime(lambda job: {}))
    item = {"batch_id": "batch", "score": 20}
    assert merge_results({"a": item}, {"a": dict(item)}) == {"a": item}
    with pytest.raises(ValueError, match="结果冲突"):
        merge_results({"a": item}, {"a": {"batch_id": "batch", "score": 21}})


@pytest.mark.parametrize("choices,agreed", [(["1"], True), ([], False), (["2"], False)])
def test_end_vote_requires_explicit_agreement_from_every_participant(choices, agreed):
    state = {**_input(), "kind": "end", "options": ["同意结束", "继续讨论"]}
    result = build_vote_graph().invoke(state, context=BatchRuntime(
        lambda job: {"choices": ["1"] if job["agent_id"] == "a" else choices,
                     "reason": "reason", "usage": {"completion_tokens": 1}},
    ))
    assert result["agreed"] is agreed
    assert [ballot["agent_id"] for ballot in result["ballots"]] == ["a", "b"]


def test_ordinary_vote_counts_repeated_choices_and_keeps_partial_ballots_on_error():
    def call(job):
        if job["agent_id"] == "b":
            raise RuntimeError("vote unavailable")
        return {"choices": ["2", "2"], "reason": "two votes"}

    result = build_vote_graph().invoke(
        {**_input(), "kind": "normal", "options": ["first", "second"]},
        context=BatchRuntime(call),
    )
    assert result["status"] == "error"
    assert result["results"] == {"1": 0, "2": 2}
    assert len(result["ballots"]) == 1
    assert result["agreed"] is False


def test_summary_failure_still_commits_completed_result():
    committed = []
    observed = []

    def fail(log):
        assert log == "公开发言"
        raise RuntimeError("provider unavailable")

    result = build_summary_graph().invoke(
        {"operation_id": "summary-op"},
        context=SummaryRuntime(lambda: "公开发言", fail, committed.append, observed.append),
    )
    assert result["status"] == "completed"
    assert result["summary"] == "总结失败：provider unavailable"
    assert committed[0]["operation_id"] == "summary-op"
    assert observed[0]["summary"] == committed[0]["summary"]


def test_empty_summary_does_not_call_model():
    def unexpected(log):
        pytest.fail("empty conversation must not make a model call")

    result = build_summary_graph().invoke(
        {"operation_id": "empty"},
        context=SummaryRuntime(lambda: "  ", unexpected, lambda value: None),
    )
    assert result["summary"] == "本次对话没有任何发言。"


def test_summary_database_failure_is_not_committed_as_model_failure():
    committed = []

    def fail(log):
        raise sqlite3.OperationalError("database unavailable")

    with pytest.raises(sqlite3.OperationalError):
        build_summary_graph().invoke(
            {"operation_id": "broken-db"},
            context=SummaryRuntime(lambda: "public log", fail, committed.append),
        )
    assert committed == []


def test_forced_selection_does_not_consume_round_robin_cursor():
    runner = SimpleNamespace(
        agents=[{}, {}, {}], _forced_next_idx=2, _rr_index=4,
        order=[1, 2, 0], scheduling_mode="round_robin",
    )
    policy = policy_for(runner)
    selection = policy.select(runner)
    assert policy.needs_scores(runner) is False
    assert selection.agent_idx == 2 and selection.rr_index == 4
    assert selection.forced_next_idx is None
    # The decision itself leaves live business state untouched until committed.
    assert runner._rr_index == 4 and runner._forced_next_idx == 2


def test_two_participants_force_alternation_in_willingness_mode():
    runner = SimpleNamespace(
        agents=[{}, {}], _forced_next_idx=None, _rr_index=0,
        order=[1, 0], scheduling_mode="willingness",
    )
    policy = policy_for(runner)
    assert policy.needs_scores(runner) is False
    first = policy.select(runner)
    runner._rr_index = first.rr_index
    second = policy.select(runner)
    assert (first.agent_idx, second.agent_idx) == (1, 0)


def test_human_turn_before_first_agent_requires_willingness_scores():
    runner = SimpleNamespace(
        agents=[{}, {}, {}], _forced_next_idx=None, _rr_index=0,
        order=[0, 1, 2], scheduling_mode="willingness", turn=1,
    )
    assert policy_for(runner).needs_scores(runner) is True
