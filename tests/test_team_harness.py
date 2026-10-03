import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.harness import HarnessSettings, HarnessTurnError
from app.team_harness import TEAM_TOOLS, TeamHarnessAdapter, parse_delivery


class Runtime:
    def __init__(self, **options):
        self.options = options
        self.calls = []
        self.closed = False
        self.response = '{"action":"complete_task","speech":"已确认","result":"42"}'

    def run(self, prompt, *, session_id, on_notification):
        self.calls.append((prompt, session_id))
        payload = {"sessionId": session_id, "event": {"seq": 1, "type": "assistant/message",
                   "data": {"usage": {"inputTokens": 10, "cacheReadTokens": 3, "outputTokens": 4}}}}
        for _ in range(2):
            on_notification(SimpleNamespace(method="session.event", payload=payload))
        return SimpleNamespace(finish_reason="completed", final_response=self.response, events=[])

    def close(self):
        self.closed = True


def adapter(tmp_path, runtime_class=Runtime, **overrides):
    runtimes = []

    def factory(**options):
        item = runtime_class(**options)
        runtimes.append(item)
        return item

    settings = replace(HarnessSettings(root=tmp_path, api_key="provider-secret"), **overrides)
    return TeamHarnessAdapter("team-a", settings, factory=factory), runtimes


def context(**overrides):
    return {"instance_id": "a", "task_id": "task-1", "activation_id": "act-1", "attempt_id": "try-1",
            "system": "独立角色", "tools": [], "output_budget": 50, "history": [], **overrides}


def execute(item, ctx=None, activity_cb=None, usage_cb=None, cancel_event=None):
    return item.execute(ctx or context(), "http://127.0.0.1:3001/command", "activation-secret",
                        activity_cb or (lambda _: None), usage_cb or (lambda _: None),
                        cancel_event or threading.Event())


def test_instance_isolation_usage_and_recovery(tmp_path):
    item, runtimes = adapter(tmp_path)
    usage, activity = [], []
    result = execute(item, activity_cb=activity.append, usage_cb=usage.append)
    assert result["usage"] == {"prompt_tokens": 13, "completion_tokens": 4, "total_tokens": 17}
    assert len(usage) == 1
    assert result["state"]["pending"] is False and activity[0]["state"]["pending"] is True
    assert "activation-secret" not in json.dumps(activity)
    execute(item, context(harness_state=result["state"]))
    assert runtimes[0].calls[0][1] == runtimes[0].calls[1][1]
    execute(item, context(instance_id="b"))
    assert len(runtimes) == 2
    assert runtimes[0].options["cwd"] != runtimes[1].options["cwd"]
    item.close("a")
    assert runtimes[0].closed and not runtimes[1].closed
    execute(item, context(harness_state=result["state"]))
    assert runtimes[2].calls[0][1] != runtimes[0].calls[0][1]
    item.close()
    assert all(runtime.closed for runtime in runtimes)


def test_auxiliary_has_no_tools_and_releases_runtime(tmp_path):
    class Auxiliary(Runtime):
        response = None

        def __init__(self, **options):
            super().__init__(**options)
            self.response = '{"score":0.8}'

    item, runtimes = adapter(tmp_path, Auxiliary, tools=())
    result = item.execute_auxiliary("willingness", context(tools=list(TEAM_TOOLS)))
    assert result["result"] == {"score": 0.8}
    control = json.loads(open(runtimes[0].options["env"]["KDS_CONTROL_FILE"], encoding="utf-8").read())
    assert control["tools"] == []
    assert runtimes[0].closed and not item._runtimes


def test_invalid_final_does_not_make_repair_or_direct_request(tmp_path):
    class Invalid(Runtime):
        def __init__(self, **options):
            super().__init__(**options)
            self.response = "我已经完成"

    item, runtimes = adapter(tmp_path, Invalid)
    with pytest.raises(HarnessTurnError) as error:
        execute(item)
    assert error.value.reason == "format"
    assert len(runtimes) == 1 and len(runtimes[0].calls) == 1
    item.close()


