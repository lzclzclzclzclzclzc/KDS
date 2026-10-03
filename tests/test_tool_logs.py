import json

import pytest
from flask import Flask

import app.db as db
from app.engine import ConversationRunner
from app.llm import LLMClient
from app.tool_logs import MAX_TOOL_LOGS, log_text, merge_tool_log, tool_log_update
from test_harness import FakeRuntime, make_manager
from test_review_fixes import _api_module


def call(call_id="c1", step=1, **extra):
    return {"type": "tool/call", "seq": 1, "time": 1790664058713, "data": {
        "callId": call_id, "name": "read", "arguments": '{"file_path":"evidence.txt"}',
        "step": step, **extra,
    }}


def result(call_id="c1", legacy=False, error=False):
    content = [{"type": "text", "text": "verified-answer=42"},
               {"type": "reasoning", "text": "not a displayable tool result"}]
    message = {"source": {"callId": call_id}, "isError": error, "content": content}
    if legacy:
        message = {"source": {"callId": call_id}, "content": [{"type": "tool-result",
            "toolCallId": call_id, "isError": error, "content": content}]}
    return {"type": "tool/result", "seq": 2, "time": 1790664058729,
            "data": {"step": 1, "message": message}}


def runner():
    value = ConversationRunner("logs-test", "cfg", "日志测试", {
        "agent_backend": "dsh", "agents": [{"id": "a0", "name": "甲"}],
        "total_max_tokens": 7,
    }, LLMClient(mock=True))
    value._persist = lambda: None
    return value


def progress(value, event, run_id="run"):
    value._harness_progress({"agent_id": "a0", "agent_name": "甲", "stage": "使用工具",
                             "tool_log": tool_log_update(event, run_id)})


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("error", [False, True])
def test_versions_match_call_result_and_do_not_include_reasoning(legacy, error):
    start = tool_log_update(call(), "run")
    end = tool_log_update(result(legacy=legacy, error=error), "run")
    assert start["id"] == end["id"]
    assert start["status"] == "running" and start["tool"] == "read"
    assert json.loads(start["arguments"]) == {"file_path": "evidence.txt"}
    assert end["status"] == ("error" if error else "completed")
    assert end["result"] == "verified-answer=42"
    assert start["started_at"] < end["finished_at"]
    assert tool_log_update({"type": "assistant/message", "data": {"text": "private"}}, "run") is None


def test_identity_separates_parallel_calls_steps_and_repeated_turns():
    keys = {tool_log_update(event, run_id)["id"] for event, run_id in [
        (call(), "one"), (call("c2"), "one"), (call(step=2), "one"), (call(), "two"),
    ]}
    assert len(keys) == 4


def test_session_identity_keeps_legitimate_repeated_arguments_and_matches_result():
    first = tool_log_update(call(), "activation", session_id="session-a")
    second = tool_log_update(call("c2"), "activation", session_id="session-a")
    other_session = tool_log_update(call(), "activation", session_id="session-b")
    final = result()
    final["sourceEventSeqs"] = [1]
    completed = tool_log_update(final, "activation", session_id="session-a")
    assert len({row["id"] for row in (first, second, other_session)}) == 3
    assert first["arguments"] == second["arguments"] == other_session["arguments"]
    assert completed["id"] == first["id"]
    assert completed["call_id"] == "c1" and completed["session_id"] == "session-a"
    assert completed["call_event_seq"] == 1 and completed["result_event_seq"] == 2
    merged = merge_tool_log(completed, first)
    assert merged["status"] == "completed" and merged["result"] == "verified-answer=42"
    assert merged["arguments"] == first["arguments"]


def test_legacy_engine_does_not_reopen_a_call_when_start_is_replayed_late():
    value = runner()
    progress(value, result())
    progress(value, call())
    progress(value, call("c2"))
    assert len(value.harness_logs) == 2
    assert value.harness_logs[0]["status"] == "completed"
    assert value.harness_logs[0]["tool"] == "read"
    assert value.harness_logs[1]["status"] == "running"


def test_secrets_are_masked_before_bounded_display():
    text = log_text('DEEPSEEK_API_KEY=hidden1\nAuthorization: Bearer hidden2\n'
                    '{"password":"hidden3", "file_path":"ok.txt"}\nknown-secret', 400,
                    ("known-secret", ""))
    assert all(secret not in text for secret in ("hidden1", "hidden2", "hidden3", "known-secret"))
    assert "ok.txt" in text and "已隐藏" in text
    log = tool_log_update(result(), "run", ("verified-answer",))
    assert "verified-answer" not in log["result"]
    long = result()
    long["data"]["message"]["content"][0]["text"] = "x" * 10000
    assert len(tool_log_update(long, "run")["result"]) < 6100
    assert "已截取" in tool_log_update(long, "run")["result"]


