"""Run the public conversation contract against both engines."""
import threading
import sqlite3
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app import db
from app.engine import ConversationRunner
from app.llm import LLMClient
from app.orchestration.runtime import GraphRunner
from app.repositories.orchestration import OrchestrationRepository


def config(**changes):
    value = {"agent_backend": "direct", "agents": [
        {"id": "a", "name": "甲", "system_prompt": "A", "visibility": ["all"]},
        {"id": "b", "name": "乙", "system_prompt": "B", "visibility": []},
        {"id": "c", "name": "丙", "system_prompt": "C", "visibility": []}],
        "first_speaker": "a", "scheduling_mode": "round_robin", "round_robin_order": ["a", "b", "c"],
        "single_max_tokens": 40, "total_max_tokens": 15,
        "whiteboard_enabled": True, "whiteboard_editors": ["a", "b"],
        "end_vote_enabled": True, "end_vote_proposers": ["a"]}
    value.update(changes)
    return value


def scripted_llm(propose=False, agree=True):
    llm = LLMClient(mock=True)
    llm.agent_turn = lambda name, *_: ({"speech": name + "发言", "propose_end": propose,
        "whiteboard_ops": [{"op": "append", "content": name}]}, {"prompt_tokens": 2, "completion_tokens": 5})
    llm.vote = lambda *_: ({"choices": ["1" if agree else "2"], "reason": "理由"},
                          {"prompt_tokens": 1, "completion_tokens": 1})
    llm.summarize = lambda *_: ("总结", {"prompt_tokens": 3, "completion_tokens": 4})
    return llm


@pytest.fixture
def make_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "business.db")
    db.init_db(db.DB_PATH)
    counter = 0
    def make(kind="langgraph", cfg=None, llm=None):
        nonlocal counter
        counter += 1
        args = (str(counter), "cfg", "测试", cfg or config(), llm or scripted_llm())
        if kind == "legacy":
            runner = ConversationRunner(*args)
        else:
            runner = GraphRunner(*args, repository=OrchestrationRepository(db.DB_PATH), saver=InMemorySaver())
        db.create_conversation(runner.id, "cfg", "测试", runner.to_dict())
        return runner
    return make


def run(runner):
    runner.start()
    runner._thread.join(10)
    assert not runner.is_alive(), runner.to_dict()
    assert runner.error is None, runner.error


def public(snapshot):
    return {key: snapshot[key] for key in ("status", "paused_reason", "turn", "total_output_tokens",
                                           "total_prompt_tokens", "whiteboard", "heat", "last_agent_idx")}, [
        {k: v for k, v in m.items() if k not in {"ts", "operation_id"}} for m in snapshot["messages"]]


def test_scripted_turns_match_legacy(make_runner):
    legacy = make_runner("legacy")
    graph = make_runner()
    run(legacy)
    run(graph)
    assert public(legacy.to_dict()) == public(graph.to_dict())
    assert graph.whiteboard_content == "甲\n乙"
    assert graph.resume() is False
    assert graph.summarize_now() is True
    assert graph.status == "completed" and graph.summary == "总结"
    assert graph.total_output_tokens == 19


@pytest.mark.parametrize("kind", ["legacy", "langgraph"])
def test_unanimous_end_vote_only_pauses(make_runner, kind):
    runner = make_runner(kind, config(total_max_tokens=100), scripted_llm(propose=True))
    run(runner)
    assert runner.status == "paused" and runner.paused_reason == "vote_end"
    assert runner.turn == 1
    assert runner.votes[0]["kind"] == "end" and runner.votes[0]["agreed"] is True
    assert runner.total_output_tokens == 8