@pytest.mark.parametrize("value", [
    {}, {"action": "complete_task", "wait_for": ["x"]},
    {"action": "wait_children", "wait_for": []},
    {"action": "wait_children", "wait_for": ["x", "x"]},
    {"action": "wait_children", "wait_for": ["x"], "wait_mode": "any"},
    {"action": "wait_children", "wait_for": ["x"], "timeout_seconds": True},
    {"action": "complete_task", "speech": 42},
    {"action": "continue", "whiteboard": {"ops": [{"op": "replace", "find": "", "replace": "x"}]}},
])
def test_invalid_delivery_rejected(value):
    with pytest.raises(HarnessTurnError):
        parse_delivery(json.dumps(value))


def test_delivery_retains_explicit_wait_and_whiteboard():
    result = parse_delivery(json.dumps({"action": "wait_children", "speech": "等待证据",
        "wait": {"task_ids": ["child-b", "child-a"], "mode": "any_success", "timeout_seconds": 30},
        "whiteboard": {"base_rev": 3, "ops": [{"op": "append", "content": "阶段产出"}]}}, ensure_ascii=False))
    assert result["wait_for"] == ["child-b", "child-a"] and result["wait_mode"] == "any_success"
    assert result["whiteboard"]["base_rev"] == 3


@pytest.mark.parametrize("tools,url", [(["subagent"], "http://127.0.0.1:3001/command"),
    (["kds_delegate_task"], "https://example.com/command"),
    (["kds_spawn_subagent"], "http://localhost:3001/command")])
def test_native_subagents_and_nonlocal_channel_rejected(tmp_path, tools, url):
    item, runtimes = adapter(tmp_path)
    with pytest.raises(HarnessTurnError):
        item.execute(context(tools=tools), url, "secret", lambda _: None, lambda _: None, threading.Event())
    assert not runtimes


def test_cancel_during_runtime_reports_settled_usage_and_reaps(tmp_path):
    cancel = threading.Event()

    class Cancelled(Runtime):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            cancel.set()
            return result

    item, runtimes = adapter(tmp_path, Cancelled)
    usage = []
    with pytest.raises(HarnessTurnError) as error:
        execute(item, cancel_event=cancel, usage_cb=usage.append)
    assert error.value.reason == "cancelled"
    assert usage[0]["completion_tokens"] == 4
    assert runtimes[0].closed and not item._runtimes


def test_same_instance_cannot_overlap(tmp_path):
    started, release = threading.Event(), threading.Event()

    class Blocked(Runtime):
        def run(self, *args, **kwargs):
            started.set()
            release.wait(2)
            return super().run(*args, **kwargs)

    item, _ = adapter(tmp_path, Blocked)
    failures = []

    def work():
        try:
            execute(item)
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    assert started.wait(1)
    try:
        with pytest.raises(HarnessTurnError) as error:
            execute(item)
        assert error.value.reason == "busy"
    finally:
        release.set()
        thread.join(3)
        item.close()
    assert not failures


def test_provider_failure_redacts_credentials(tmp_path):
    class Failed(Runtime):
        def run(self, *args, **kwargs):
            return SimpleNamespace(finish_reason="error", events=[{"type": "turn/end", "data": {
                "reason": {"error": {"message": "invalid provider-secret activation-secret"}}}}])

    item, _ = adapter(tmp_path, Failed)
    with pytest.raises(HarnessTurnError) as error:
        execute(item)
    assert "provider-secret" not in str(error.value) and "activation-secret" not in str(error.value)
    item.close()