def test_logs_are_live_separate_from_model_context_and_snapshots():
    value = runner()
    persisted = []
    value._persist = lambda: persisted.append(value.to_dict())
    progress(value, call("c1"))
    progress(value, call("c2"))
    progress(value, result("c2"))
    progress(value, result("c1", error=True))
    assert len(value.harness_logs) == 2
    assert [entry["status"] for entry in value.harness_logs] == ["error", "completed"]
    assert len(persisted) == 4 and persisted[0]["harness_logs"][0]["status"] == "running"
    assert "tool_log" not in value.harness_activity
    assert "verified-answer" not in json.dumps(value._history())
    assert "harness_logs" not in value.to_dict(include_tool_logs=False)
    index = value.to_dict(include_tool_logs=False)["harness_log_index"]
    assert len(index) == 2 and index[0]["agent_id"] == "a0" and index[0]["turn"] == 1
    assert all("arguments" not in item and "result" not in item for item in index)
    snapshot = value.to_dict()
    snapshot["harness_logs"][0]["result"] = "changed"
    assert value.harness_logs[0]["result"] == "verified-answer=42"


def test_bounded_history_and_restart_mark_unfinished_calls():
    value = runner()
    for i in range(MAX_TOOL_LOGS + 3):
        progress(value, call(str(i)))
    snapshot = value.to_dict()
    restored = ConversationRunner.from_payload(snapshot, LLMClient(mock=True))
    assert len(restored.harness_logs) == MAX_TOOL_LOGS
    assert restored.harness_log_dropped == 3
    assert all(entry["status"] == "interrupted" for entry in restored.harness_logs)
    assert all(entry["status"] == "running" for entry in value.harness_logs)
    assert restored.harness_log_rev > snapshot["harness_log_rev"]
    snapshot.pop("harness_logs")
    assert ConversationRunner.from_payload(snapshot, LLMClient(mock=True)).harness_logs == []


def test_observer_deduplicates_events_and_engine_retains_logs_on_pause(tmp_path):
    class LoggedRuntime(FakeRuntime):
        def run(self, prompt, *, session_id, on_notification):
            final = super().run(prompt, session_id=session_id, on_notification=on_notification)
            for event in (call(), call(), result()):
                self.emit(on_notification, session_id, event["seq"] + 10, event["type"], event["data"])
            return final
    value = runner()
    value.harness, _ = make_manager(tmp_path, factory=LoggedRuntime)
    value._run()
    assert value.status == "paused"
    assert len(value.harness_logs) == 2  # FakeRuntime's unpaired call + matched call.
    assert [item["status"] for item in value.harness_logs] == ["interrupted", "completed"]
    assert value.harness_activity is None
    assert value.messages[0]["agent_id"] == "a0"
    assert value.messages[0]["round"] + 1 == value.harness_logs[0]["turn"]


@pytest.mark.parametrize("live", [True, False])
def test_api_fetches_log_bodies_only_on_demand_and_persists_them(tmp_path, monkeypatch, live):
    api = _api_module()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "logs.db")
    db.init_db(db.DB_PATH)
    value = runner()
    progress(value, call())
    payload = value.to_dict()
    db.create_conversation(value.id, value.config_id, value.name, payload)
    monkeypatch.setattr(api, "RUNNERS", {value.id: value} if live else {})
    app = Flask(__name__)
    app.register_blueprint(api.api_bp)
    client = app.test_client()
    brief = client.get(f"/api/conversations/{value.id}").get_json()
    assert "harness_logs" not in brief and brief["harness_log_count"] == 1
    detailed = client.get(f"/api/conversations/{value.id}?tool_logs=1").get_json()
    assert len(detailed["harness_logs"]) == 1
    assert detailed["harness_logs"][0]["status"] == ("running" if live else "interrupted")
    assert brief["harness_log_index"][0]["status"] == detailed["harness_logs"][0]["status"]
    assert "arguments" not in brief["harness_log_index"][0]
    if not live:
        assert detailed["harness_log_rev"] == brief["harness_log_rev"] > payload["harness_log_rev"]
    assert db.get_conversation(value.id)["harness_logs"][0]["arguments"] == payload["harness_logs"][0]["arguments"]
