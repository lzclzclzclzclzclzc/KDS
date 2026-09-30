import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db
from app.repositories.orchestration import OrchestrationRepository, RepositoryConflict


def _insert(repo, conversation_id="chat", **overrides):
    payload = {
        "status": "paused", "orchestration_backend": "langgraph", "runner_epoch": 0,
        "total_prompt_tokens": 5, "total_output_tokens": 2, "messages": [], "votes": [],
        "active_seconds": 10, "elapsed_seconds": 10, "total_max_tokens": 100,
        "total_duration_seconds": 100,
    }
    payload.update(overrides)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "INSERT INTO conversations (id, config_id, name, payload, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, "cfg", "测试", json.dumps(payload), payload["status"], db._now(), db._now()),
        )
    return repo.get_conversation(conversation_id)


@pytest.fixture
def repo(tmp_path):
    repository = OrchestrationRepository(tmp_path / "business.db")
    _insert(repository)
    return repository


def test_additive_migration_keeps_old_payload(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, config_id TEXT, name TEXT, payload TEXT, status TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO conversations VALUES ('old', 'c', '旧记录', '{\"messages\": [\"保留\"]}', 'paused', 'before', 'before')")
    repo = OrchestrationRepository(path)
    OrchestrationRepository(path)
    saved = repo.get_conversation("old")
    assert saved["messages"] == ["保留"]
    assert saved["state_rev"] == 0
    assert saved["updated_at"] == "before"
    assert "orchestration_backend" not in saved


def test_commit_is_idempotent_and_does_not_append_message_twice(repo):
    epoch = repo.claim("chat")
    saved = repo.get_conversation("chat")
    repo.create_operation("turn", "chat", "turn", runner_epoch=epoch, input={"history_boundary": 0})
    repo.update_operation("turn", status="result_ready", result={"speech": "你好"}, output={"vote_id": "v1"})
    saved["messages"].append({"content": "你好"})
    revision = repo.commit("turn", saved, saved["state_rev"], epoch)
    assert repo.commit("turn", dict(saved, messages=[{"content": "错误重放"}]), 0, epoch) == revision
    assert repo.get_conversation("chat")["messages"] == [{"content": "你好"}]
    assert repo.get_operation("turn")["committed_rev"] == revision
    assert repo.get_operation("turn")["output"] == {"vote_id": "v1"}
    assert repo.get_operation("turn")["committed_snapshot"]["messages"] == [{"content": "你好"}]
    assert repo.get_operation("turn")["committed_snapshot"]["state_rev"] == revision


def test_commit_failure_rolls_back_snapshot_and_command(repo):
    saved = repo.get_conversation("chat")
    command = repo.reserve_command("chat", {"message": "插话", "target": "a1"})
    saved = repo.get_conversation("chat")
    repo.create_operation("human", "chat", "human", runner_epoch=0)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("CREATE TRIGGER fail_commit BEFORE UPDATE OF status ON orchestration_operations WHEN NEW.status = 'committed' BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repo.commit("human", dict(saved, messages=[{"content": "插话"}]), saved["state_rev"], 0, consume_command_id=command["command_id"])
    assert repo.get_conversation("chat")["messages"] == []
    assert repo.get_conversation("chat")["state_rev"] == saved["state_rev"]
    assert repo.get_pending_command("chat")["command_id"] == command["command_id"]
    assert repo.get_operation("human")["status"] == "prepared"


def test_usage_deduplicates_and_survives_stale_snapshot(repo):
    old = repo.get_conversation("chat")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: repo.record_usage("event", "chat", prompt_tokens=10, completion_tokens=3), range(8)))
    assert results.count(True) == 1
    assert repo.get_conversation("chat")["total_prompt_tokens"] == 15
    with pytest.raises(RepositoryConflict):
        repo.save_snapshot("chat", old, expected_rev=old["state_rev"])
    repo.save_snapshot("chat", old)
    assert repo.get_conversation("chat")["total_output_tokens"] == 5
    with pytest.raises(RepositoryConflict):
        repo.record_usage("event", "chat", prompt_tokens=11, completion_tokens=3)


def test_epoch_rejects_late_result_but_accepts_known_usage(repo):
    first = repo.claim("chat")
    repo.create_operation("turn", "chat", "turn", runner_epoch=first)
    repo.update_operation("turn", status="running")
    repo.create_attempt("attempt", "turn", first)
    current = repo.get_conversation("chat")
    second = repo.claim("chat")
    assert second == first + 1
    with pytest.raises(RepositoryConflict):
        repo.commit("turn", current, current["state_rev"], first)
    with pytest.raises(RepositoryConflict):
        repo.update_attempt("attempt", status="result_ready", result={"speech": "迟到"})
    with pytest.raises(RepositoryConflict):
        repo.update_operation("turn", status="result_ready", result={"speech": "迟到"})
    assert repo.record_usage("late_usage", "chat", "turn", "attempt", "dsh", 1, 2)
    assert repo.get_conversation("chat")["messages"] == []