def test_format_repair_uses_no_tools_shares_budget_and_preserves_intent(tmp_path):
    original = {"action": "wait_children", "speech": "等子任务", "result": "已有内容",
                "wait": {"task_ids": ["child-a"], "mode": "all", "timeout_seconds": 20},
                "whiteboard": {"base_rev": 3, "ops": [{"op": "append", "content": "已有证据"}]}}
    responses = [str(original), json.dumps({**original, "action": "complete_task", "wait": {}}),
                 json.dumps({**original, "speech": "修正器新增正文", "result": "修正器新事实"})]
    runtimes = []

    class Repair(Runtime):
        def __init__(self, **options):
            super().__init__(**options)
            self.response = responses[len(runtimes)]
            # The prior process must be reaped before admitting repair.
            assert all(runtime.closed for runtime in runtimes)
            runtimes.append(self)

    item = TeamHarnessAdapter("repair", HarnessSettings(root=tmp_path, api_key="test",
                              max_steps=4, turn_max_tokens=30), factory=Repair)
    usage = []
    result = execute(item, context(tools=["kds_get_task_results"], output_budget=30), usage_cb=usage.append)
    assert result["action"] == "wait_children" and result["wait_for"] == ["child-a"]
    assert result["speech"] == "等子任务" and result["result"] == "已有内容"
    assert result["whiteboard"] == original["whiteboard"]
    assert result["usage"]["completion_tokens"] == 12 and len(usage) == 3
    assert len(runtimes) == 3 and all(runtime.closed for runtime in runtimes)
    for runtime in runtimes[1:]:
        control = json.loads(open(runtime.options["env"]["KDS_CONTROL_FILE"], encoding="utf-8").read())
        assert control["tools"] == [] and control["output_budget"] <= 26
        assert runtime.options["max_tokens"] <= 26


def test_format_repair_cannot_invent_missing_task_action(tmp_path):
    class MissingAction(Runtime):
        def __init__(self, **options):
            super().__init__(**options)
            self.response = "{'speech': '也许完成'}"

    item, runtimes = adapter(tmp_path, MissingAction)
    with pytest.raises(HarnessTurnError):
        execute(item)
    assert len(runtimes) == 1 and runtimes[0].closed


def test_format_repair_cannot_exceed_original_budget(tmp_path):
    class InvalidSyntax(Runtime):
        def __init__(self, **options):
            super().__init__(**options)
            self.response = "{'action':'complete_task','speech':'已有正文'}"

    item, runtimes = adapter(tmp_path, InvalidSyntax)
    with pytest.raises(HarnessTurnError) as error:
        execute(item, context(output_budget=4))
    assert error.value.reason == "limit"
    assert len(runtimes) == 1 and runtimes[0].closed


@pytest.mark.parametrize("changed", [
    {"tools": ["kds_get_task_results"]},
    {"model_config": {"model": "new-registered-model"}},
])
def test_reused_instance_profile_change_keeps_new_control_authorized(tmp_path, changed):
    class InspectControl(Runtime):
        def run(self, *args, **kwargs):
            control = json.loads(open(self.options["env"]["KDS_CONTROL_FILE"], encoding="utf-8").read())
            assert control["cancel"] is None
            assert control["credential"] == "activation-secret"
            assert control["command_url"] == "http://127.0.0.1:3001/command"
            return super().run(*args, **kwargs)

    item, runtimes = adapter(tmp_path, InspectControl)
    original = execute(item)
    following = execute(item, context(harness_state=original["state"], attempt_id="try-2", **changed))
    assert len(runtimes) == 2 and runtimes[0].closed
    assert not runtimes[1].closed
    assert following["state"]["session_id"] != original["state"]["session_id"]
    item.close()


def test_unlimited_output_does_not_restore_hidden_turn_cap(tmp_path):
    item, runtimes = adapter(tmp_path, turn_max_tokens=1)
    result = execute(item, context(output_budget=None))
    control = json.loads(open(runtimes[0].options["env"]["KDS_CONTROL_FILE"], encoding="utf-8").read())
    assert control["output_budget"] is None
    assert result["usage"]["completion_tokens"] == 4
    item.close()


def test_unlimited_output_format_repair_is_still_time_and_step_bounded(tmp_path):
    responses = ["{'action':'complete_task','speech':'已有正文'}", '{"action":"complete_task","speech":"已有正文"}']
    runtimes = []

    class Repair(Runtime):
        def __init__(self, **options):
            super().__init__(**options)
            self.response = responses[len(runtimes)]
            runtimes.append(self)

    item = TeamHarnessAdapter("repair-unlimited", HarnessSettings(root=tmp_path, api_key="test",
                            turn_max_tokens=1, max_steps=3, repair_max_tokens=128), factory=Repair)
    result = execute(item, context(output_budget=None))
    assert result["speech"] == "已有正文"
    assert result["usage"]["completion_tokens"] == 8
    assert runtimes[1].options["max_tokens"] <= 128
    assert len(runtimes) == 2 and all(runtime.closed for runtime in runtimes)