def test_vote_history_frozen_and_completed_vote_allowed(make_runner):
    entered, release = threading.Event(), threading.Event()
    histories = []
    llm = scripted_llm()
    def vote(name, system, history, *_):
        histories.append(history)
        entered.set()
        assert release.wait(5)
        return {"choices": ["1", "1"], "reason": "理由"}, {"prompt_tokens": 1, "completion_tokens": 2}
    llm.vote = vote
    runner = make_runner(llm=llm)
    runner.status = "paused"
    runner._persist()
    runner.start_vote("题目", ["是", "否"], 2)
    assert entered.wait(5)
    assert not runner.resume() and not runner.summarize_now()
    assert runner.start_vote("重复", ["是", "否"], 1) is None
    assert runner.human_say("中途消息") == "appended"
    release.set()
    runner._vote_thread.join(10)
    assert runner.votes[0]["status"] == "completed", runner.votes[0]
    assert histories == ["", "", ""]
    assert runner.messages[0]["content"] == "中途消息"
    assert runner.votes[0]["results"] == {"1": 6, "2": 0}
    runner.summarize_now()
    assert runner.start_vote("完成后投票", ["是", "否"], 1) is not None
    runner._vote_thread.join(10)
    assert runner.status == "completed"


def test_running_reservation_survives_interrupt_and_restore(make_runner):
    entered, release = threading.Event(), threading.Event()
    llm = scripted_llm()
    original = llm.agent_turn
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    llm.agent_turn = blocked
    runner = make_runner(cfg=config(total_max_tokens=100), llm=llm)
    runner.start()
    assert entered.wait(5)
    assert runner.human_say("预约消息", target="c") == "reserved"
    runner.interrupt()
    release.set()
    runner._thread.join(10)
    saved = runner.repository.get_conversation(runner.id)
    assert saved["pending_human_message"] == "预约消息"
    restored = GraphRunner.from_payload(saved, scripted_llm(), repository=runner.repository, saver=runner.saver)
    assert restored.pending_human_target == 2
    restored.total_max_tokens = 15
    restored._persist()
    assert restored.resume()
    restored._thread.join(10)
    assert [(m["role"], m["speaker"]) for m in restored.messages[:3]] == [
        ("agent", "甲"), ("human", "人类"), ("agent", "丙")]


def test_summary_failure_remains_completed(make_runner):
    runner = make_runner()
    run(runner)
    runner.llm.summarize = lambda *_: (_ for _ in ()).throw(RuntimeError("模拟断网"))
    assert runner.summarize_now()
    assert runner.status == "completed"
    assert runner.summary == "总结失败：模拟断网"


def test_many_units_do_not_share_recursion_budget(make_runner):
    runner = make_runner(cfg=config(total_max_tokens=205))
    run(runner)
    assert runner.turn == 41
    assert runner.paused_reason == "limit"


def test_round_robin_skips_score_context_and_historical_payloads(make_runner, monkeypatch):
    runner = make_runner()
    runner.repository.create_operation("historical", runner.id, "advance", status="committed")
    with sqlite3.connect(runner.repository.db_path) as conn:
        conn.execute("UPDATE orchestration_operations SET input = 'unreadable', "
                     "committed_snapshot = 'unreadable' WHERE operation_id = 'historical'")

    def unexpected_context(*args):
        raise AssertionError("轮流发言不应生成评分上下文")

    monkeypatch.setattr(runner, "_build_score_system", unexpected_context)
    monkeypatch.setattr(runner, "_log_text", unexpected_context)
    run(runner)
    assert [message["speaker"] for message in runner.messages] == ["甲", "乙", "丙"]
    assert runner.total_output_tokens == 15


def test_willingness_scores_keep_frozen_context_and_agent_order(make_runner):
    histories = []
    llm = scripted_llm()

    def score(name, system, history, turn):
        histories.append((name, history, turn))
        return 50, {"prompt_tokens": 0, "completion_tokens": 0}

    llm.willingness_score = score
    runner = make_runner(cfg=config(scheduling_mode="willingness", total_max_tokens=10), llm=llm)
    run(runner)
    assert len(runner.messages) == 2
    assert sorted(name for name, _, _ in histories) == sorted(["甲", "乙", "丙"])
    assert [score["name"] for score in runner.messages[-1]["scores"]] == ["甲", "乙", "丙"]
    assert all(turn == 1 and history == "甲: 甲发言" for _, history, turn in histories)
    assert runner.total_output_tokens == 10
