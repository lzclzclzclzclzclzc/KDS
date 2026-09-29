import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.harness import HarnessTurnError
from app.engine import ConversationRunner
from app.llm import LLMClient
from app.json_repair import RepairCancelled, RepairReply, request_json_repair
from test_harness import FakeRuntime, invoke, make_manager


def reply(content='{"speech":"修正后的发言"}', tokens=5, finish="stop"):
    return RepairReply(content, finish, {"prompt_tokens": 11, "completion_tokens": tokens,
                                        "total_tokens": 11 + tokens})


def repairing_manager(tmp_path, original="这是一条普通发言。", responses=None, **settings):
    manager, runtimes = make_manager(tmp_path, **settings)
    original_factory = manager.factory
    def create(**options):
        runtime = original_factory(**options)
        runtime.response = original
        return runtime
    manager.factory = create
    calls = []
    responses = iter(responses or [reply()])
    def repair(**kwargs):
        calls.append(kwargs)
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result
    manager.repair_request = repair
    return manager, runtimes, calls


def test_valid_nested_fences_do_not_trigger_repair(tmp_path):
    data = {"speech": "有效", "whiteboard": {"ops": [{"op": "append", "content": "```python\nx = {}\n```"}]}}
    manager, _, calls = repairing_manager(tmp_path, "```json\n" + json.dumps(data) + "\n```")
    turn, usage, _ = invoke(manager)
    assert not calls
    assert turn["whiteboard_ops"] == data["whiteboard"]["ops"]
    assert usage["completion_tokens"] == 7


def test_prose_repair_uses_separate_base_and_counts_once_without_rerunning_tools(tmp_path):
    manager, runtimes, calls = repairing_manager(tmp_path, json_base_url="http://json-api/v1")
    deltas, progress = [], []
    turn, usage, state = invoke(manager, on_usage=deltas.append, on_progress=progress.append)
    assert turn["speech"] == "修正后的发言"
    assert len(runtimes[0].requests) == len(calls) == 1
    assert calls[0]["base_url"] == "http://json-api/v1"
    assert calls[0]["max_tokens"] == 93  # 100 total allowance - 7 already spent.
    assert calls[0]["api_key"] == "test-key"
    assert usage["completion_tokens"] == sum(d["completion_tokens"] for d in deltas) == 12
    assert progress[-1]["stage"] == "修正输出格式"
    assert progress[-1]["format_repairs"] == 1
    assert state["pending"] is False


def test_repair_cannot_invent_vote_or_whiteboard_edits(tmp_path):
    manager, _, _ = repairing_manager(tmp_path, responses=[reply(json.dumps({
        "speech": "修正", "propose_end": True,
        "whiteboard": {"ops": [{"op": "set", "content": "不能凭空覆盖"}]},
    }))])
    turn, _, _ = invoke(manager)
    assert turn == {"speech": "修正", "propose_end": False, "whiteboard_ops": []}


def test_existing_valid_actions_are_preserved_exactly(tmp_path):
    ops = [{"op": "append", "content": "```\n原文\n```"}]
    original = json.dumps({"content": "缺少 speech 名称", "propose_end": True,
                           "whiteboard": {"ops": ops}})
    manager, _, _ = repairing_manager(tmp_path, original)
    turn, _, _ = invoke(manager)
    assert turn["propose_end"] is True
    assert turn["whiteboard_ops"] == ops


def test_schema_error_can_be_repaired(tmp_path):
    manager, _, calls = repairing_manager(tmp_path, '{"speech":"原文","propose_end":"false"}')
    turn, _, _ = invoke(manager)
    assert turn["speech"] == "原文" and turn["propose_end"] is False
    assert "布尔值" in json.loads(calls[0]["messages"][1]["content"])["validation_error"]