def test_mock_executor_handles_unlimited_output_for_tasks_and_auxiliary():
    from app.services.team_sessions import MockTeamExecutor
    from types import SimpleNamespace

    executor = MockTeamExecutor()
    events = []
    item = {"_repository": SimpleNamespace(list_entities=lambda *args: []),
            "task": {"id": "task", "goal": "有限工作"}, "instance": {"id": "agent", "name": "测试"},
            "conversation_id": "run", "output_budget": None}
    assert executor.execute(item, usage_cb=events.append)["action"] == "complete_task"
    assert executor.execute_auxiliary("score", item, usage_cb=events.append)["score"] == 50
    assert [event["completion_tokens"] for event in events] == [24, 24]


@pytest.mark.parametrize("configured_effort,runtime_effort,profile,expected", [
    (None, None, {}, "off"), ("low", None, {}, "low"),
    (None, "high", {}, "high"), (None, None, {"reasoning_effort": "max"}, "max"),
    (None, None, {"model": "custom-deployment"}, None),
])
def test_service_auxiliary_dsh_effort_respects_explicit_configuration(tmp_path, monkeypatch, configured_effort, runtime_effort, profile, expected):
    from app import config
    from app.repositories.teams import TeamRepository
    from app.services.team_sessions import MockTeamExecutor, TeamSessionService
    from langgraph.checkpoint.memory import InMemorySaver

    monkeypatch.setattr(config, "DSH_REASONING_EFFORT", configured_effort)
    monkeypatch.setattr(config, "DSH_MODEL", "deepseek-v4-flash")
    monkeypatch.setattr(config, "TEAM_MODEL_CONFIGS", {"default": profile})
    contexts = []

    class Observe(MockTeamExecutor):
        settings = HarnessSettings(model="deepseek-v4-flash", reasoning_effort=runtime_effort)
        def execute_auxiliary(self, kind, context, **kwargs):
            contexts.append((kind, context))
            return super().execute_auxiliary(kind, context, **kwargs)

    service = TeamSessionService(TeamRepository(tmp_path / "auxiliary.db"), Observe(),
                                 autostart=False, checkpointer=InMemorySaver())
    try:
        role = service.create_role({"name": "单角色", "system_prompt": "完成当前任务", "tools": []})
        team = service.create_team({"name": "辅助测试", "nodes": [{"id": "root", "role_id": role["id"],
                                    "role_version": 1}], "edges": []})
        run = service.create_run({"team_id": team["id"], "entry_node_ids": ["root"], "goal": "测试", "request_id": "start",
                                  "limits": {"total_max_tokens": 1000, "summary_max_tokens": 100}})
        assert service.run_auxiliary(run["id"], {"kind": "score", "request_id": "score"})["result"] == {"score": 50}
        finished = service.finalize(run["id"], {"summarize": True})
        assert finished["summary"] and finished["usage"]["completion_tokens"] == 48
        assert [kind for kind, _ in contexts] == ["score", "summary"]
        assert all(context["model_config"].get("reasoning_effort") == expected for _, context in contexts)
        assert all(context["tools"] == [] for _, context in contexts)
    finally:
        service.close()


@pytest.mark.parametrize("case", ["safe", "stale", "unfinished_native", "failed_native", "transport", "cancel", "timeout", "timeout_boundary", "stop_limit",
                                 "rejected_kds", "untrusted_kds", "server_5xx", "stale_rejection", "mismatched_rejection", "native_claims_kds", "unmatched_results"])
