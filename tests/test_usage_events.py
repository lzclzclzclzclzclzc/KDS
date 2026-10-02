"""Provider fallback boundaries and durable Harness usage identities."""
import json
from types import SimpleNamespace

import pytest

from app.json_repair import RepairReply
from app.llm import LLMClient
from test_harness import invoke, make_manager


class ProviderError(RuntimeError):
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status_code = status
        self.body = body


def provider_client(error):
    calls = []

    def create(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            raise error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"score": 42}'))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )

    client = LLMClient(mock=True)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, calls


@pytest.mark.parametrize("error", [
    ProviderError(400, "response_format is not supported with this model"),
    ProviderError(422, "Unknown parameter: response_format"),
    ProviderError(400, "JSON mode is not supported"),
    ProviderError(400, "opaque", {"error": {
        "param": "response_format", "code": "unsupported_parameter", "message": "Unsupported parameter",
    }}),
])
def test_json_mode_downgrades_only_explicit_capability_rejection(error):
    client, calls = provider_client(error)
    content, usage = client._call([], 0, 10, json_mode=True)
    assert content == '{"score": 42}' and usage["total_tokens"] == 5
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    assert {k: v for k, v in calls[0].items() if k != "response_format"} == calls[1]


@pytest.mark.parametrize("error", [
    ProviderError(429, "response_format is unsupported while requests are rate limited"),
    ProviderError(503, "response_format is unsupported"),
    ProviderError(401, "response_format is unsupported"),
    ProviderError(400, "Invalid response_format schema"),
    ProviderError(400, "Unsupported parameter: temperature"),
    ProviderError(400, "opaque", {"error": {
        "param": "temperature", "message": "response_format is supported; temperature is unsupported",
    }}),
    ProviderError(422, "Cannot validate request"),
    TimeoutError("request timed out"),
    ConnectionError("network unavailable"),
])
def test_json_mode_does_not_retry_unrelated_failures(error):
    client, calls = provider_client(error)
    with pytest.raises(type(error)) as captured:
        client._call([], 0, 10, json_mode=True)
    assert captured.value is error and len(calls) == 1


def test_non_json_call_does_not_retry_capability_rejection():
    error = ProviderError(400, "response_format is not supported")
    client, calls = provider_client(error)
    with pytest.raises(ProviderError):
        client._call([], 0, 10)
    assert len(calls) == 1


def test_harness_usage_identity_deduplicates_notifications_but_separates_runs(tmp_path):
    manager, runtimes = make_manager(tmp_path)
    try:
        first, second = [], []
        _, usage, state = invoke(manager, on_usage=first.append)
        history = [{"role": "user", "content": text} for text in ("问题", "甲:已回答", "乙:新问题")]
        _, second_usage, _ = invoke(manager, state=state, history=history, on_usage=second.append)
        assert usage == second_usage == {
            "prompt_tokens": 45, "completion_tokens": 7, "total_tokens": 52,
        }
        assert len(first) == len(second) == 2  # The repeated seq=2 is already removed.
        keys = [json.loads(delta["_event_id"]) for delta in first + second]
        assert all(key[0] == "dsh" for key in keys)
        assert [key[3] for key in keys] == [2, 4, 2, 4]
        assert keys[0][1] == keys[1][1] != keys[2][1] == keys[3][1]
        assert keys[0][2] == keys[2][2] == runtimes[0].requests[0][0]
        assert len({delta["_event_id"] for delta in first + second}) == 4
    finally:
        manager.close()


def test_json_repair_usage_identity_tracks_each_request_without_mutating_reply(tmp_path):
    manager, _ = make_manager(tmp_path)
    original_factory = manager.factory

    def create(**options):
        runtime = original_factory(**options)
        runtime.response = "待修正的原文"
        return runtime

    manager.factory = create
    replies = [
        RepairReply('{}', 'stop', {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}),
        RepairReply('{"speech":"有效发言"}', 'stop', {
            "prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16,
        }),
    ]
    responses = iter(replies)
    manager.repair_request = lambda **_: next(responses)
    deltas = []
    try:
        _, usage, _ = invoke(manager, on_usage=deltas.append)
        repair_keys = [json.loads(delta["_event_id"]) for delta in deltas[2:]]
        original_key = json.loads(deltas[0]["_event_id"])
        assert repair_keys == [
            ["json_repair", original_key[1], original_key[2], 1],
            ["json_repair", original_key[1], original_key[2], 2],
        ]
        assert usage["completion_tokens"] == sum(delta["completion_tokens"] for delta in deltas) == 17
        assert all("_event_id" not in reply.usage for reply in replies)
    finally:
        manager.close()
