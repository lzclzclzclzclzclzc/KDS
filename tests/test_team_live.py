"""Explicitly opt-in billable REST/service/DSH checks with isolated data.

KDS_TEST_DEEPSEEK_LIVE=1 python -m pytest tests/test_team_live.py -q -s
The test never loads saved discussions, modifies personal DSH profiles, or prints
credentials. It makes a finite mixed tree and uses a hard session token budget.
"""
import json
import os
import sqlite3
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from flask import Flask
from langgraph.checkpoint.memory import InMemorySaver

from app import config
from app.harness import HarnessSettings
from app.repositories.teams import TeamRepository
from app.routes.team_api import team_api_bp
from app.services.team_sessions import TeamSessionService
from app.team_harness import TeamHarnessAdapter


pytestmark = pytest.mark.skipif(os.getenv("KDS_TEST_DEEPSEEK_LIVE") != "1",
                                reason="真实 DSH API 会消耗额度，需显式启用")


def test_live_rest_mixed_tree_native_tools_and_system_auxiliary(tmp_path):
    marker = "live-" + uuid.uuid4().hex[:10]
    read_marker, write_marker = marker + "-READ", marker + "-WRITE"
    settings = replace(HarnessSettings(), root=tmp_path / "dsh", request_max_tokens=2048,
                       turn_max_tokens=18000, max_steps=8, max_tool_calls=8, timeout=120,
                       repair_attempts=1, repair_max_tokens=1024, tools=("read", "write"))
    settings.validate()
    lifecycle = []
    from deepseek_harness import DeepSeekHarness

    def factory(**options):
        control = json.loads(Path(options["env"]["KDS_CONTROL_FILE"]).read_text(encoding="utf-8"))
        if "STATIC-READER" in control["system"]:
            (Path(options["cwd"]) / "evidence.txt").write_text(read_marker, encoding="utf-8")
        runtime = DeepSeekHarness(**options)
        lifecycle.append(runtime)
        return runtime

    adapter = TeamHarnessAdapter("live-rest-" + marker, settings, factory=factory)
    repository = TeamRepository(tmp_path / "live-team.db")
    service = TeamSessionService(repository, adapter, autostart=True, checkpointer=InMemorySaver())
    app = Flask(__name__)
    app.config.update(TESTING=True, JSON_AS_ASCII=False)
    app.extensions["team_service"] = service
    app.register_blueprint(team_api_bp)
    browser = app.test_client()
    run_id = None
    report = {"case": "mixed_tree_real_rest_dsh", "model": settings.model,
              "output_budget": 18000, "runtime": "configured CLI" if settings.dsh_bin else "pinned SDK",
              "cost_reported": None}

    def request(method, url, body=None):
        response = getattr(browser, method)(url, json=body) if body is not None else getattr(browser, method)(url)
        value = response.get_json()
        assert 200 <= response.status_code < 300, {"status": response.status_code, "response": value}
        return value

    try:
        # Parent grants both native capabilities so a dynamic writer can inherit
        # write, while its static reader receives only read. Both 0 and null mean
        # no single-activation cap; their task/session totals remain finite.
        writer_prompt = (
            "DYNAMIC-WRITER：这是有限的真实工具验证。必须调用 write 在自己的当前工作目录创建 artifact.txt，"
            "文件正文必须恰好为 task.input.marker。不要调用其他工具，不派发子任务，不讨论。"
            "写入成功后交付 JSON {\"action\":\"complete_task\",\"speech\":\"已写入\",\"result\":\"task.input.marker的原文\"}。"
        )
        parent_prompt = (
            "PARENT：这是有限的团队编排连通性检查。不要自己读写文件，不要调查其他内容。"
            "第一次激活请严格执行：1. 从输入 history.children 找到名字 reader 的直接子实例，"
            "调用 kds_delegate_task，request_id=live-static，goal=读取证据，input={marker:'" + read_marker + "'}，"
            "budget={total_max_tokens:4000}。2. 调用 kds_spawn_subagent，request_id=live-dynamic，"
            "临时 role={name:'临时写入校验',system_prompt:" + json.dumps(writer_prompt, ensure_ascii=False) + ",tools:['write']},"
            "task={goal:'写入证据',input:{marker:'" + write_marker + "'}},budget={total_max_tokens:4000}。"
            "3. 取得两个工具回执 task_id 后交付 action=wait_children，speech='已派发两个子任务'，"
            "wait={task_ids:[两个回执task_id],mode:'all'}，本次立即结束，不轮询结果。"
            "后续激活只检查 history.child_results，两个子任务都成功后交付 action=complete_task，"
            "speech='两个子任务已完成'，result包含两个子结果的完整标识。"
            "恢复时已有 receipts 不得重复创建任务。最终输出严格 JSON，不加代码围栏。"
        )
        parent = request("post", "/api/roles", {"name": "parent", "system_prompt": parent_prompt,
            "tools": ["read", "write"], "default_budget": {"single_max_tokens": 0, "total_max_tokens": 12000}})
        reader = request("post", "/api/roles", {"name": "reader", "system_prompt": (
            "STATIC-READER：这是简短的真实工具连通性测试。必须调用 read 读取当前工作目录 evidence.txt。"
            "只读这个文件，不要调用其他工具，不派任务、不讨论。读取成功后只交付严格 JSON："
            "{\"action\":\"complete_task\",\"speech\":\"已读取\",\"result\":\"文件中完整标识原文\"}。"
        ), "tools": ["read"], "default_budget": {"single_max_tokens": None, "total_max_tokens": 4000}})
        assert parent["default_budget"]["single_max_tokens"] is None
        assert reader["default_budget"]["single_max_tokens"] is None
        role_ids_before = {role["id"] for role in request("get", "/api/roles")}
        team = request("post", "/api/teams", {"name": "真实混合任务树", "nodes": [
            {"id": "parent", "name": "parent", "role_id": parent["id"], "role_version": 1},
            {"id": "reader", "name": "reader", "role_id": reader["id"], "role_version": 1}],
            "edges": [{"id": "parent-reader", "type": "task", "source": "parent", "target": "reader"}]})
        run = request("post", "/api/team-runs", {"team_id": team["id"], "team_version": 1,
            "entry_node_ids": ["parent"], "goal": "验证静态读取与动态写入，正式结果回传父任务",
            "request_id": "start-" + marker, "limits": {"single_max_tokens": None,
                "total_max_tokens": 18000, "total_duration_seconds": 240,
                "summary_max_tokens": 1024, "max_concurrency": 2, "max_processes": 2,
                "max_instances": 4, "max_tasks": 6, "max_depth": 3, "max_children": 2,
                "max_activations_per_task": 4}})
        run_id = run["id"]
        assert run["limits"]["single_max_tokens"] is None
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            snapshot = request("get", "/api/team-runs/" + run_id)
            if len(snapshot["tasks"]) == 3 and all(task["status"] == "succeeded" for task in snapshot["tasks"]):
                break
            if snapshot["status"] == "paused":
                errors = {task["goal"]: task.get("error") for task in snapshot["tasks"] if task.get("error")}
                report["failure"] = {"reason": snapshot.get("paused_reason"), "errors": errors}
                pytest.fail("真实团队已暂停：" + json.dumps({"reason": snapshot.get("paused_reason"), "errors": errors}, ensure_ascii=False))
            time.sleep(0.25)
        else:
            pytest.fail("真实团队未在240秒期限内完成")
        tasks = {task["goal"]: task for task in snapshot["tasks"]}
        root_task = next(task for task in snapshot["tasks"] if task["parent_task_id"] is None)
        assert read_marker in tasks["读取证据"]["result"]
        assert write_marker in tasks["写入证据"]["result"]
        parent_result = json.dumps(root_task["result"], ensure_ascii=False)
        assert read_marker in parent_result and write_marker in parent_result
        assert snapshot["status"] != "completed"
        assert len(snapshot["agents"]) == 3
        dynamic = next(instance for instance in snapshot["agents"] if instance["role"].get("temporary"))
        assert dynamic["role"]["tools"] == ["write"]
        assert {role["id"] for role in request("get", "/api/roles")} == role_ids_before
        logs = service.list_tool_logs(run_id)
        assert any(log.get("tool") == "read" and log.get("status") == "completed" for log in logs)
        assert any(log.get("tool") == "write" and log.get("status") == "completed" for log in logs)
        artifacts = list(adapter.root.glob("*/workspace/artifact.txt"))
        assert len(artifacts) == 1 and artifacts[0].read_text(encoding="utf-8").strip() == write_marker
        assert snapshot["usage"]["completion_tokens"] < 18000
        assert snapshot["usage"]["reserved_tokens"] == 0
        report.update(agents=3, tasks=3, native_tools=["read", "write"],
                      single_turn_inputs=[0, None], business_status="passed", auxiliary=[])
        for kind, arguments in (("score", {"instruction": "连通性检查，输出score=42"}),
                                 ("vote", {"question": "选择第一个选项完成连通性检查", "options": ["PASS", "FAIL"]})):
            value = request("post", "/api/team-runs/" + run_id + "/auxiliary", {
                "kind": kind, "arguments": arguments, "request_id": "aux-" + kind + "-" + marker})["result"]
            assert isinstance(value, dict) and ("score" if kind == "score" else "choice") in value
            report["auxiliary"].append(kind)
        finished = request("post", "/api/team-runs/" + run_id + "/finalize", {"summarize": True})
        report["summary_error"] = finished.get("summary_error")
        assert finished["status"] == "completed"
        assert finished["summary"] and not finished.get("summary_error")
        assert finished["usage"]["completion_tokens"] <= 18000
        assert all(runtime.client._proc is None for runtime in lifecycle)
        report.update(status="passed", agents=3, tasks=3, activations=len(lifecycle),
                      input_tokens=finished["usage"]["prompt_tokens"], output_tokens=finished["usage"]["completion_tokens"],
                      native_tools=["read", "write"], auxiliary=["score", "vote", "summary"],
                      single_turn_inputs=[0, None], reserved_tokens=finished["usage"]["reserved_tokens"])
    finally:
        if run_id:
            current = service.get_snapshot(run_id)
            if current:
                report.setdefault("input_tokens", current["usage"]["prompt_tokens"])
                report.setdefault("output_tokens", current["usage"]["completion_tokens"])
                report["run_status"] = current["status"]
        report.setdefault("status", "failed")
        service.close()
        (tmp_path / "live-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


@pytest.mark.skipif(not os.getenv("KDS_TEAM_LIVE_CAME_SNAPSHOT"),
                    reason="仅对已保留的隔离 CAMEL 实测快照验证辅助修复，不重放业务")
def test_live_rest_finalize_saved_camel_snapshot_with_off_summary(tmp_path):
    from deepseek_harness import DeepSeekHarness

    source = Path(os.environ["KDS_TEAM_LIVE_CAME_SNAPSHOT"]).resolve()
    assert source.is_relative_to(Path("data").resolve()) and "team-live-camel-" in str(source)
    original = next(source.rglob("*.db"))
    copied = tmp_path / "saved-camel.db"
    with sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as before_db:
        with sqlite3.connect(copied) as after_db:
            before_db.backup(after_db)
    settings = replace(HarnessSettings(), root=tmp_path / "dsh", request_max_tokens=1024,
                       turn_max_tokens=1024, max_steps=2, max_tool_calls=1, timeout=120,
                       repair_attempts=0, tools=())
    lifecycle = []

    def factory(**options):
        runtime = DeepSeekHarness(**options)
        lifecycle.append(runtime)
        return runtime

    service = TeamSessionService(TeamRepository(copied), TeamHarnessAdapter("saved-camel", settings, factory=factory),
                                autostart=False, checkpointer=InMemorySaver())
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.extensions["team_service"] = service
    app.register_blueprint(team_api_bp)
    report = {"case": "saved_paused_camel_real_rest_off_summary", "model": settings.model,
              "summary_budget": 1024, "cost_reported": None}
    try:
        with service.repository.reading() as conn:
            run_id = conn.execute("SELECT id FROM team_sessions").fetchone()[0]
        before = service.get_snapshot(run_id)
        assert before["status"] == "paused"
        assert any(task["status"] == "succeeded" and "42" in json.dumps(task["result"], ensure_ascii=False)
                   for task in before["tasks"])
        full_business = all(task["status"] == "succeeded" for task in before["tasks"])
        if full_business:
            events = service.list_events(run_id)["events"]
            assert any(event.get("type") == "activation_committed" and event.get("action") == "wait_children" for event in events)
            report.update(case="saved_completed_camel_business_real_rest_off_summary", business_status="passed",
                          tasks=len(before["tasks"]), task_statuses=[task["status"] for task in before["tasks"]])
        response = app.test_client().post("/api/team-runs/" + run_id + "/finalize", json={"summarize": True})
        assert response.status_code == 200, response.get_json()
        finished = response.get_json()
        report.update(input_tokens=finished["usage"]["prompt_tokens"] - before["usage"]["prompt_tokens"],
                      output_tokens=finished["usage"]["completion_tokens"] - before["usage"]["completion_tokens"],
                      summary_error=finished.get("summary_error"), run_status=finished["status"])
        assert finished["status"] == "completed"
        assert finished["summary"] and not finished.get("summary_error")
        assert "42" in finished["summary"]
        assert finished["usage"]["completion_tokens"] <= before["limits"]["total_max_tokens"]
        assert finished["usage"]["reserved_tokens"] == 0
        assert len(lifecycle) == 1 and lifecycle[0].client._proc is None
        control = next(service.executor.root.glob("*/control.json"))
        assert json.loads(control.read_text(encoding="utf-8"))["tools"] == []
        report.update(status="passed", reused_business_results=True, business_reexecutions=0,
                      reasoning_effort="off", reserved_tokens=0)
    finally:
        service.close()
        report.setdefault("status", "failed")
        (tmp_path / "live-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


def test_live_rest_camel_preset_keeps_prompts_and_returns_document_evidence(tmp_path, monkeypatch):
    """Exercise the actual reusable preset, without replacing role instructions."""
    from deepseek_harness import DeepSeekHarness
    from app.domain.team_presets import get_role_preset
    from app.domain.team_tools import available_team_tools

    settings = replace(HarnessSettings(), root=tmp_path / "dsh", request_max_tokens=2048,
                       turn_max_tokens=8000, max_steps=6, max_tool_calls=6, timeout=100,
                       repair_attempts=1, repair_max_tokens=768)
    monkeypatch.setattr(config, "TEAM_MODEL_CONFIGS", {
        **config.TEAM_MODEL_CONFIGS, "team-live-low": {"reasoning_effort": "low"}})
    lifecycle = []

    def factory(**options):
        runtime = DeepSeekHarness(**options)
        lifecycle.append(runtime)
        return runtime

    adapter = TeamHarnessAdapter("live-camel-" + uuid.uuid4().hex, settings, factory=factory)
    service = TeamSessionService(TeamRepository(tmp_path / "camel.db"), adapter,
                                autostart=True, checkpointer=InMemorySaver())
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.extensions["team_service"] = service
    app.register_blueprint(team_api_bp)
    browser = app.test_client()
    run_id = None
    report = {"case": "camel_pair_original_prompts_real_rest_dsh", "model": settings.model,
              "output_budget": 8000, "role_reasoning_effort": "low", "summary_reasoning_effort": "off",
              "cost_reported": None}

    def request(method, url, body=None):
        response = getattr(browser, method)(url, json=body) if body is not None else getattr(browser, method)(url)
        value = response.get_json()
        assert 200 <= response.status_code < 300, {"status": response.status_code, "response": value}
        return value

    try:
        team = request("post", "/api/team-presets/teams/camel_pair", {})
        assert len(team["nodes"]) == 2
        enabled = [tool["name"] for tool in available_team_tools()]
        for node in team["nodes"]:
            role = request("get", "/api/roles/" + node["role_id"])
            original = get_role_preset(role["preset_key"], enabled)["role"]
            assert role["system_prompt"] == original["system_prompt"]
            assert role["tools"] == original["tools"]
            assert role["default_budget"]["single_max_tokens"] is None
            # Keep the preset's actual role prompt/capabilities and select a
            # separately registered bounded test profile for business thinking.
            updated = request("put", "/api/roles/" + role["id"], {
                **role, "base_version": role["version"], "model_config_id": "team-live-low"})
            assert updated["system_prompt"] == original["system_prompt"]
            node["role_version"] = updated["version"]
        team = request("put", "/api/teams/" + team["id"], {**team, "base_version": team["version"]})
        run = request("post", "/api/team-runs", {"team_id": team["id"], "team_version": team["version"],
            "entry_node_ids": ["analyst"], "goal": (
                "设计并生成一个简单本地文档 answer.md，文件正文恰好为42。由执行员在自己工作目录实际写入，"
                "随后实际读取确认。正式交付简短包含文件路径、读取正文和验收证据，澄清员收到结果后简短汇总。"
                "工作范围仅这个小文档，使用write/read即可，无需shell、字节统计、联网或额外团队成员。"
            ), "request_id": "camel-" + uuid.uuid4().hex, "limits": {
                "single_max_tokens": 0, "total_max_tokens": 8000, "total_duration_seconds": 180,
                "summary_max_tokens": 1024, "max_concurrency": 1, "max_processes": 1,
                "max_instances": 2, "max_tasks": 2, "max_children": 1, "max_activations_per_task": 3}})
        run_id = run["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            snapshot = request("get", "/api/team-runs/" + run_id)
            if len(snapshot["tasks"]) == 2 and all(task["status"] == "succeeded" for task in snapshot["tasks"]):
                break
            if snapshot["status"] == "paused":
                errors = {task["goal"]: task.get("error") for task in snapshot["tasks"] if task.get("error")}
                report["failure"] = {"reason": snapshot.get("paused_reason"), "errors": errors}
                pytest.fail("真实预设已暂停：" + json.dumps({"reason": snapshot.get("paused_reason"), "errors": errors}, ensure_ascii=False))
            time.sleep(0.25)
        else:
            pytest.fail("真实预设未在180秒期限内完成")
        artifacts = list(adapter.root.glob("*/workspace/answer.md"))
        assert len(artifacts) == 1 and artifacts[0].read_text(encoding="utf-8").strip() == "42"
        root_task = next(task for task in snapshot["tasks"] if task["parent_task_id"] is None)
        assert "42" in json.dumps(root_task["result"], ensure_ascii=False)
        logs = service.list_tool_logs(run_id)
        for native in ("read", "write"):
            assert any(log.get("tool") == native and log.get("status") == "completed" for log in logs)
        events = service.list_events(run_id)["events"]
        assert any(event.get("type") == "activation_committed" and event.get("action") == "wait_children" for event in events)
        finished = request("post", "/api/team-runs/" + run_id + "/finalize", {"summarize": True})
        report["summary_error"] = finished.get("summary_error")
        assert finished["status"] == "completed"
        assert finished["summary"] and not finished.get("summary_error")
        assert finished["usage"]["completion_tokens"] <= 8000
        assert finished["usage"]["reserved_tokens"] == 0
        assert all(runtime.client._proc is None for runtime in lifecycle)
        report.update(status="passed", preset="camel_pair", prompts="unchanged", agents=2, tasks=2,
                      activations=len(lifecycle), input_tokens=finished["usage"]["prompt_tokens"],
                      output_tokens=finished["usage"]["completion_tokens"], native_tools=["read", "write"],
                      max_processes=1, auxiliary=["summary"], reserved_tokens=0)
    finally:
        if run_id:
            current = service.get_snapshot(run_id)
            if current:
                report.setdefault("input_tokens", current["usage"]["prompt_tokens"])
                report.setdefault("output_tokens", current["usage"]["completion_tokens"])
                report["run_status"] = current["status"]
        report.setdefault("status", "failed")
        service.close()
        (tmp_path / "live-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
