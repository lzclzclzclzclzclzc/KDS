"""Explicitly opt-in, billable end-to-end checks against the configured API.

KDS_TEST_DEEPSEEK_LIVE=1 python -m pytest tests/test_harness_live.py -q -s
Uses synthetic records, temporary workspaces, bounded output and real read tools.
Never runs against the user's saved discussions or prints credentials.
"""
import json
import os
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.engine import ConversationRunner
from app.harness import HarnessManager, HarnessSettings, _identity
from app.llm import LLMClient


pytestmark = pytest.mark.skipif(os.getenv("KDS_TEST_DEEPSEEK_LIVE") != "1",
                                reason="真实 API 验证会消耗额度，需显式启用")


def test_live_two_roles_read_files_and_update_whiteboard(tmp_path):
    roles = [{"id": role, "name": role, "system_prompt": (
        "这是一次简短的连通性测试。你必须先调用 read 读取自己工作目录中的 evidence.txt。"
        "speech 必须包含文件里的完整标识；whiteboard.ops 追加同样标识。"
        "只读这个文件即可，不需要其他调查。"
    )} for role in ("甲", "乙")]
    runner = ConversationRunner("live-" + uuid.uuid4().hex, "live", "真实 API 测试", {
        "agent_backend": "dsh", "agents": roles, "first_speaker": "甲",
        "single_max_tokens": 200, "total_max_tokens": 16000, "total_duration_seconds": 240,
        "whiteboard_enabled": True, "whiteboard_editors": ["甲", "乙"],
    }, LLMClient(mock=False))
    settings = replace(HarnessSettings(), root=tmp_path, tools=("read",),
                       request_max_tokens=4096, turn_max_tokens=8000,
                       max_steps=4, max_tool_calls=3, timeout=120)
    runner.harness = HarnessManager(runner.id, settings)
    markers = {}
    for role in roles:
        workspace = runner.harness.root / _identity(role["id"]) / "workspace"
        workspace.mkdir(parents=True)
        markers[role["name"]] = "evidence-" + uuid.uuid4().hex[:12]
        (workspace / "evidence.txt").write_text(markers[role["name"]], encoding="utf-8")

    # Exercise the real engine but keep synthetic conversations out of the DB.
    activities = []
    def checkpoint():
        snapshot = runner.to_dict()
        if snapshot["harness_activity"]:
            activity = snapshot["harness_activity"]
            if not activities or activity != activities[-1]:
                activities.append(activity)
                print(json.dumps({"role": activity["agent_name"],
                                  "stage": activity["stage"], "steps": activity["steps"]},
                                 ensure_ascii=False), flush=True)
        if len(snapshot["messages"]) >= 2 and snapshot["status"] == "running":
            runner.interrupt()
    runner._persist = checkpoint
    runner._run()
    snapshot = runner.to_dict()
    assert snapshot["error"] is None, snapshot["error"]
    assert snapshot["paused_reason"] == "manual"
    assert len(snapshot["messages"]) == 2
    for message in snapshot["messages"]:
        assert markers[message["speaker"]] in message["content"]
        assert markers[message["speaker"]] in snapshot["whiteboard"]["content"]
    assert len({s["session_id"] for s in snapshot["harness_state"].values()}) == 2
    assert all(not s["pending"] for s in snapshot["harness_state"].values())
    assert all(any(a["agent_name"] == role["name"] and a["tool_calls"] >= 1
                   for a in activities) for role in roles)
    print(json.dumps({"live_discussion": "passed", "roles": 2,
                      "output_tokens": snapshot["total_output_tokens"],
                      "input_tokens": snapshot["total_prompt_tokens"]}, ensure_ascii=False))


def test_live_auxiliary_json_call():
    client = LLMClient(mock=False)
    content, usage = client._call([
        {"role": "system", "content": '只输出 JSON 对象 {"score":42}，不要额外解释。'},
        {"role": "user", "content": "请给出约定的评分。"},
    ], 0, max_tokens=512, json_mode=True)
    assert content, "辅助 API 没有返回正文；检查推理输出是否耗尽 token 上限"
    assert json.loads(content) == {"score": 42}
    print(json.dumps({"live_auxiliary": "passed", "usage": usage}, ensure_ascii=False))


def test_live_json_mode_repair_after_injected_format_error(tmp_path):
    # Controlled format fault, then a REAL JSON-mode API call. No tool replay.
    class InvalidFinalRuntime:
        def __init__(self, **_options):
            self.calls = 0
        def run(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(finish_reason="completed", events=[],
                final_response='{"speech":"格式修正测试：答案是42。","propose_end":false,}')
        def close(self):
            pass

    settings = replace(HarnessSettings(), root=tmp_path, request_max_tokens=512,
                       repair_max_tokens=512, turn_max_tokens=1024, timeout=45)
    manager = HarnessManager("live-repair", settings, factory=InvalidFinalRuntime)
    usage, progress = [], []
    try:
        turn, totals, state = manager.run_turn(agent={"id": "a0", "name": "测试"},
            system="格式测试", history=[], state={}, remaining_output=1024,
            should_stop=lambda: None, on_usage=usage.append, on_progress=progress.append)
        assert "42" in turn["speech"]
        assert not turn["propose_end"] and turn["whiteboard_ops"] == []
        assert progress[-1]["format_repairs"] >= 1 and state["pending"] is False
        assert totals["completion_tokens"] == sum(u["completion_tokens"] for u in usage) > 0
        assert manager._runtimes["a0"].calls == 1
        print(json.dumps({"live_json_repair": "passed", "usage": totals}, ensure_ascii=False))
    finally:
        manager.close()
