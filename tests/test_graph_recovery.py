"""Crash windows use separate temporary business and checkpoint files."""
from types import SimpleNamespace
import threading

import pytest

from app import db
from app.llm import LLMClient
from app.orchestration.checkpointer import close_savers, get_saver
from app.orchestration.runtime import GraphRunner, OrchestrationPersistenceError
from app.repositories.orchestration import OrchestrationRepository


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    business = tmp_path / "business.db"
    checkpoint = tmp_path / "checkpoints.db"
    monkeypatch.setattr(db, "DB_PATH", business)
    repository = OrchestrationRepository(business)
    saver = get_saver(checkpoint)
    llm = LLMClient(mock=True)
    calls = []

    def turn(name, system, history, limit):
        calls.append(name)
        return {"speech": "已保存的发言", "propose_end": False,
                "whiteboard_ops": [{"op": "append", "content": "只追加一次"}]}, {
                    "prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8,
                }

    llm.agent_turn = turn
    runner = GraphRunner("recovery", "config", "恢复测试", {
        "agent_backend": "direct", "agents": [{"id": "a0", "name": "甲"}, {"id": "a1", "name": "乙"}],
        "first_speaker": "a0", "total_max_tokens": 100, "whiteboard_enabled": True,
        "whiteboard_editors": ["a0", "a1"],
    }, llm, repository=repository, saver=saver)
    db.create_conversation(runner.id, runner.config_id, runner.name, runner.to_dict())
    runner._claim()
    yield runner, repository, saver, llm, calls
    close_savers(checkpoint)


def restore(runner, repository, saver, llm, *, lose_checkpoint=False):
    repository.recover()
    if lose_checkpoint:
        saver.delete_thread("conversation:" + runner.id)
    recovered = GraphRunner.from_payload(repository.get_conversation(runner.id), llm,
                                         repository=repository, saver=saver)
    recovered._claim()
    recovered.status = "running"
    original = recovered.nodes.return_continue

    def finish_one(state):
        outcome = original(state)
        recovered._interrupt_requested = True
        return outcome

    recovered.nodes.return_continue = finish_one
    return recovered


@pytest.mark.parametrize("lose_checkpoint", [False, True])
def test_result_receipt_survives_failed_public_commit_without_another_model_call(conversation, monkeypatch,
                                                                              lose_checkpoint):
    runner, repository, saver, llm, calls = conversation
    original = repository.commit
    failed = []

    def commit(operation, *args, **kwargs):
        if operation.endswith(":turn") and not failed:
            failed.append(True)
            raise RuntimeError("模拟公开结果提交前中断")
        return original(operation, *args, **kwargs)

    monkeypatch.setattr(repository, "commit", commit)
    runner._run()
    assert calls == ["甲"] and runner.messages == []
    recovered = restore(runner, repository, saver, llm, lose_checkpoint=lose_checkpoint)
    recovered._run()
    assert calls == ["甲"]
    assert len(recovered.messages) == recovered.turn == 1
    assert recovered.whiteboard_content == "只追加一次" and recovered.whiteboard_rev == 1
    assert recovered.total_output_tokens == 5 and recovered.total_prompt_tokens == 3
    assert recovered.status == "paused"


@pytest.mark.parametrize("lose_checkpoint", [False, True])
def test_committed_turn_is_not_republished_when_root_was_not_finished(conversation, lose_checkpoint):
    runner, repository, saver, llm, calls = conversation

    def crash(_state):
        raise RuntimeError("模拟消息提交后、根操作完成前中断")

    runner.nodes.return_continue = crash
    runner._run()
    assert len(runner.messages) == 1
    recovered = restore(runner, repository, saver, llm, lose_checkpoint=lose_checkpoint)
    recovered._run()
    assert calls == ["甲"] and len(recovered.messages) == 1
    assert recovered.whiteboard_content == "只追加一次" and recovered.whiteboard_rev == 1
    assert recovered.total_output_tokens == 5


def test_attempt_receipt_is_reused_when_operation_receipt_was_not_saved(conversation, monkeypatch):
    runner, repository, saver, llm, calls = conversation
    original = repository.update_operation
    failed = []

    def update(operation, **fields):
        if operation.endswith(":turn") and fields.get("status") == "result_ready" and not failed:
            failed.append(True)
            raise RuntimeError("模拟 attempt 已保存、operation 未保存时中断")
        return original(operation, **fields)

    monkeypatch.setattr(repository, "update_operation", update)
    runner._run()
    assert calls == ["甲"] and not runner.messages
    recovered = restore(runner, repository, saver, llm, lose_checkpoint=True)
    recovered._run()
    assert calls == ["甲"] and len(recovered.messages) == 1
    assert recovered.total_output_tokens == 5