def test_malformed_whiteboard_can_be_repaired_without_changing_valid_speech(tmp_path):
    original = json.dumps({"speech": "原发言", "whiteboard": {"ops": [
        {"op": ["append"], "content": "原白板内容"},
    ]}})
    repaired_ops = [{"op": "append", "content": "原白板内容"}]
    manager, _, calls = repairing_manager(tmp_path, original, responses=[reply(json.dumps({
        "speech": "不应替换原发言", "whiteboard": {"ops": repaired_ops},
    }))])
    turn, _, _ = invoke(manager)
    assert turn["speech"] == "原发言"
    assert turn["whiteboard_ops"] == repaired_ops
    assert json.loads(calls[0]["messages"][1]["content"])["preserve_whiteboard_ops"] is None


def test_second_repair_uses_original_and_accounts_failed_attempt(tmp_path):
    manager, _, calls = repairing_manager(tmp_path, responses=[reply('{}'), reply()])
    _, usage, _ = invoke(manager)
    assert len(calls) == 2
    second_input = json.loads(calls[1]["messages"][1]["content"])
    assert second_input["original_response"] == "这是一条普通发言。"
    assert second_input["previous_attempt"] == '{}'
    assert usage["completion_tokens"] == 17


@pytest.mark.parametrize("attempts", [0, 2])
def test_repairs_are_bounded_and_invalid_content_never_published(tmp_path, attempts):
    manager, _, calls = repairing_manager(tmp_path, responses=[reply('{}'), reply('{}')], repair_attempts=attempts)
    usage = []
    with pytest.raises(HarnessTurnError, match="JSON 格式校验失败"):
        invoke(manager, on_usage=usage.append)
    assert len(calls) == attempts
    assert sum(d["completion_tokens"] for d in usage) == 7 + 5 * attempts


@pytest.mark.parametrize("remaining,steps,expected", [(7, 8, "limit"), (100, 1, "steps")])
def test_repair_cannot_bypass_shared_limits(tmp_path, remaining, steps, expected):
    manager, _, calls = repairing_manager(tmp_path, max_steps=steps)
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager, remaining_output=remaining)
    assert error.value.reason == expected
    assert not calls


def test_failed_repair_exhausting_budget_prevents_another_call(tmp_path):
    manager, _, calls = repairing_manager(tmp_path, responses=[reply('{}', tokens=3)])
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager, remaining_output=10)
    assert error.value.reason == "limit"
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 3


def test_empty_original_is_not_invented(tmp_path):
    manager, _, calls = repairing_manager(tmp_path, "  ")
    with pytest.raises(HarnessTurnError, match="空正文"):
        invoke(manager)
    assert not calls


def test_even_parseable_original_is_rejected_if_provider_reports_truncation(tmp_path):
    class TruncatedRuntime(FakeRuntime):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            result.events = [{"type": "assistant/message", "data": {"stream": [
                {"type": "chunk", "chunk": {"type": "finish", "reason": {"kind": "max-tokens"}}},
            ]}}]
            return result
    manager, _ = make_manager(tmp_path, factory=TruncatedRuntime)
    manager.repair_request = lambda **_: pytest.fail("must not repair truncated content")
    with pytest.raises(HarnessTurnError, match="截断"):
        invoke(manager)


def test_truncated_repair_usage_is_counted_and_not_published(tmp_path):
    manager, _, calls = repairing_manager(tmp_path, responses=[reply(finish="length")])
    usage = []
    with pytest.raises(HarnessTurnError, match="截断"):
        invoke(manager, on_usage=usage.append)
    assert len(calls) == 1
    assert sum(d["completion_tokens"] for d in usage) == 12


def test_repair_api_error_never_falls_back_to_non_json_or_leaks_key(tmp_path):
    manager, _, calls = repairing_manager(tmp_path, responses=[RuntimeError("secret=test-key")])
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager)
    assert len(calls) == 1
    assert "test-key" not in str(error.value)


def test_cancellation_after_repair_settles_usage_but_discards_output(tmp_path):
    manager, _, calls = repairing_manager(tmp_path)
    stopped = []
    def usage(delta):
        if delta["completion_tokens"] == 5:
            stopped.append(True)
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager, on_usage=usage, should_stop=lambda: "manual" if stopped else None)
    assert error.value.reason == "manual"
    assert stopped and len(calls) == 1


