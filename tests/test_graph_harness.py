"""Graph/DSH compatibility using the real manager and local fake runtimes."""

import json
import os
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app import db
from app.harness import HarnessManager, HarnessSettings
from app.llm import LLMClient
from app.orchestration.runtime import GraphRunner
from app.repositories.orchestration import OrchestrationRepository
from test_harness import CancellableRuntime, FakeRuntime, make_manager
from test_json_repair import repairing_manager, reply


def make_graph_runner(tmp_path, monkeypatch, **overrides):
    path = tmp_path / "business.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    repository = OrchestrationRepository(path)
    config = {
        "agents": [{"id": "a0", "name": "甲"}, {"id": "a1", "name": "乙"}],
        "agent_backend": "dsh", "orchestration_backend": "langgraph",
        "first_speaker": "a0", "total_max_tokens": 100,
        "whiteboard_enabled": True, "whiteboard_editors": ["a0"],
    }
    config.update(overrides)
    runner = GraphRunner("graph-harness", "config", "graph harness", config, LLMClient(mock=True),
                         repository=repository, saver=InMemorySaver())
    db.create_conversation(runner.id, runner.config_id, runner.name, runner.to_dict())
    return runner


def test_manager_and_role_sessions_survive_multiple_turn_graphs_until_pause(tmp_path, monkeypatch):
    runner = make_graph_runner(tmp_path, monkeypatch, total_max_tokens=21)
    runtime_list = []

    class LiveRuntime(FakeRuntime):
        def run(self, *args, **kwargs):
            assert not any(runtime.closed for runtime in runtime_list), "a manager closed between graph units"
            return super().run(*args, **kwargs)

    manager, runtime_list = make_manager(tmp_path / "dsh", factory=LiveRuntime)
    runner.harness = manager
    close_calls = []
    original_close = manager.close

    def close():
        close_calls.append(runner.status)
        original_close()

    monkeypatch.setattr(manager, "close", close)
    runner._claim()
    runner._run()
    assert runner.status == "paused" and runner.paused_reason == "limit"
    assert [message["agent_id"] for message in runner.messages] == ["a0", "a1", "a0"]
    assert close_calls == ["paused"]
    assert len(runtime_list) == 2 and all(runtime.closed for runtime in runtime_list)
    assert [len(runtime.requests) for runtime in runtime_list] == [2, 1]
    assert runtime_list[0].requests[0][0] == runtime_list[0].requests[1][0]
    assert runner.total_output_tokens == 21 and runner.total_prompt_tokens == 135
    assert runner.repository.get_conversation(runner.id)["total_output_tokens"] == 21