def test_uncertain_external_attempt_with_lost_checkpoint_is_not_replayed(conversation):
    runner, repository, saver, llm, calls = conversation
    root = runner._prepare_operation()
    state = {"operation_id": root["operation_id"]}
    state.update(runner.nodes.load_operation(state))
    state.update(runner.nodes.select_speaker(state))
    turn = state["turn_result_ref"]
    repository.create_attempt("inflight", turn, runner.runner_epoch)
    repository.update_operation(turn, status="running")
    recovered = restore(runner, repository, saver, llm, lose_checkpoint=True)
    recovered._run()
    assert not calls and not recovered.messages
    assert recovered.status == "paused" and recovered.paused_reason == "error"
    assert "没有保存确定结果" in recovered.error


def test_changed_graph_version_does_not_replay_pending_operation(conversation):
    runner, repository, saver, llm, calls = conversation
    root = runner._prepare_operation()
    repository.update_operation(root["operation_id"], input={**root["input"], "graph_version": "future-v2"})
    runner._run()
    assert not calls and not runner.messages
    assert runner.status == "paused" and "图版本不兼容" in runner.error


def test_restart_interrupts_end_vote_and_finishes_original_cooldown(conversation):
    runner, repository, saver, llm, calls = conversation
    runner.end_vote_enabled = True
    runner.end_vote_proposers = ["a0"]
    original = llm.agent_turn

    def proposing(*args):
        result, usage = original(*args)
        result["propose_end"] = True
        return result, usage

    llm.agent_turn = proposing
    runner._persist()
    runner.nodes.end_vote = lambda _: (_ for _ in ()).throw(RuntimeError("投票调用前进程中断"))
    runner._run()
    assert runner.votes and runner.votes[0]["status"] == "running"
    llm.vote = lambda *args: pytest.fail("重启不能自动补投")
    recovered = restore(runner, repository, saver, llm)
    recovered._run()
    assert calls == ["甲"] and len(recovered.messages) == 1
    assert recovered.votes[0]["status"] == "error"
    assert recovered._end_vote_block_until_turn == 1 + runner.end_vote_cooldown_turns


def test_failed_usage_callback_cannot_publish_even_if_executor_returns_late_result(conversation, monkeypatch):
    runner, repository, saver, llm, calls = conversation
    runner.harness = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(repository, "record_usage", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("模拟用量库不可写")))

    def execute(*args, on_usage, **kwargs):
        try:
            on_usage({"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8,
                      "_event_id": "callback"})
        except Exception:
            pass  # A misbehaving SDK must not defeat the local publication fence.
        return {"speech": "迟到发言", "whiteboard_ops": [{"op": "append", "content": "不应出现"}]}, {
            "prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8,
        }, {"pending": False}

    runner.executor.execute = execute
    runner._run()
    assert not runner.messages and runner.whiteboard_content == ""
    assert runner.status == "paused" and runner.paused_reason == "error"
    assert any(op["status"] == "uncertain" for op in repository.list_operations(runner.id, kind="turn"))


def test_old_epoch_known_usage_is_settled_but_result_is_not_accepted(conversation):
    runner, repository, saver, llm, calls = conversation

    def call(_attempt):
        repository.claim(runner.id)
        return {"speech": "旧代次结果", "usage": {"prompt_tokens": 3, "completion_tokens": 5}}

    with pytest.raises(OrchestrationPersistenceError):
        runner._external("late", "turn", {}, call)
    saved = repository.get_conversation(runner.id)
    assert saved["total_output_tokens"] == 5 and not saved["messages"]
    assert repository.get_operation("late").get("result") is None


def test_resume_retries_only_failed_score_and_keeps_successful_usage(conversation):
    runner, repository, saver, llm, calls = conversation
    runner.agents.append({"id": "a2", "name": "丙", "system_prompt": "", "visibility": [],
                          "max_tokens": runner.single_max_tokens})
    runner.heat.append(0)
    runner.order = [0, 1, 2]
    runner.scheduling_mode = "willingness"
    runner.status = "paused"
    runner.human_say("开始评分的公开历史")
    runner.status = "running"
    runner._persist()
    scores = []
    failed = []

    def score(name, *_args):
        scores.append(name)
        if name == "乙" and not failed:
            failed.append(True)
            raise RuntimeError("一次评分失败")
        return 50, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    llm.willingness_score = score
    runner._run()
    assert not calls and runner.total_output_tokens == 2
    recovered = restore(runner, repository, saver, llm)
    recovered._run()
    assert scores.count("甲") == scores.count("丙") == 1 and scores.count("乙") == 2
    assert len(calls) == 1 and len(recovered.messages) == 2
    assert recovered.total_output_tokens == 8
    assert all(op["status"] == "committed" for op in repository.list_operations(runner.id, kind="score"))