@pytest.mark.parametrize("editor", ["a0", "a1"])
def test_engine_repaired_turn_keeps_whiteboard_permissions_and_single_accounting(tmp_path, editor):
    runner = ConversationRunner("repair-engine", "test", "test", {
        "agents": [{"id": "a0", "name": "甲"}, {"id": "a1", "name": "乙"}],
        "agent_backend": "dsh", "first_speaker": "a0", "total_max_tokens": 12,
        "whiteboard_enabled": True, "whiteboard_editors": [editor],
    }, LLMClient(mock=True))
    runner._persist = lambda: None
    original = '{"content":"原文","whiteboard":{"ops":[{"op":"append","content":"证据"}]}}'
    runner.harness, _, calls = repairing_manager(tmp_path, original)
    runner._run()
    assert len(runner.messages) == 1
    assert runner.total_output_tokens == 12
    assert runner.whiteboard_content == ("证据" if editor == "a0" else "")
    assert runner.paused_reason == "limit"
    assert not runner.harness_state["a0"]["pending"]
    assert len(calls) == 1


def test_engine_failed_repairs_preserve_usage_and_never_apply_partial_output(tmp_path):
    runner = ConversationRunner("repair-engine-failure", "test", "test", {
        "agents": [{"id": "a0", "name": "甲"}, {"id": "a1", "name": "乙"}],
        "agent_backend": "dsh", "first_speaker": "a0", "total_max_tokens": 100,
        "whiteboard_enabled": True, "whiteboard_editors": ["a0"],
    }, LLMClient(mock=True))
    runner._persist = lambda: None
    runner.harness, runtimes, calls = repairing_manager(tmp_path, responses=[reply('{}'), reply('{}')])
    runner._run()
    assert runner.messages == [] and runner.whiteboard_content == ""
    assert runner.total_output_tokens == 17
    assert runner.paused_reason == "error" and runner._forced_next_idx == 0
    assert runner.harness_state["a0"]["pending"] and runtimes[0].closed
    assert len(calls) == 2


class FakeAsyncClient:
    def __init__(self, response=None, delay=False):
        self.response, self.delay = response, delay
        self.options, self.request, self.closed, self.cancelled = None, None, False, False
        self.chat = SimpleNamespace(completions=self)

    def factory(self, **options):
        self.options = options
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def create(self, **kwargs):
        self.request = kwargs
        try:
            if self.delay:
                await asyncio.Event().wait()
            return self.response
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def async_request(client, **overrides):
    args = dict(api_key="test-key", base_url="http://local/v1", model="test-model",
                messages=[{"role": "user", "content": "JSON"}], max_tokens=20,
                deadline=time.monotonic()+2, should_stop=lambda: None, client_factory=client.factory)
    args.update(overrides)
    return request_json_repair(**args)


def test_transport_requests_json_mode_and_disables_implicit_retries():
    client = FakeAsyncClient(SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=10, completion_tokens=2, total_tokens=12), choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"speech":"x"}'), finish_reason="stop")]))
    result = async_request(client)
    assert client.request["response_format"] == {"type": "json_object"}
    assert client.request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "tools" not in client.request
    assert client.options["max_retries"] == 0
    assert result.usage["completion_tokens"] == 2 and client.closed


@pytest.mark.parametrize("reason", ["manual", "limit", "timeout"])
def test_transport_cancels_pending_http_request_and_closes_client(reason):
    client = FakeAsyncClient(delay=True)
    start = time.monotonic()
    with pytest.raises(RepairCancelled) as error:
        async_request(client, deadline=start+(0.15 if reason=="timeout" else 2),
                      should_stop=lambda: reason if reason!="timeout" and time.monotonic()-start>0.1 else None)
    assert error.value.reason == reason
    assert client.cancelled and client.closed
    assert time.monotonic()-start < 1