def test_pending_child_control_yields_only_after_confirmed_tools(tmp_path, case):
    cancel = threading.Event()
    events = []

    class Yield(Runtime):
        def run(self, prompt, *, session_id, on_notification):
            result = super().run(prompt, session_id=session_id, on_notification=on_notification)
            control_path = self.options["env"]["KDS_CONTROL_FILE"]
            control = json.loads(open(control_path, encoding="utf-8").read())
            query_args = {"request_id": "query-1", "task_ids": ["child"]}
            receipt = {"tasks": [{"id": "child", "parent_task_id": "task-1", "status": "queued"}],
                       "kds_control": {"action": "wait_children", "task_ids": ["child"], "mode": "all"}}
            signal = {"run_id": "old-activation" if case == "stale" else control["run_id"],
                      "task_id": "task-1", "tool": "kds_get_task_results", "request_id": "query-1",
                      "task_ids": ["child"], "mode": "all", "receipt": receipt}
            from pathlib import Path
            Path(control["yield_file"]).write_text(json.dumps(signal), encoding="utf-8")
            Path(control["request_log"]).write_text(json.dumps({"task_id": "task-1", "tool": "kds_get_task_results",
                                                                 "args": query_args}) + "\n", encoding="utf-8")
            def emit(seq, kind, data):
                on_notification(SimpleNamespace(method="session.event", payload={"sessionId": session_id,
                    "event": {"seq": seq, "type": kind, "data": data}}))
            emit(2, "tool/call", {"step": 1, "callId": "query-call", "name": "kds_get_task_results", "arguments": query_args})
            emit(3, "tool/result", {"step": 1, "message": {"toolCallId": "query-call", "source": {"kind": "tool", "callId": "query-call"},
                    "content": [{"type": "text", "text": json.dumps(receipt)}]}})
            if case in {"unfinished_native", "failed_native", "unmatched_results"}:
                emit(4, "tool/call", {"step": 2, "callId": "native-call", "name": "write", "arguments": {"file_path": "artifact.txt"}})
                if case != "unfinished_native":
                    identifier = "different-call" if case == "unmatched_results" else "native-call"
                    emit(5, "tool/result", {"step": 2, "message": {"toolCallId": identifier, "source": {"kind": "tool", "callId": identifier},
                        "isError": case == "failed_native", "content": []}})
            if case in {"rejected_kds", "untrusted_kds", "server_5xx", "stale_rejection", "mismatched_rejection", "native_claims_kds"}:
                tool = "write" if case == "native_claims_kds" else "kds_delegate_task"
                args = {"request_id": "rejected-id", "child_instance_id": "child", "goal": "duplicate"}
                intent = {"task_id": "task-1", "tool": tool, "args": args}
                with open(control["request_log"], "a", encoding="utf-8") as journal:
                    journal.write(json.dumps(intent) + "\n")
                emit(4, "tool/call", {"step": 2, "callId": "rejected-call", "name": tool, "arguments": args})
                emit(5, "tool/result", {"step": 2, "message": {"toolCallId": "rejected-call", "source": {"kind": "tool", "callId": "rejected-call"},
                    "isError": True, "content": [{"type": "text", "text": "Error: KDS：编排命令被拒绝（HTTP 409）"}]}})
                status = 500 if case == "server_5xx" else 409
                proof = {**intent, "run_id": "old-activation" if case == "stale_rejection" else control["run_id"],
                         "call_id": "different-call" if case == "mismatched_rejection" else "rejected-call", "status": status,
                         "rejection": {"kind": "rejected", "accepted": False, "status": status, "tool": tool, "request_id": args["request_id"]}}
                if case != "untrusted_kds":
                    Path(control["rejection_log"]).write_text(json.dumps(proof) + "\n", encoding="utf-8")
            if case == "transport":
                raise ConnectionError("transport did not settle")
            if case == "cancel":
                cancel.set()
            if case in {"timeout", "timeout_boundary"}:
                import time
                time.sleep(.25 if case == "timeout" else .04)
            Path(self.options["env"]["KDS_STOP_FILE"]).write_text(json.dumps({"run_id": control["run_id"],
                                                                                "reason": "limit" if case == "stop_limit" else "wait_children"}), encoding="utf-8")
            result.finish_reason = "completed" if case == "stop_limit" else "blocked"
            return result

    item, runtimes = adapter(tmp_path, Yield, timeout=.1 if case == "timeout" else .03 if case == "timeout_boundary" else 10)
    if case in {"safe", "rejected_kds"}:
        result = execute(item, cancel_event=cancel, usage_cb=events.append)
        assert result["action"] == "wait_children" and result["result"] is None
        assert result["wait"] == {"task_ids": ["child"], "mode": "all"}
        assert result["scheduling_control"] == {"kind": "pending_children_yield", "request_id": "query-1", "task_ids": ["child"]}
        assert result["whiteboard_ops"] == [] and result["usage"]["completion_tokens"] == 4
        assert len(events) == 1
    else:
        with pytest.raises(HarnessTurnError) as error:
            execute(item, cancel_event=cancel, usage_cb=events.append)
        expected_reason = "cancelled" if case == "cancel" else "timeout" if case.startswith("timeout") else "limit" if case == "stop_limit" else "error"
        assert error.value.reason == expected_reason
        assert len(events) == 1 and events[0]["completion_tokens"] == 4
    item.close()
    assert all(runtime.closed for runtime in runtimes)