def test_settled_vote_abandons_failed_ballot_but_finalizes_successful_ballot(conversation):
    runner, repository, saver, llm, calls = conversation
    runner.status = "paused"
    runner.votes = [{"id": "v1", "question": "测试投票", "options": ["一", "二"],
                     "votes_per_person": 1, "status": "pending", "results": {}, "ballots": [], "error": None}]
    runner._persist()

    def vote(name, *_args):
        if name == "乙":
            raise RuntimeError("乙的投票调用失败")
        return {"choices": ["1"], "reason": "理由"}, {"prompt_tokens": 1, "completion_tokens": 1}

    llm.vote = vote
    runner._execute_vote("v1")
    ballots = repository.list_operations(runner.id, kind="ballot")
    assert {op["input"]["agent_id"]: op["status"] for op in ballots} == {
        "a0": "committed", "a1": "abandoned",
    }
    assert runner.votes[0]["status"] == "error" and runner.total_output_tokens == 1


def test_failed_auxiliary_attempt_keeps_known_usage_once(conversation):
    runner, repository, saver, llm, calls = conversation
    error = RuntimeError("提供商失败但已报告用量")
    error.usage = {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}

    def fail(_attempt):
        raise error

    with pytest.raises(RuntimeError, match="提供商失败"):
        runner._external("failed-score", "score", {}, fail)
    assert runner.total_prompt_tokens == 7 and runner.total_output_tokens == 2
    assert len(repository.list_attempts("failed-score")) == 1


