"""A real DSH parent/child lifecycle through the team service and finite graphs."""
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.harness import HarnessSettings
from app.repositories.teams import TeamRepository
from app.services.team_sessions import TeamSessionService
from app.team_harness import TeamHarnessAdapter


pytestmark = pytest.mark.skipif(os.getenv("KDS_TEST_DSH_RUNTIME") != "1",
                                reason="显式启用本机 DSH 运行时集成测试")


@pytest.mark.parametrize("waiting", ["model_delivery", "server_control", "server_control_queue"])
def test_real_dsh_service_parent_wait_child_return_and_human_summary(tmp_path, waiting):
    requests, stages = [], []
    lifecycle = []
    run_id = None
    service = None
    human_id = None
    queued = waiting == "server_control_queue"
    controlled = waiting.startswith("server_control")
    runtime_sessions, child_inputs = [], []
    assignments = {
        "first": {"goal": "Queued first", "input": {"evidence": "queue-first-42"}},
        "second": {"goal": "Queued second", "input": {"evidence": "queue-second-84"}},
    }
    expected_results = {"Queued first": {"evidence": "queue-first-42", "answer": 42},
                        "Queued second": {"evidence": "queue-second-84", "answer": 84}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            inputs = []
            for message in request["messages"]:
                text = message.get("content")
                if message.get("role") == "user" and isinstance(text, str) and "输入快照" in text:
                    inputs.append(json.JSONDecoder().raw_decode(text.split("\n", 1)[1])[0])
            latest = inputs[-1]
            history = json.loads(latest.get("history", "{}"))
            tools = {tool["function"]["name"] for tool in request.get("tools", [])}
            if latest.get("purpose") == "summary":
                assert not tools
                stage = "summary"
                delta = {"content": "团队总结：子任务交付42，父任务汇总完成。"}
            elif history.get("children") and not history.get("child_results"):
                if controlled:
                    assert any(message.get("content") == "请在子结果回传后保留这条补充要求。" for message in history["messages"])
                previous = sum(stage in {"delegate", "delegate_second", "duplicate_delegate", "wait", "query_yield"} for stage in stages)
                if previous == 0:
                    stage = "delegate"
                    delta = {"tool_calls": [{"index": 0, "id": "delegate-call", "type": "function", "function": {
                        "name": "kds_delegate_task", "arguments": json.dumps({
                            "request_id": "stable-parent-delegate", "child_instance_id": history["children"][0]["id"],
                            "goal": "Confirm child evidence", "input": {"evidence": "42"},
                        })}}]}
                    if queued:
                        delta = {"tool_calls": [{"index": 0, "id": "delegate-first", "type": "function", "function": {
                            "name": "kds_delegate_task", "arguments": json.dumps({
                                "request_id": "stable-parent-delegate-first",
                                "child_instance_id": history["children"][0]["id"], **assignments["first"]})}}]}
                elif queued and previous == 1:
                    stage = "delegate_second"
                    snapshot = service.get_snapshot(run_id)
                    children = [task for task in snapshot["tasks"] if task["parent_task_id"] == history["task"]["id"]]
                    assert len(children) == 1 and children[0]["goal"] == "Queued first"
                    assert children[0]["status"] == "queued" and children[0]["activation_count"] == 0
                    # Establish acceptance order with separate model steps;
                    # parallel tool array order does not define FIFO.
                    delta = {"tool_calls": [{"index": 0, "id": "delegate-second", "type": "function", "function": {
                        "name": "kds_delegate_task", "arguments": json.dumps({
                            "request_id": "stable-parent-delegate-second",
                            "child_instance_id": history["children"][0]["id"], **assignments["second"]})}}]}
                elif queued and previous == 2:
                    stage = "duplicate_delegate"
                    snapshot = service.get_snapshot(run_id)
                    children = [task for task in snapshot["tasks"] if task["parent_task_id"] == history["task"]["id"]]
                    assert len(children) == 2 and all(task["status"] == "queued" and task["activation_count"] == 0 for task in children)
                    # A distinct model tool-call ID retries exactly the first
                    # accepted operation; it must not create a third task.
                    delta = {"tool_calls": [{"index": 0, "id": "delegate-replay", "type": "function", "function": {
                        "name": "kds_delegate_task", "arguments": json.dumps({
                            "request_id": "stable-parent-delegate-first",
                            "child_instance_id": history["children"][0]["id"], **assignments["first"]})}}]}
                else:
                    assert previous == (3 if queued else 1), "待处理子任务的查询必须让位，不能触发额外模型轮询"
                    stage = "query_yield" if controlled else "wait"
                    snapshot = service.get_snapshot(run_id)
                    children = [task for task in snapshot["tasks"] if task["parent_task_id"] == history["task"]["id"]]
                    if queued:
                        assert len(children) == 2 and [task["goal"] for task in children] == ["Queued first", "Queued second"]
                        assert all(task["status"] == "queued" and task["activation_count"] == 0 for task in children)
                    ids = [task["id"] for task in children]
                    delta = {"tool_calls": [{"index": 0, "id": "query-call", "type": "function", "function": {
                        "name": "kds_get_task_results", "arguments": json.dumps({
                            "request_id": "stable-parent-query", "task_ids": ids})}}]} if controlled else {
                                "content": json.dumps({"action": "wait_children", "speech": "等待正式子结果",
                                "wait": {"task_ids": ids, "mode": "all"}}, ensure_ascii=False)}
            elif history.get("child_results"):
                stage = "parent_complete"
                if queued:
                    assert {task["goal"]: task["result"] for task in history["child_results"]} == expected_results
                    assert len(history["receipts"]) == 3
                else:
                    assert history["child_results"][0]["result"] == "42"
                if controlled:
                    assert any(message.get("content") == "请在子结果回传后保留这条补充要求。" for message in history["messages"])
                    with service.repository.reading() as connection:
                        assert connection.execute("SELECT consumed_by FROM mailbox_deliveries WHERE message_id=?", (human_id,)).fetchone()[0] is None
                delta = {"content": json.dumps({"action": "complete_task", "speech": "父任务汇总完成",
                         "result": "parent-queue-result=42,84" if queued else "parent-result=42"}, ensure_ascii=False)}
            else:
                stage = "child_complete"
                child_result = "42"
                if queued:
                    assert len(inputs) == 1  # A new DSH task session, without a previous task prompt.
                    goal, task_id = history["task"]["goal"], history["task"]["id"]
                    name = "first" if goal == "Queued first" else "second"
                    assert goal == assignments[name]["goal"]
                    assert history["task"]["input"] == assignments[name]["input"]
                    assert latest["task"]["id"] == task_id and latest["task"]["input"] == assignments[name]["input"]
                    assert not history["children"] and not history["child_results"] and not history["receipts"]
                    other = "second" if name == "first" else "first"
                    assert assignments[other]["input"]["evidence"] not in json.dumps(request["messages"])
                    child_inputs.append((task_id, goal, history["task"]["input"]))
                    assert [value[1] for value in child_inputs] == ["Queued first", "Queued second"][:len(child_inputs)]
                    child_result = expected_results[goal]
                    stage += ":" + name
                else:
                    assert history["task"]["input"] == {"evidence": "42"}
                delta = {"content": json.dumps({"action": "complete_task", "speech": "子任务证据已核对",
                         "result": child_result}, ensure_ascii=False)}
            stages.append(stage)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for record in [
                {"id": "scripted-team", "object": "chat.completion.chunk", "choices": [{"index": 0,
                    "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                {"id": "scripted-team", "object": "chat.completion.chunk", "choices": [{"index": 0,
                    "delta": {}, "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40,
                              "prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 20}},
            ]:
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
    from deepseek_harness import DeepSeekHarness

    def factory(**options):
        assert all(runtime.client._proc is None or runtime.client._proc.poll() is not None for runtime in lifecycle)
        runtime = DeepSeekHarness(**options)
        if queued:
            original_run = runtime.run
            def audited_run(prompt, *, session_id, on_notification):
                control = json.loads(Path(options["env"]["KDS_CONTROL_FILE"]).read_text(encoding="utf-8"))
                record = {"task_id": control["task_id"], "session_id": session_id,
                          "control_path": options["env"]["KDS_CONTROL_FILE"], "observed_sessions": set()}
                runtime_sessions.append(record)
                def observe(notification):
                    if notification.method == "session.event":
                        record["observed_sessions"].add(notification.payload["sessionId"])
                    on_notification(notification)
                return original_run(prompt, session_id=session_id, on_notification=observe)
            runtime.run = audited_run
        lifecycle.append(runtime)
        return runtime

    adapter = TeamHarnessAdapter("service", HarnessSettings(root=tmp_path / "dsh", api_key="local-test-key",
        base_url=f"http://127.0.0.1:{server.server_port}/v1", tools=(), dsh_bin=None, timeout=45 if queued else 30), factory=factory)
    service = TeamSessionService(TeamRepository(tmp_path / "team.db"), adapter, autostart=False,
                                 checkpointer=InMemorySaver())
    try:
        role = service.create_role({"name": "Worker", "system_prompt": "独立完成本次任务。", "tools": []})
        team = service.create_team({"name": "Real DSH tree", "nodes": [
            {"id": "parent", "role_id": role["id"], "role_version": 1},
            {"id": "child", "role_id": role["id"], "role_version": 1}],
            "edges": [{"id": "delegate", "type": "task", "source": "parent", "target": "child"}]})
        run = service.create_run({"team_id": team["id"], "team_version": 1, "entry_node_ids": ["parent"],
            "goal": "Complete tree integration", "request_id": "start-real-tree", "limits": {
                "total_max_tokens": 2000, "single_max_tokens": 200, "summary_max_tokens": 100,
                "max_concurrency": 1, "max_processes": 1}})
        run_id = run["id"]
        if controlled:
            parent_instance = next(instance for instance in run["agents"] if instance.get("node_id") == "parent")
            human_id = service.send_human_message(run_id, {"request_id": "frozen-yield-mail", "target_id": parent_instance["id"],
                "content": "请在子结果回传后保留这条补充要求。"})["id"]
        service.autostart = True
        service.start(run_id)
        deadline = time.monotonic() + (150 if queued else 90)
        while time.monotonic() < deadline:
            snapshot = service.get_snapshot(run_id)
            if all(task["status"] == "succeeded" for task in snapshot["tasks"]):
                break
            if snapshot["status"] == "paused":
                pytest.fail("团队意外暂停：" + json.dumps(snapshot, ensure_ascii=False))
            time.sleep(0.05)
        else:
            pytest.fail("团队执行未在期限内完成：" + json.dumps(snapshot, ensure_ascii=False))
        assert len(snapshot["tasks"]) == (3 if queued else 2)
        assert stages == (["delegate", "delegate_second", "duplicate_delegate", "query_yield", "child_complete:first", "child_complete:second", "parent_complete"]
                          if queued else ["delegate", "query_yield" if controlled else "wait", "child_complete", "parent_complete"])
        assert snapshot["status"] != "completed"
        assert snapshot["usage"]["completion_tokens"] == (70 if queued else 40)
        assert snapshot["usage"]["reserved_tokens"] == 0
        assert len(lifecycle) == (4 if queued else 3) and not adapter._runtimes
        if queued:
            children = [task for task in snapshot["tasks"] if task["parent_task_id"]]
            assert len({task["instance_id"] for task in children}) == 1
            assert [(task["id"], task["goal"], task["input"]) for task in children] == child_inputs
            assert {task["goal"]: task["result"] for task in children} == expected_results
            sessions = [record for record in runtime_sessions if record["task_id"] in {task["id"] for task in children}]
            assert [record["task_id"] for record in sessions] == [task["id"] for task in children]
            assert len({record["session_id"] for record in sessions}) == 2
            assert len({record["control_path"] for record in sessions}) == 1  # Same instance, different task sessions.
            assert all(record["observed_sessions"] == {record["session_id"]} for record in runtime_sessions)
            assert snapshot["tasks"][0]["result"] == "parent-queue-result=42,84"
        if controlled:
            with service.repository.reading() as connection:
                delivery = connection.execute("SELECT consumed_by FROM mailbox_deliveries WHERE message_id=?", (human_id,)).fetchone()
                assert delivery[0] is not None
                assert connection.execute("SELECT count(*) FROM mailbox_deliveries WHERE message_id=?", (human_id,)).fetchone()[0] == 1
        finalized = service.finalize(run_id, {"summarize": True})
        assert finalized["status"] == "completed"
        assert finalized["usage"]["completion_tokens"] == (80 if queued else 50)
        assert stages[-1] == "summary" and len(lifecycle) == (5 if queued else 4)
        assert all(runtime.client._proc is None for runtime in lifecycle)
        with service.repository.reading() as connection:
            events = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM team_events WHERE run_id=?", (run_id,))]
        if queued:
            assert all(sum(event.get("request_id") == "stable-parent-delegate-" + name for event in events) == 1 for name in assignments)
            assert len({record["session_id"] for record in runtime_sessions}) == 5
            with service.repository.reading() as connection:
                receipts = connection.execute("SELECT request_id FROM team_receipts WHERE scope LIKE ?", (run_id + ":task:%",)).fetchall()
            assert {row["request_id"] for row in receipts} == {"stable-parent-delegate-first", "stable-parent-delegate-second", "stable-parent-query"}
        else:
            assert sum(event.get("request_id") == "stable-parent-delegate" for event in events) == 1
    finally:
        service.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_real_dsh_three_level_dynamic_roles_spawn_receipts_and_process_limit(tmp_path):
    """Two levels of runtime role design, duplicate accepted commands, one slot."""
    requests, stages, lifecycle = [], [], []
    calls_by_task = {}
    spawn_arguments = {}
    service = None
    run_id = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            inputs = []
            for message in request["messages"]:
                text = message.get("content")
                if message.get("role") == "user" and isinstance(text, str) and "输入快照" in text:
                    inputs.append(json.JSONDecoder().raw_decode(text.split("\n", 1)[1])[0])
            history = json.loads(inputs[-1]["history"])
            task_id, goal = history["task"]["id"], history["task"]["goal"]
            names = {tool["function"]["name"] for tool in request.get("tools", [])}
            assert names == {"kds_delegate_task", "kds_spawn_subagent", "kds_get_task_results",
                "kds_send_group_message", "kds_request_discussion", "kds_cancel_task", "kds_retry_task"}
            index = calls_by_task.get(task_id, 0)
            calls_by_task[task_id] = index + 1
            if goal in {"dynamic-root", "dynamic-middle"} and not history["child_results"]:
                if index < 2:
                    if task_id not in spawn_arguments:
                        child_goal = "dynamic-middle" if goal == "dynamic-root" else "dynamic-leaf"
                        budget = {"single_max_tokens": 150 if goal == "dynamic-root" else 120,
                                  "total_max_tokens": 500 if goal == "dynamic-root" else 200,
                                  "max_children": 1, "max_depth": 3}
                        spawn_arguments[task_id] = {
                            "request_id": "stable-spawn:" + task_id,
                            "name": "临时实例：" + child_goal,
                            "role": {"name": "临时角色：" + child_goal,
                                     "system_prompt": "你是自主设计的证据校验角色 " + child_goal + "；仅按任务与权限工作。",
                                     "tools": [], "model_config_id": "default"},
                            "task": {"goal": child_goal, "input": {"evidence": "42"}},
                            "budget": budget, "reason": "父任务需要独立证据核对",
                        }
                    args = spawn_arguments[task_id]
                    stages.append(goal + (":spawn" if index == 0 else ":duplicate_spawn"))
                    delta = {"tool_calls": [{"index": 0, "id": "spawn-" + str(index), "type": "function",
                        "function": {"name": "kds_spawn_subagent", "arguments": json.dumps(args, ensure_ascii=False)}}]}
                else:
                    assert index == 2
                    snapshot = service.get_snapshot(run_id)
                    child = next(task for task in snapshot["tasks"] if task["parent_task_id"] == task_id)
                    stages.append(goal + ":wait")
                    delta = {"content": json.dumps({"action": "wait_children", "speech": "等待动态子任务",
                        "wait": {"task_ids": [child["id"]], "mode": "all"}}, ensure_ascii=False)}
            else:
                if goal == "dynamic-leaf":
                    assert history["task"]["input"] == {"evidence": "42"}
                else:
                    assert len(history["child_results"]) == 1
                    assert history["child_results"][0]["result"] == "42"
                    assert len(history["receipts"]) == 1
                stages.append(goal + ":complete")
                delta = {"content": json.dumps({"action": "complete_task", "speech": "动态任务已完成",
                                                "result": "42"}, ensure_ascii=False)}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for record in [
                {"id": "dynamic-team", "object": "chat.completion.chunk", "choices": [{"index": 0,
                    "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                {"id": "dynamic-team", "object": "chat.completion.chunk", "choices": [{"index": 0,
                    "delta": {}, "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40,
                              "prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 20}},
            ]:
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
    from deepseek_harness import DeepSeekHarness

    def factory(**options):
        assert all(runtime.client._proc is None or runtime.client._proc.poll() is not None for runtime in lifecycle)
        runtime = DeepSeekHarness(**options)
        lifecycle.append(runtime)
        return runtime

    adapter = TeamHarnessAdapter("dynamic-service", HarnessSettings(root=tmp_path / "dsh", api_key="local-test-key",
        base_url=f"http://127.0.0.1:{server.server_port}/v1", tools=(), dsh_bin=None, timeout=30), factory=factory)
    service = TeamSessionService(TeamRepository(tmp_path / "dynamic-team.db"), adapter, autostart=False,
                                 checkpointer=InMemorySaver())
    try:
        role = service.create_role({"name": "Original planner", "system_prompt": "按任务自主定义临时子角色。",
            "tools": [], "default_budget": {"single_max_tokens": 200, "total_max_tokens": 1000,
                                               "max_children": 1, "max_depth": 3}})
        original_roles = service.list_roles()
        team = service.create_team({"name": "Dynamic DSH tree", "nodes": [
            {"id": "parent", "role_id": role["id"], "role_version": 1}], "edges": []})
        run = service.create_run({"team_id": team["id"], "team_version": 1, "entry_node_ids": ["parent"],
            "goal": "dynamic-root", "request_id": "start-dynamic-tree", "limits": {
                "total_max_tokens": 2000, "single_max_tokens": 200, "summary_max_tokens": 100,
                "max_concurrency": 1, "max_processes": 1, "max_children": 1, "max_depth": 3}})
        run_id = run["id"]
        service.autostart = True
        service.start(run_id)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            snapshot = service.get_snapshot(run_id)
            if len(snapshot["tasks"]) == 3 and all(task["status"] == "succeeded" for task in snapshot["tasks"]):
                break
            if snapshot["status"] == "paused":
                pytest.fail("动态团队意外暂停：" + json.dumps(snapshot, ensure_ascii=False))
            time.sleep(0.05)
        else:
            pytest.fail("动态团队未在期限内完成：" + json.dumps(snapshot, ensure_ascii=False))
        assert stages == ["dynamic-root:spawn", "dynamic-root:duplicate_spawn", "dynamic-root:wait",
            "dynamic-middle:spawn", "dynamic-middle:duplicate_spawn", "dynamic-middle:wait",
            "dynamic-leaf:complete", "dynamic-middle:complete", "dynamic-root:complete"]
        assert len(snapshot["agents"]) == 3 and len(snapshot["tasks"]) == 3
        assert snapshot["status"] != "completed"
        assert snapshot["usage"]["completion_tokens"] == 90 and snapshot["usage"]["reserved_tokens"] == 0
        assert len(lifecycle) == 5 and not adapter._runtimes
        assert all(runtime.client._proc is None for runtime in lifecycle)
        assert service.list_roles() == original_roles
        assert len(snapshot["definition"]["nodes"]) == 1
        dynamic = [instance for instance in snapshot["agents"] if instance["role"].get("temporary")]
        assert len(dynamic) == 2
        for instance in dynamic:
            assert "自主设计的证据校验角色" in instance["role"]["system_prompt"]
            assert instance["role"]["tools"] == [] and instance["role"]["model_config_id"] == "default"
            assert not instance.get("node_id")
        by_goal = {task["goal"]: task for task in snapshot["tasks"]}
        assert by_goal["dynamic-middle"]["parent_task_id"] == by_goal["dynamic-root"]["id"]
        assert by_goal["dynamic-leaf"]["parent_task_id"] == by_goal["dynamic-middle"]["id"]
        assert by_goal["dynamic-middle"]["budget"]["single_max_tokens"] == 150
        assert by_goal["dynamic-leaf"]["budget"]["single_max_tokens"] == 120
        with service.repository.reading() as connection:
            receipts = connection.execute("SELECT request_id,result FROM team_receipts WHERE scope LIKE ?",
                (run_id + ":task:%",)).fetchall()
            spawned = connection.execute("SELECT count(*) FROM team_events WHERE run_id=? AND type='agent_spawned'",
                (run_id,)).fetchone()[0]
        assert len(receipts) == 2 and spawned == 2
        assert {row["request_id"] for row in receipts} == {args["request_id"] for args in spawn_arguments.values()}
        assert len({json.loads(row["result"])["instance_id"] for row in receipts}) == 2
    finally:
        service.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