def test_graph_deduplicates_usage_even_after_committed_result_and_pause(tmp_path, monkeypatch):
    runner = make_graph_runner(tmp_path, monkeypatch, total_max_tokens=3)

    class DuplicateManager:
        def run_turn(self, *, agent, system, history, state, remaining_output, should_stop, on_progress, on_usage):
            self.callback = on_usage
            self.delta = {"_event_id": "sdk-delivery", "prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}
            on_usage(self.delta)
            on_usage(dict(self.delta))
            return ({"speech": "可发布", "whiteboard_ops": [], "propose_end": False},
                    {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
                    {"session_id": "session", "history_cursor": len(history) + 1, "pending": False})

        def close(self):
            pass

    runner.harness = manager = DuplicateManager()
    runner._claim()
    runner._run()
    manager.callback(dict(manager.delta))
    assert runner.total_output_tokens == 3 and runner.total_prompt_tokens == 8
    assert len(runner.messages) == 1
    with closing(db._connect(runner.repository.db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1


def test_graph_failed_json_repairs_keep_all_usage_without_partial_publication(tmp_path, monkeypatch):
    runner = make_graph_runner(tmp_path, monkeypatch)
    runner.harness, runtimes, repairs = repairing_manager(
        tmp_path / "dsh", responses=[reply('{}'), reply('{}')],
    )
    runner._claim()
    runner._run()
    assert runner.status == "paused" and runner.paused_reason == "error"
    assert runner.messages == [] and runner.whiteboard_content == ""
    assert runner.total_output_tokens == 17
    assert runner.total_prompt_tokens == 67
    assert runner._forced_next_idx == 0
    assert runner.harness_state["a0"]["pending"] is True
    assert len(repairs) == 2 and len(runtimes[0].requests) == 1 and runtimes[0].closed
    persisted = runner.repository.get_conversation(runner.id)
    assert persisted["messages"] == [] and persisted["total_output_tokens"] == 17
    with closing(db._connect(runner.repository.db_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 4


def test_late_progress_after_new_epoch_is_rejected_but_known_usage_is_kept(tmp_path, monkeypatch):
    runner = make_graph_runner(tmp_path, monkeypatch)
    runner._claim()
    old_epoch = runner.runner_epoch
    runner.repository.create_operation("old-turn", runner.id, "turn", runner_epoch=old_epoch)
    runner.repository.create_attempt("old-attempt", "old-turn", old_epoch)
    runner.repository.claim(runner.id)
    runner._progress({
        "agent_id": "a0", "agent_name": "甲", "stage": "迟到",
        "state": {"session_id": "stale"},
        "tool_log": {"id": "stale-log", "tool": "read", "status": "running"},
    }, old_epoch)
    assert runner.harness_activity is None and runner.harness_logs == [] and runner.harness_state == {}
    delta = {"_event_id": "settled-old-usage", "prompt_tokens": 7, "completion_tokens": 2}
    runner._usage("old-turn", "old-attempt", "dsh", delta)
    runner._usage("old-turn", "old-attempt", "dsh", dict(delta))
    assert runner.total_output_tokens == 2 and runner.total_prompt_tokens == 7


def test_graph_pause_cancels_current_dsh_turn_without_publishing(tmp_path, monkeypatch):
    runner = make_graph_runner(tmp_path, monkeypatch)
    runner.harness, runtimes = make_manager(tmp_path / "dsh", factory=CancellableRuntime, timeout=3)
    runner.start()
    deadline = time.monotonic() + 3
    while not any(getattr(runtime, "running", False) for runtime in runtimes) and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert any(getattr(runtime, "running", False) for runtime in runtimes)
        runner.interrupt()
        runner._thread.join(4)
        assert not runner.is_alive()
        assert runner.status == "paused" and runner.paused_reason == "manual"
        assert runner.messages == [] and runner.whiteboard_content == ""
        assert runner.total_output_tokens == 2 and runner._forced_next_idx == 0
        assert runner.harness_state["a0"]["pending"] is True
        assert runtimes[0].closed
        assert runner.repository.get_conversation(runner.id)["total_output_tokens"] == 2
    finally:
        runner._interrupt_requested = True
        if runner.is_alive():
            runner._thread.join(4)
        runner.harness.close()


def test_graph_filters_unauthorized_actions_and_keeps_tool_data_out_of_public_context(tmp_path, monkeypatch):
    runner = make_graph_runner(
        tmp_path, monkeypatch, total_max_tokens=7,
        whiteboard_editors=["a1"], end_vote_enabled=True, end_vote_proposers=["a1"],
    )

    class PrivateToolRuntime(FakeRuntime):
        def emit(self, callback, session, seq, event_type, data):
            if event_type == "tool/call":
                data = {**data, "callId": "read-1", "step": 1,
                        "arguments": {"api_key": "test-key", "file_path": "private.txt"}}
            super().emit(callback, session, seq, event_type, data)

        def run(self, prompt, *, session_id, on_notification):
            result = super().run(prompt, session_id=session_id, on_notification=on_notification)
            self.emit(on_notification, session_id, 5, "tool/result", {
                "callId": "read-1", "step": 1,
                "message": {"content": [{"type": "text", "text": "raw-private-evidence; secret=test-key"}]},
            })
            result.final_response = json.dumps({
                "speech": "公开发言", "propose_end": True,
                "whiteboard": {"ops": [{"op": "set", "content": "未经授权"}]},
            })
            return result

    runner.harness, _ = make_manager(tmp_path / "dsh", factory=PrivateToolRuntime)
    runner._claim()
    runner._run()
    assert len(runner.messages) == 1 and runner.messages[0]["content"] == "公开发言"
    assert not runner.messages[0].get("proposed_end")
    assert runner.votes == [] and runner.whiteboard_content == ""
    assert "raw-private-evidence" not in json.dumps(runner._history())
    assert "raw-private-evidence" not in runner._log_text()
    assert len(runner.harness_logs) == 1 and runner.harness_logs[0]["status"] == "completed"
    assert "test-key" not in json.dumps(runner.harness_logs)
    assert "harness_logs" not in runner.to_dict(include_tool_logs=False)
    assert "raw-private-evidence" not in json.dumps(runner.to_dict(False)["harness_log_index"])


@pytest.mark.skipif(os.getenv("KDS_TEST_DSH_RUNTIME") != "1", reason="需要显式启用本机 DSH 运行时集成验证")
def test_graph_runner_with_real_sdk_and_local_scripted_api(tmp_path, monkeypatch):
    """Exercise graph-to-manager-to-SDK wiring without external credentials."""
    requests = []
    final = json.dumps({"speech": "本地模拟 API 验证完成"}, ensure_ascii=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path not in {"/v1/chat/completions", "/anthropic/v1/messages"}:
                self.send_response(404)
                self.end_headers()
                return
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if self.path.endswith("/messages"):
                chunks = [
                    {"type": "message_start", "message": {"id": "graph", "role": "assistant",
                     "usage": {"input_tokens": 20, "cache_read_input_tokens": 10, "output_tokens": 0}}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": final}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 10}},
                    {"type": "message_stop"},
                ]
                for chunk in chunks:
                    self.wfile.write(("event: " + chunk["type"] + "\ndata: " + json.dumps(chunk) + "\n\n").encode())
            else:
                chunks = [
                    {"id": "graph", "object": "chat.completion.chunk", "choices": [{"index": 0,
                     "delta": {"role": "assistant", "content": final}, "finish_reason": None}]},
                    {"id": "graph", "object": "chat.completion.chunk", "choices": [{"index": 0,
                     "delta": {}, "finish_reason": "stop"}], "usage": {
                     "prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40,
                     "prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 20}},
                ]
                for chunk in chunks:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runner = make_graph_runner(tmp_path, monkeypatch, total_max_tokens=10)
    runner.harness = HarnessManager(runner.id, HarnessSettings(
        root=tmp_path / "dsh", api_key="local-test-key", timeout=30,
        base_url=f"http://127.0.0.1:{server.server_port}" + (
            "/anthropic" if os.getenv("KDS_TEST_DSH_BIN") else "/v1"),
        dsh_bin=os.getenv("KDS_TEST_DSH_BIN") or None, tools=("read",),
    ))
    try:
        runner._claim()
        runner._run()
        assert runner.status == "paused" and runner.paused_reason == "limit", runner.error
        assert len(requests) == len(runner.messages) == 1
        assert runner.messages[0]["content"] == "本地模拟 API 验证完成"
        assert runner.total_output_tokens == 10 and runner.total_prompt_tokens == 30
        assert runner.harness_state["a0"]["pending"] is False
    finally:
        runner.harness.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