def test_saved_result_can_be_explicitly_adopted(repo):
    old_epoch = repo.claim("chat")
    repo.create_operation("turn", "chat", "turn", runner_epoch=old_epoch)
    repo.update_operation("turn", status="result_ready", result={"speech": "已保存"})
    new_epoch = repo.claim("chat")
    repo.update_operation("turn", runner_epoch=new_epoch)
    saved = repo.get_conversation("chat")
    repo.commit("turn", dict(saved, messages=[repo.get_operation("turn")["result"]]), saved["state_rev"], new_epoch)
    assert repo.get_conversation("chat")["messages"] == [{"speech": "已保存"}]
    assert repo.list_attempts("turn") == []


def test_command_slot_overwrites_and_consumes_atomically(repo):
    first = repo.reserve_command("chat", {"message": "旧消息", "target": "a0"}, command_id="old")
    second = repo.reserve_command("chat", {"message": "新消息", "target_agent_id": "a1"}, command_id="new")
    assert first["command_id"] != second["command_id"]
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT status FROM orchestration_commands WHERE command_id = 'old'").fetchone()[0] == "superseded"
    saved = repo.get_conversation("chat")
    assert saved["pending_human_message"] == "新消息"
    assert saved["pending_human_target"] == "a1"
    repo.create_operation("human", "chat", "human")
    repo.commit("human", dict(saved, messages=[{"content": "新消息"}], forced_next_idx=1), saved["state_rev"], 0, consume_command_id="new")
    assert repo.get_pending_command("chat") is None
    assert repo.get_conversation("chat")["pending_human_message"] is None
    assert repo.get_conversation("chat")["forced_next_idx"] == 1


def test_restart_preserves_result_and_marks_uncertain_work(repo):
    saved = repo.get_conversation("chat")
    repo.save_snapshot("chat", dict(saved, status="running", elapsed_seconds=20,
                                     votes=[{"id": "vote", "status": "running"}]))
    repo.create_operation("turn", "chat", "turn", status="running")
    repo.create_attempt("attempt", "turn", 0)
    repo.create_operation("saved", "chat", "turn", status="result_ready", result={"speech": "保留结果"})
    repo.create_operation("vote", "chat", "vote", status="running")
    assert repo.recover() == ["chat"]
    recovered = repo.get_conversation("chat")
    assert recovered["status"] == "paused"
    assert recovered["active_seconds"] == 20
    assert recovered["paused_reason"] == "manual"
    assert recovered["votes"][0]["status"] == "error"
    assert repo.list_attempts("turn")[0]["status"] == "uncertain"
    assert repo.get_operation("turn")["status"] == "uncertain"
    assert repo.get_operation("saved")["result"] == {"speech": "保留结果"}
    assert repo.get_operation("saved")["status"] == "result_ready"
    assert repo.get_operation("vote")["status"] == "abandoned"
    recovered_rev, recovered_epoch = recovered["state_rev"], recovered["runner_epoch"]
    assert repo.recover() == []
    assert repo.get_conversation("chat")["state_rev"] == recovered_rev
    assert repo.get_conversation("chat")["runner_epoch"] == recovered_epoch


def test_interrupt_command_preserves_pending_human_slot(repo):
    human = repo.reserve_command("chat", {"message": "待插话", "target": "a1"})
    interrupt = repo.reserve_command("chat", {}, kind="interrupt")
    assert repo.get_pending_command("chat")["command_id"] == human["command_id"]
    assert repo.get_conversation("chat")["pending_human_message"] == "待插话"
    assert repo.consume_command(interrupt["command_id"])
    assert repo.get_pending_command("chat")["command_id"] == human["command_id"]


def test_legacy_reservation_sync_preserves_payload_and_revision(repo):
    original = repo.get_conversation("chat")
    first = repo.sync_legacy_reservation("chat", "旧预约", 0)
    assert repo.sync_legacy_reservation("chat", "旧预约", 0)["command_id"] == first["command_id"]
    second = repo.sync_legacy_reservation("chat", "新预约", 1)
    assert second["command_id"] != first["command_id"]
    assert repo.get_pending_command("chat")["payload"] == {"content": "新预约", "target": 1}
    assert repo.get_conversation("chat") == original
    repo.reserve_command("chat", {}, kind="interrupt", command_id="interrupt")
    repo.sync_legacy_reservation("chat", None, None)
    assert repo.get_pending_command("chat") is None
    assert repo.get_conversation("chat") == original
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT status FROM orchestration_commands WHERE command_id = ?", (first["command_id"],)).fetchone()[0] == "superseded"
        assert conn.execute("SELECT status FROM orchestration_commands WHERE command_id = ?", (second["command_id"],)).fetchone()[0] == "consumed"
        assert conn.execute("SELECT status FROM orchestration_commands WHERE command_id = 'interrupt'").fetchone()[0] == "pending"