def test_concurrent_resume_starts_one_owner_and_one_model_turn(conversation):
    runner, repository, saver, llm, calls = conversation
    runner.status = "paused"
    runner.total_max_tokens = 5
    runner._persist()
    epoch = runner.runner_epoch
    started, release = threading.Event(), threading.Event()
    original = llm.agent_turn

    def blocked(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    llm.agent_turn = blocked
    barrier = threading.Barrier(3)
    results = []

    def resume():
        barrier.wait()
        results.append(runner.resume())

    contenders = [threading.Thread(target=resume) for _ in range(2)]
    for thread in contenders:
        thread.start()
    barrier.wait()
    assert started.wait(5)
    for thread in contenders:
        thread.join(5)
        assert not thread.is_alive()
    assert sorted(results) == [False, True]
    assert runner.runner_epoch == epoch + 1
    release.set()
    runner._thread.join(5)
    assert not runner.is_alive() and calls == ["甲"]


def test_delete_inflight_direct_turn_prevents_late_message_and_checkpoint_recreation(conversation, monkeypatch):
    runner, repository, saver, llm, calls = conversation
    started, release, deleted = threading.Event(), threading.Event(), threading.Event()
    original = llm.agent_turn
    original_delete = repository.delete_conversation

    def blocked(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    def delete(conversation_id):
        result = original_delete(conversation_id)
        deleted.set()
        return result

    llm.agent_turn = blocked
    monkeypatch.setattr(repository, "delete_conversation", delete)
    runner.start()
    assert started.wait(5)
    results = []
    deleting = threading.Thread(target=lambda: results.append(runner.delete()))
    deleting.start()
    assert deleted.wait(5)
    release.set()
    deleting.join(5)
    assert not deleting.is_alive() and results == [True]
    assert repository.get_conversation(runner.id) is None
    assert not runner.messages and runner.whiteboard_content == ""
    assert list(saver.list(None)) == []


def test_explicit_resume_with_checkpoint_rebuilds_uncertain_dsh_turn_for_original_role(conversation):
    runner, repository, saver, llm, calls = conversation
    runner.harness = SimpleNamespace(close=lambda: None)
    runner.total_max_tokens = 5
    runner._persist()
    runner.executor.execute = lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt("进程中断"))
    with pytest.raises(KeyboardInterrupt):
        runner._run()
    repository.recover()
    recovered = GraphRunner.from_payload(repository.get_conversation(runner.id), llm,
                                         repository=repository, saver=saver)
    recovered.harness = SimpleNamespace(close=lambda: None)

    def execute(_runner, agent, system, history, *, on_usage, **kwargs):
        result, usage = llm.agent_turn(agent["name"], system, history, 100)
        on_usage({**usage, "_event_id": "explicit-dsh-retry"})
        return result, usage, {"pending": False}

    recovered.executor.execute = execute
    assert recovered.resume() is True
    recovered._thread.join(5)
    assert not recovered.is_alive() and calls == ["甲"]
    assert len(recovered.messages) == 1 and recovered._rr_index == 1
    assert recovered.total_output_tokens == 5
    assert any(op["status"] == "abandoned" for op in repository.list_operations(runner.id, kind="turn"))


def test_human_message_after_restart_can_abandon_old_epoch_uncertain_turn(conversation, monkeypatch):
    runner, repository, saver, llm, calls = conversation
    root = runner._prepare_operation()
    state = {"operation_id": root["operation_id"]}
    state.update(runner.nodes.load_operation(state))
    state.update(runner.nodes.select_speaker(state))
    repository.create_attempt("interrupted", state["turn_result_ref"], runner.runner_epoch)
    repository.update_operation(state["turn_result_ref"], status="running")
    repository.recover()
    recovered = GraphRunner.from_payload(repository.get_conversation(runner.id), llm,
                                         repository=repository, saver=saver)
    assert recovered.human_say("按最新输入重新讨论", target="a1") == "appended"
    assert recovered.messages[-1]["content"] == "按最新输入重新讨论"
    assert recovered._forced_next_idx == 1
    assert repository.get_operation(root["operation_id"])["status"] == "abandoned"
    assert repository.get_operation(state["turn_result_ref"])["status"] == "abandoned"
    assert not repository.list_operations(runner.id, statuses=("prepared", "running", "result_ready", "uncertain"))
    # The audit remains, while the abandoned child no longer blocks migration.
    assert repository.list_attempts(state["turn_result_ref"])[0]["status"] == "uncertain"
    assert not calls
    recovered.total_max_tokens = 5
    recovered._persist()
    recovered.start()
    recovered._thread.join(5)
    assert not recovered.is_alive() and calls == ["乙"]
    from app.engine import RUNNERS
    from app.services.conversations import conversation_service
    monkeypatch.setitem(RUNNERS, runner.id, recovered)
    migrated = conversation_service.migrate(runner.id, "legacy", llm)
    assert migrated.to_dict()["orchestration_backend"] == "legacy"


def test_vote_start_persistence_failure_releases_control_flag(conversation, monkeypatch):
    runner, repository, saver, llm, calls = conversation
    runner.status = "paused"
    runner._persist()
    original = repository.save_snapshot
    failed = []

    def save(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise RuntimeError("投票启动前写入失败")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "save_snapshot", save)
    llm.vote = lambda *_: pytest.fail("未落盘的投票不能启动模型")
    assert runner.start_vote("题目", ["一", "二"], 1) is None
    assert runner._vote_in_progress is False and runner._vote_thread is None
    assert runner.votes[-1]["status"] == "error"
    assert repository.get_conversation(runner.id)["votes"][-1]["status"] == "error"


def test_summary_start_persistence_failure_keeps_completed_and_releases_control_flag(conversation, monkeypatch):
    runner, repository, saver, llm, calls = conversation
    runner.status = "paused"
    runner.human_say("总结这段公开发言")
    original = repository.create_operation

    def create(operation, conversation_id, kind, **fields):
        if kind == "summary":
            raise RuntimeError("总结操作记录写入失败")
        return original(operation, conversation_id, kind, **fields)

    monkeypatch.setattr(repository, "create_operation", create)
    llm.summarize = lambda *_: pytest.fail("未落盘的总结不能启动模型")
    assert runner.summarize_now() is True
    assert runner.status == "completed" and "总结中断" in runner.summary
    assert runner._summary_in_progress is False
    assert repository.get_conversation(runner.id)["status"] == "completed"


def test_discussion_thread_start_failure_stays_observably_paused(conversation, monkeypatch):
    runner, repository, saver, llm, calls = conversation
    monkeypatch.setattr(threading.Thread, "start", lambda _: (_ for _ in ()).throw(RuntimeError("线程启动失败")))
    with pytest.raises(RuntimeError, match="线程启动失败"):
        runner.start()
    assert runner.status == "paused" and runner.paused_reason == "error"
    assert runner._thread is None and runner._segment_start is None
    assert repository.get_conversation(runner.id)["status"] == "paused"
    assert runner.resume() is False
    assert runner.status == "paused" and not calls
