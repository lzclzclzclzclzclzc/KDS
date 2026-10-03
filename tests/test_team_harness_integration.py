"""Pinned DSH runtime + local scripted provider, never a paid API call."""
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.harness import HarnessSettings, HarnessTurnError
from app.team_harness import TeamHarnessAdapter


pytestmark = pytest.mark.skipif(os.getenv("KDS_TEST_DSH_RUNTIME") != "1",
                                reason="显式启用本机 DSH 运行时集成测试")


@pytest.mark.parametrize("mode", ["custom", "no_tools", "auxiliary", "repair", "cancel", "yield", "yield_rejected"])
def test_pinned_team_runtime_registration_and_no_tools(tmp_path, mode):
    requests, commands = [], []
    cancel = threading.Event()
    service, run_id = None, None
    child_instances = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/command":
                commands.append(value)
                assert self.headers["Authorization"] == "Bearer activation-secret"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {"tasks": [{"id": "child-1", "parent_task_id": "task-1", "status": "queued"}],
                            "kds_control": {"action": "wait_children", "task_ids": ["child-1"], "mode": "all"}} if mode == "yield" else {
                                "tasks": [{"task_id": "child-1", "status": "succeeded", "result": "42"}]}
                self.wfile.write(json.dumps(response).encode())
                return
            assert self.path == "/v1/chat/completions"
            requests.append(value)
            first_tool = mode in {"custom", "cancel", "yield"} and len(requests) == 1 or mode == "yield_rejected" and len(requests) <= 3
            final = {"answer": 42} if mode == "auxiliary" else {
                "action": "complete_task", "speech": "已确认", "result": "42",
            }
            final_text = str(final) if mode == "repair" and len(requests) == 1 else json.dumps(final, ensure_ascii=False)
            delta = {"tool_calls": [{"index": 0, "id": "lookup-1", "type": "function", "function": {
                "name": "kds_get_task_results", "arguments": json.dumps({
                    "request_id": "lookup-stable", "task_ids": ["child-1"],
                })}}]} if first_tool else {"content": final_text}
            if mode == "yield_rejected":
                assert len(requests) <= 3, "已确认拒绝后让位不得继续模型轮询"
                if len(requests) == 1:
                    calls = [{"index": index, "id": "delegate-" + str(index), "type": "function", "function": {
                        "name": "kds_delegate_task", "arguments": json.dumps({"request_id": "dispatch-" + str(index),
                            "child_instance_id": child_instances[index], "goal": "same comparison"})}} for index in range(3)]
                elif len(requests) == 2:
                    calls = [{"index": 0, "id": "busy-1", "type": "function", "function": {
                        "name": "kds_delegate_task", "arguments": json.dumps({"request_id": "noop-placeholder",
                            "child_instance_id": child_instances[0], "goal": "fourth beyond max_children"})}}]
                else:
                    child_tasks = [task["id"] for task in service.get_snapshot(run_id)["tasks"] if task["parent_task_id"]]
                    calls = [{"index": 0, "id": "lookup-1", "type": "function", "function": {
                        "name": "kds_get_task_results", "arguments": json.dumps({"request_id": "lookup-stable",
                            "task_ids": child_tasks})}}]
                delta = {"tool_calls": calls}
            if mode == "cancel":
                cancel.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            records = [
                {"id": "local-team", "object": "chat.completion.chunk", "choices": [{"index": 0,
                    "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                {"id": "local-team", "object": "chat.completion.chunk", "choices": [{"index": 0,
                    "delta": {}, "finish_reason": "tool_calls" if first_tool else "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40,
                              "prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 20}},
            ]
            for record in records:
                self.wfile.write(("data: " + json.dumps(record) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    for _ in range(30):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 49152 + secrets.randbelow(16383)), Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("无法找到本机测试端口")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = HarnessSettings(root=tmp_path, api_key="local-test-key", dsh_bin=None,
        base_url=f"http://127.0.0.1:{server.server_port}/v1", timeout=90 if mode in {"repair", "yield_rejected"} else 30, tools=())
    adapter = TeamHarnessAdapter("integration", settings)
    usage, activity = [], []
    context = {"instance_id": "parent", "task_id": "task-1", "activation_id": "act-1",
               "attempt_id": "attempt-1", "system": "你是团队测试角色 {{literal}}。",
               "history": [{"role": "user", "content": "请确认结果"}], "output_budget": 1000,
               "tools": ["kds_delegate_task", "kds_get_task_results"] if mode == "yield_rejected" else ["kds_get_task_results"] if mode in {"custom", "cancel", "yield"} else []}
    command_url = f"http://127.0.0.1:{server.server_port}/command"
    credential = "activation-secret"
    observe, record_usage = activity.append, usage.append
    try:
        if mode == "yield_rejected":
            from langgraph.checkpoint.memory import InMemorySaver
            from app.repositories.teams import TeamRepository
            from app.services.team_sessions import TeamSessionService
            service = TeamSessionService(TeamRepository(tmp_path / "team.db"), adapter,
                                         autostart=False, checkpointer=InMemorySaver())
            role = service.create_role({"name": "Worker", "system_prompt": "测试已接单任务的有界等待。", "tools": []})
            nodes = ("parent", "a", "b", "c")
            team = service.create_team({"name": "Three calls and definite rejection", "nodes": [
                {"id": node, "role_id": role["id"], "role_version": 1} for node in nodes], "edges": [
                {"id": "edge-" + node, "type": "task", "source": "parent", "target": node} for node in nodes[1:]]})
            run = service.create_run({"team_id": team["id"], "entry_node_ids": ["parent"], "request_id": "start",
                "goal": "Verify bounded delegation", "limits": {"max_children": 3, "max_processes": 1,
                "max_concurrency": 1, "total_max_tokens": 2000, "summary_max_tokens": 100, "single_max_tokens": 500}})
            run_id = run["id"]
            child_instances = [row["id"] for row in run["agents"] if row["parent_id"]]
            activation_id = service.repository.dispatch(run_id)[0]
            attempt = service.repository.start_attempt(activation_id)
            context = service.repository.context(activation_id)
            context.update(attempt_id=attempt["attempt_id"], model_config={}, tools=["kds_delegate_task", "kds_get_task_results"])
            channel = service.get_channel()
            command_url, credential = channel.url, channel.bind(activation_id)
            def observe(event):
                activity.append(event)
                service.repository.record_activity(activation_id, event)
            def record_usage(event):
                usage.append(event)
                service.repository.record_usage(activation_id, attempt["attempt_id"], event)
        args = (context, command_url, credential, observe, record_usage, cancel)
        if mode == "cancel":
            with pytest.raises(HarnessTurnError) as error:
                adapter.execute(*args)
            assert error.value.reason == "cancelled"
            return
        result = adapter.execute_auxiliary("score", *args) if mode == "auxiliary" else adapter.execute(*args)
        assert result["state"]["pending"] is False
        assert result["usage"]["completion_tokens"] == sum(item["completion_tokens"] for item in usage)
        if mode in {"yield", "yield_rejected"}:
            assert result["action"] == "wait_children" and result["result"] is None
            ids = [task["id"] for task in service.get_snapshot(run_id)["tasks"] if task["parent_task_id"]] if mode == "yield_rejected" else ["child-1"]
            assert result["wait"] == {"task_ids": ids, "mode": "all"}
            assert len(requests) == (3 if mode == "yield_rejected" else 1)
            assert len(commands) == (0 if mode == "yield_rejected" else 1)  # This mode uses the real authenticated CommandChannel.
            if mode == "yield_rejected":
                from app.tool_logs import merge_tool_log
                merged = {}
                for row in activity:
                    if "tool_log" in row:
                        log = row["tool_log"]
                        merged[log["id"]] = merge_tool_log(merged.get(log["id"], {}), log)
                assert len(merged) == 5  # Same names/arguments do not hide different calls.
                assert [row["status"] for row in merged.values()] == ["completed", "completed", "completed", "error", "completed"]
                assert all(row["session_id"] == result["state"]["session_id"] for row in merged.values())
                assert result["usage"]["completion_tokens"] == 30
                service.repository.save_result(activation_id, attempt["attempt_id"], result)
                service.repository.commit(activation_id, service.repository.validate_result(activation_id, result))
                snapshot = service.get_snapshot(run_id)
                assert len(snapshot["tasks"]) == 4 and snapshot["tasks"][0]["status"] == "waiting_children"
                assert snapshot["usage"]["completion_tokens"] == 30 and snapshot["usage"]["reserved_tokens"] == 0
                assert len(snapshot["tool_logs"]) == 5
                assert sum(event["type"] == "command_accepted" for event in service.list_events(run_id)["events"]) == 4
            return
        assert result["result"] == ({"answer": 42} if mode == "auxiliary" else "42")
        names = {tool["function"]["name"] for tool in requests[0].get("tools", [])}
        assert names == ({"kds_get_task_results"} if mode == "custom" else set())
        assert "activation-secret" not in json.dumps(requests)
        assert "{{literal}}" in json.dumps(requests[0])
        if mode == "custom":
            assert len(commands) == 1 and len(requests) == 2
            assert commands[0] == {"tool": "kds_get_task_results", "args": {
                "request_id": "lookup-stable", "task_ids": ["child-1"]}}
            assert "42" in json.dumps(requests[1]["messages"])
            logs = [row["tool_log"] for row in activity if "tool_log" in row]
            assert any(row["status"] == "completed" for row in logs)
        elif mode == "repair":
            assert len(requests) == 2 and not commands
            assert not requests[1].get("tools")
            assert requests[1]["max_tokens"] <= context["output_budget"] - 10
            assert any(row.get("format_repairs") == 1 for row in activity)
            assert not adapter._runtimes
        else:
            assert len(requests) == 1 and not commands
    finally:
        processes = [runtime.client._proc for runtime in adapter._runtimes.values() if runtime.client._proc]
        adapter.close()
        if service is not None:
            service.close()
        assert not adapter._runtimes
        assert all(process.poll() is not None for process in processes)
        server.shutdown()
        server.server_close()
        thread.join(2)