def test_service_events_are_pages_and_wait_commit_is_in_event_list(tmp_path):
    import time
    from app.repositories.teams import TeamRepository
    from app.services.team_sessions import MockTeamExecutor, TeamSessionService
    from langgraph.checkpoint.memory import InMemorySaver

    service = TeamSessionService(TeamRepository(tmp_path / "events.db"), MockTeamExecutor(),
                                 autostart=True, checkpointer=InMemorySaver())
    try:
        role = service.create_role({"name": "工作角色", "system_prompt": "完成当前任务", "tools": []})
        team = service.create_team({"name": "分页验证", "nodes": [
            {"id": key, "role_id": role["id"], "role_version": 1} for key in ("parent", "child")],
            "edges": [{"id": "edge", "type": "task", "source": "parent", "target": "child"}]})
        run = service.create_run({"team_id": team["id"], "entry_node_ids": ["parent"], "goal": "验证回传",
                                  "request_id": "start", "limits": {"total_max_tokens": 1000, "max_processes": 1}})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = service.get_snapshot(run["id"])
            if len(snapshot["tasks"]) == 2 and all(task["status"] == "succeeded" for task in snapshot["tasks"]):
                break
            time.sleep(.02)
        else:
            pytest.fail("离线父子任务未完成")
        page = service.list_events(run["id"], limit=1)
        assert set(page) == {"events", "event_seq", "next_after"}
        assert len(page["events"]) == 1 and page["next_after"] == page["events"][0]["seq"]
        following = service.list_events(run["id"], after=page["next_after"], limit=1)
        assert following["events"][0]["seq"] > page["next_after"]
        events = service.list_events(run["id"])["events"]
        assert any(event.get("type") == "activation_committed" and event.get("action") == "wait_children" for event in events)
    finally:
        service.close()


@pytest.mark.parametrize("failure", ["rollback", "post_commit_notification", "malformed_request"])
def test_command_channel_attests_only_transactional_rejection(failure):
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from app.repositories.teams import TeamConflict
    from app.services.team_sessions import CommandChannel
    committed = []
    class Repository:
        def command(self, activation_id, tool, args):
            if failure == "rollback":
                raise TeamConflict("任务参数被拒绝")
            committed.append(args["request_id"])
            return {"task_id": "accepted-child"}
    def notify(_):
        if failure == "post_commit_notification":
            raise RuntimeError("notification failed after commit")
    service = SimpleNamespace(repository=Repository(), notify_activation=notify)
    channel = CommandChannel(service)
    try:
        token = channel.bind("activation-test")
        body = {"tool": "kds_delegate_task", "args": [] if failure == "malformed_request" else {"request_id": "same-command"}}
        request = Request(channel.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=3)
        response = json.loads(error.value.read())
        if failure == "rollback":
            assert error.value.code == 409 and not committed
            assert response["kds_command_error"] == {"kind": "rejected", "accepted": False, "status": 409,
                                                      "tool": "kds_delegate_task", "request_id": "same-command"}
        else:
            assert "kds_command_error" not in response
            assert error.value.code == (500 if failure == "post_commit_notification" else 400)
            assert committed == (["same-command"] if failure == "post_commit_notification" else [])
    finally:
        channel.close()
