"""Opt-in real SDK/runtime tests with a local scripted API; no DeepSeek credits.

KDS_TEST_DSH_RUNTIME=1 python -m pytest tests/test_harness_integration.py -q
Set KDS_TEST_DSH_BIN to also exercise an installed CLI instead of the SDK wheel.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.harness import HarnessManager, HarnessSettings, HarnessTurnError, _identity


pytestmark = pytest.mark.skipif(os.getenv("KDS_TEST_DSH_RUNTIME") != "1",
                                reason="需要显式启用本机 dsh 运行时集成验证")


@pytest.mark.parametrize("mode", ["normal", "steps", "tools", "budget", "cancel"])
def test_real_sdk_tool_loop_and_durable_resume(tmp_path, mode):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass  # Runtime shutdown also closes speculative HTTP connections.

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            # Match the official routes instead of accepting every URL: a
            # Messages request sent to /v1/messages must fail as it does live.
            if self.path not in {"/v1/chat/completions", "/anthropic/v1/messages"}:
                self.send_response(404)
                self.end_headers()
                return
            requests.append(body)
            if mode == "cancel":
                time.sleep(1)
            messages = body["messages"]
            last = messages[-1]
            last_blocks = last.get("content") if isinstance(last.get("content"), list) else []
            if last["role"] == "tool" or any(b.get("type") == "tool_result" for b in last_blocks):
                delta = {"content": json.dumps({"speech": "资料显示答案是42。",
                    "whiteboard": {"ops": [{"op": "append", "content": "答案：42"}]}}, ensure_ascii=False)}
                finish = "stop"
            else:
                delta = {"tool_calls": [{"index": 0, "id": "call-" + str(len(requests)),
                    "type": "function", "function": {"name": "read",
                    "arguments": json.dumps({"file_path": "evidence.txt"})}}]}
                if mode == "tools":
                    delta["tool_calls"].append({"index": 1, "id": "second-call", "type": "function",
                        "function": {"name": "read", "arguments": json.dumps({"file_path": "evidence.txt"})}})
                finish = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if self.path.endswith("/messages"):
                # dsh 0.1.7 speaks DeepSeek Messages; the 0.1.5 wheel speaks
                # Chat Completions. Exercise each runtime's actual protocol.
                chunks = [{"type": "message_start", "message": {"id": "test", "role": "assistant",
                    "usage": {"input_tokens": 20, "cache_read_input_tokens": 10, "output_tokens": 0}}}]
                if finish == "stop":
                    blocks = [{"type": "text", "text": delta["content"]}]
                else:
                    blocks = [{"type": "tool_use", "id": call["id"], "name": "read",
                        "input": {"file_path": "evidence.txt"}} for call in delta["tool_calls"]]
                for index, block in enumerate(blocks):
                    chunks.extend([{"type": "content_block_start", "index": index, "content_block": block},
                                   {"type": "content_block_stop", "index": index}])
                chunks.extend([{"type": "message_delta", "delta": {
                    "stop_reason": "end_turn" if finish == "stop" else "tool_use"}, "usage": {"output_tokens": 10}},
                    {"type": "message_stop"}])
                for chunk in chunks:
                    self.wfile.write(("event: " + chunk["type"] + "\ndata: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                return
            chunks = [
                {"id": "test", "object": "chat.completion.chunk", "choices": [{"index": 0,
                    "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                {"id": "test", "object": "chat.completion.chunk", "choices": [{"index": 0,
                    "delta": {}, "finish_reason": finish}], "usage": {
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
    settings = HarnessSettings(root=tmp_path, api_key="local-test-key",
        base_url=f"http://127.0.0.1:{server.server_port}" + (
            "/anthropic" if os.getenv("KDS_TEST_DSH_BIN") else "/v1"),
        dsh_bin=os.getenv("KDS_TEST_DSH_BIN") or None, tools=("read",), timeout=45,
        max_steps=1 if mode == "steps" else 8, max_tool_calls=1 if mode == "tools" else 12)
    manager = HarnessManager("integration", settings)
    workspace = manager.root / _identity("a0") / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "evidence.txt").write_text("verified-answer=42", encoding="utf-8")
    progress, usage = [], []
    args = dict(agent={"id": "a0", "name": "甲"},
        system='你是甲。保留字面量 {{literal}}。读取 evidence.txt，再交付 {"speech":"...","whiteboard":{"ops":[]}}。',
        history=[{"role": "user", "content": "人类：请核对资料。"}], state={},
        remaining_output=10 if mode == "budget" else 2000,
        should_stop=lambda: "manual" if mode == "cancel" and requests else None,
        on_progress=progress.append, on_usage=usage.append)
    try:
        if mode != "normal":
            with pytest.raises(HarnessTurnError) as error:
                manager.run_turn(**args)
            assert error.value.reason == {"steps": "steps", "tools": "tools", "budget": "limit", "cancel": "manual"}[mode]
            assert len(requests) == 1
            if mode != "cancel":
                assert sum(u["completion_tokens"] for u in usage) == 10
            return
        turn, totals, state = manager.run_turn(**args)
        assert turn["whiteboard_ops"][0]["content"] == "答案：42"
        assert totals["completion_tokens"] == 20
        assert totals["prompt_tokens"] == 60
        assert len(requests) == 2
        assert "verified-answer=42" in json.dumps(requests[-1]["messages"])
        assert "{{literal}}" in json.dumps(requests[0])
        assert {t.get("function", t)["name"] for t in requests[0]["tools"]} == {"read"}
        args.update(state=state, system=args["system"] + " 当前白板已更新：board-revision-two。",
                    history=[*args["history"], {"role": "user", "content": "甲：答案42"},
                             {"role": "user", "content": "乙：再核对一次"}])
        _, _, state_live = manager.run_turn(**args)
        assert state_live["session_id"] == state["session_id"]
        assert "board-revision-two" in json.dumps(requests[2])
        assert len(requests) == 4
        manager.close()
        # The released SDK cannot reopen persisted IDs. Replay the KDS checkpoint
        # into a new runtime session while retaining private evidence/workspace.
        manager = HarnessManager("integration", settings)
        args.update(state=state_live, history=[*args["history"], {"role": "user", "content": "甲：确认42"},
                                              {"role": "user", "content": "乙：请继续"}])
        _, _, second_state = manager.run_turn(**args)
        assert second_state["session_id"] != state["session_id"]
        assert "verified-answer=42" in json.dumps(requests[4]["messages"])
        assert len(requests) == 6
        assert sum(u["completion_tokens"] for u in usage) == 60
    finally:
        manager.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