def test_interrupted_summary_stays_completed(repo):
    saved = repo.get_conversation("chat")
    repo.save_snapshot("chat", dict(saved, status="completed", summary=""))
    repo.create_operation("summary", "chat", "summary", status="running")
    repo.create_attempt("summary_attempt", "summary", 0)
    repo.recover()
    saved = repo.get_conversation("chat")
    assert saved["status"] == "completed"
    assert "总结中断" in saved["summary"]
    assert saved["can_resume"] is False
    assert repo.get_operation("summary")["status"] == "abandoned"


@pytest.mark.parametrize("saved_on_root", [True, False])
def test_restart_reconciles_saved_summary_without_another_model_attempt(repo, saved_on_root):
    saved = repo.get_conversation("chat")
    repo.save_snapshot("chat", dict(saved, status="completed", summary=""))
    repo.create_operation("summary", "chat", "summary", status="running")
    repo.create_operation("summary:model", "chat", "summary_model", parent_operation_id="summary", status="result_ready", result={"summary": "确定总结", "usage": {}})
    if saved_on_root:
        repo.update_operation("summary", status="result_ready", result={"summary": "确定总结", "usage": {}})
    repo.recover()
    assert repo.get_conversation("chat")["summary"] == "确定总结"
    assert repo.get_conversation("chat")["status"] == "completed"
    assert repo.get_operation("summary")["status"] == "committed"
    assert repo.list_attempts("summary:model") == []


def test_delete_prevents_all_late_writes(repo):
    repo.create_operation("turn", "chat", "turn", status="running")
    repo.create_attempt("attempt", "turn", 0)
    repo.reserve_command("chat", {"message": "消息"})
    repo.record_usage("usage", "chat", "turn", "attempt", completion_tokens=2)
    saved = repo.get_conversation("chat")
    assert repo.delete_conversation("chat")
    assert repo.get_conversation("chat") is None
    assert repo.get_operation("turn") is None
    assert repo.list_attempts("turn") == []
    assert repo.get_pending_command("chat") is None
    with pytest.raises(RepositoryConflict):
        repo.save_snapshot("chat", saved)
    with pytest.raises(RepositoryConflict):
        repo.record_usage("late", "chat", completion_tokens=2)


def test_shared_checkpointer_deletes_child_threads_and_stays_open(tmp_path):
    pytest.importorskip("langgraph")
    from app.orchestration.checkpointer import close_savers, get_saver
    from langgraph.graph import END, START, StateGraph
    from typing import TypedDict

    class State(TypedDict):
        value: int

    path = tmp_path / "checkpoints.db"
    saver = get_saver(path)
    assert get_saver(path) is saver
    builder = StateGraph(State)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    graph = builder.compile(checkpointer=saver)
    try:
        threads = ["conversation:chat", "conversation:chat:vote:v1", "conversation:other"]
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda thread_id: graph.invoke({"value": 1}, {"configurable": {"thread_id": thread_id}}), threads))
        saver.delete_conversation("chat")
        for thread_id in threads[:2]:
            assert saver.get_tuple({"configurable": {"thread_id": thread_id}}) is None
        assert saver.get_tuple({"configurable": {"thread_id": threads[2]}}) is not None
        assert saver.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(RuntimeError, match="deleted"):
            graph.invoke({"value": 2}, {"configurable": {"thread_id": threads[0]}})
        assert graph.invoke({"value": 3}, {"configurable": {"thread_id": threads[2]}})["value"] == 4
    finally:
        close_savers(path)


def test_file_checkpoint_resumes_failed_node_and_emits_custom_events(tmp_path):
    from app.orchestration.checkpointer import close_savers, get_saver
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph
    from typing import TypedDict

    class State(TypedDict):
        value: int

    first_calls, finish_calls = [], []
    should_fail = [True]

    def first(state):
        first_calls.append(state["value"])
        get_stream_writer()({"stage": "first_saved", "value": state["value"] + 1})
        return {"value": state["value"] + 1}

    def finish(state):
        finish_calls.append(state["value"])
        if should_fail[0]:
            raise RuntimeError("injected crash after first checkpoint")
        return {"value": state["value"] + 1}

    def build(saver):
        builder = StateGraph(State)
        builder.add_node("first", first)
        builder.add_node("finish", finish)
        builder.add_edge(START, "first")
        builder.add_edge("first", "finish")
        builder.add_edge("finish", END)
        return builder.compile(checkpointer=saver)

    path = tmp_path / "restart.db"
    config = {"configurable": {"thread_id": "conversation:restart"}}
    events = []
    try:
        graph = build(get_saver(path))
        with pytest.raises(RuntimeError, match="injected crash"):
            for event in graph.stream({"value": 1}, config, stream_mode="custom", durability="sync"):
                events.append(event)
        assert events == [{"stage": "first_saved", "value": 2}]
        assert graph.get_state(config).next == ("finish",)
        close_savers(path)
        should_fail[0] = False
        graph = build(get_saver(path))
        assert graph.invoke(None, config, durability="sync")["value"] == 3
        assert first_calls == [1]
        assert finish_calls == [2, 2]
        assert graph.get_state(config).next == ()
    finally:
        close_savers(path)
