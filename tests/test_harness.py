import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.engine import ConversationRunner
from app.harness import HarnessManager, HarnessSettings, HarnessTurnError, event_usage, parse_final
from app.llm import LLMClient


class FakeRuntime:
    def __init__(self, **options):
        self.options = options
        self.requests = []
        self.closed = False
        self.response = '{"speech":"已查阅资料","whiteboard":{"ops":[{"op":"append","content":"证据"}]}}'

    def emit(self, callback, session, seq, event_type, data):
        callback(SimpleNamespace(method="session.event", payload={
            "sessionId": session, "event": {"seq": seq, "type": event_type, "data": data},
        }))

    def run(self, prompt, *, session_id, on_notification):
        self.requests.append((session_id, prompt))
        self.emit(on_notification, session_id, 1, "step/start", {})
        self.emit(on_notification, session_id, 2, "assistant/message", {"usage": {
            "inputTokens": 10, "cacheReadTokens": 20, "outputTokens": 3, "reasoningTokens": 2,
        }})
        # A duplicated delivery must not double-charge.
        self.emit(on_notification, session_id, 2, "assistant/message", {"usage": {
            "inputTokens": 10, "cacheReadTokens": 20, "outputTokens": 3,
        }})
        self.emit(on_notification, session_id, 3, "tool/call", {"name": "read"})
        self.emit(on_notification, session_id, 4, "assistant/message", {"usage": {
            "inputTokens": 15, "outputTokens": 4,
        }})
        return SimpleNamespace(finish_reason="completed", final_response=self.response)

    def close(self):
        self.closed = True


def make_manager(tmp_path, factory=FakeRuntime, **overrides):
    settings = replace(HarnessSettings(root=tmp_path, api_key="test-key"), **overrides)
    runtimes = []

    def create(**kwargs):
        runtime = factory(**kwargs)
        runtimes.append(runtime)
        return runtime

    return HarnessManager("conversation", settings, factory=create), runtimes


def invoke(manager, **overrides):
    options = dict(agent={"id": "a0", "name": "甲"}, system="甲的独立设定 {{原样保留}}",
                   history=[{"role": "user", "content": "人类:问题"}], state={},
                   remaining_output=100, should_stop=lambda: None,
                   on_progress=lambda _: None, on_usage=lambda _: None)
    options.update(overrides)
    return manager.run_turn(**options)


def test_separate_roles_reuse_session_and_only_append_new_history(tmp_path):
    manager, runtimes = make_manager(tmp_path)
    deltas, progress = [], []
    turn, usage, state = invoke(manager, on_usage=deltas.append, on_progress=progress.append)
    assert turn["speech"] == "已查阅资料"
    assert usage == {"prompt_tokens": 45, "completion_tokens": 7, "total_tokens": 52}
    assert sum(d["completion_tokens"] for d in deltas) == 7
    assert progress[0]["state"]["pending"]
    assert progress[-1]["tool_calls"] == 1
    history = [{"role": "user", "content": text} for text in ("原问题", "甲:已回答", "乙:新问题")]
    invoke(manager, state=state, history=history)
    assert len(runtimes) == 1
    assert runtimes[0].requests[0][0] == runtimes[0].requests[1][0]
    assert "乙:新问题" in runtimes[0].requests[1][1]
    assert "原问题" not in runtimes[0].requests[1][1]
    invoke(manager, agent={"id": "../../other", "name": "乙"}, system="乙的私有设定")
    assert len(runtimes) == 2
    assert runtimes[0].options["dsh_home"] != runtimes[1].options["dsh_home"]
    assert runtimes[0].options["cwd"] != runtimes[1].options["cwd"]
    assert "test-key" not in json.dumps(progress)
    manager.close()
    assert all(r.closed for r in runtimes)


def test_uncertain_turn_uses_fresh_session_with_full_history(tmp_path):
    manager, runtimes = make_manager(tmp_path)
    _, _, state = invoke(manager)
    invoke(manager, state={**state, "pending": True})
    assert runtimes[0].requests[0][0] != runtimes[0].requests[1][0]
    assert "人类:问题" in runtimes[0].requests[1][1]


@pytest.mark.parametrize("text", ["普通文本", '{"speech":""}',
    '{"speech":"x","propose_end":"false"}', '{"speech":"x","whiteboard":"覆盖"}',
    '{"speech":"x","whiteboard":{"ops":[{"op":"replace","find":1}]}}'])
def test_invalid_final_is_not_published(text):
    with pytest.raises(HarnessTurnError):
        parse_final(text)


def test_failed_attempt_usage_includes_cache_but_not_duplicate_reasoning():
    usage = event_usage({"type": "assistant/attempt", "data": {"stream": [
        {"type": "chunk", "time": 1, "chunk": {"type": "usage", "usage": {"inputTokens": 5, "cacheReadTokens": 20,
         "cacheWriteTokens": 3, "outputTokens": 7, "reasoningTokens": 4}}},
    ]}})
    assert usage == {"prompt_tokens": 28, "completion_tokens": 7, "total_tokens": 35}


def test_compaction_usage_is_counted():
    assert event_usage({"type": "compaction/summary", "data": {
        "usage": {"inputTokens": 10, "outputTokens": 4},
    }}) == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}


def test_initialization_error_is_recoverable_and_does_not_expose_key(tmp_path):
    def broken_factory(**options):
        raise RuntimeError(options["api_key"])

    manager, _ = make_manager(tmp_path, factory=broken_factory)
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager)
    assert "初始化失败" in str(error.value)
    assert "test-key" not in str(error.value)


def test_provider_error_is_readable_and_key_is_redacted(tmp_path):
    class FailedRuntime(FakeRuntime):
        def run(self, *_args, **_kwargs):
            return SimpleNamespace(finish_reason="error", events=[{
                "type": "turn/end", "data": {"reason": {"error": {
                    "message": "invalid model, credential=test-key"}}},
            }])

    manager, _ = make_manager(tmp_path, factory=FailedRuntime)
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager)
    assert "invalid model" in str(error.value)
    assert "test-key" not in str(error.value)
    manager.close()


def test_messages_404_explains_separate_api_bases(tmp_path):
    class FailedRuntime(FakeRuntime):
        def run(self, *_args, **_kwargs):
            return SimpleNamespace(finish_reason="error", events=[{
                "type": "turn/end", "data": {"reason": {"error": {
                    "message": "DeepSeek Messages request failed (404)"}}},
            }])

    manager, _ = make_manager(tmp_path, factory=FailedRuntime)
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager)
    assert "DSH_BASE_URL" in str(error.value)
    assert "https://api.deepseek.com/anthropic" in str(error.value)
    assert "LLM_BASE_URL" in str(error.value)
    manager.close()


class CancellableRuntime(FakeRuntime):
    def run(self, prompt, *, session_id, on_notification):
        from pathlib import Path
        path = Path(self.options["env"]["KDS_CONTROL_FILE"])
        self.running = True
        deadline = time.monotonic() + 3
        while not self.closed and time.monotonic() < deadline:
            try:
                cancelled = json.loads(path.read_text(encoding="utf-8")).get("cancel")
            except PermissionError:
                # Python's Windows reader can race atomic replacement. Keep
                # this test double polling instead of simulating an SDK crash.
                time.sleep(0.01)
                continue
            if cancelled:
                self.emit(on_notification, session_id, 1, "assistant/message", {
                    "usage": {"inputTokens": 10, "outputTokens": 2}, "interrupted": True,
                })
                return SimpleNamespace(finish_reason="aborted", final_response='{"speech":"未完成"}')
            time.sleep(0.01)
        raise TimeoutError("fake runtime did not receive cancellation")


@pytest.mark.parametrize("reason", ["manual", "limit", "timeout"])
def test_cancellation_accounts_partial_usage_without_publishing(tmp_path, reason):
    manager, runtimes = make_manager(tmp_path, factory=CancellableRuntime, timeout=0.15 if reason == "timeout" else 2)
    started = time.monotonic()
    usage = []
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager, should_stop=lambda: reason if reason != "timeout" and any(
                   getattr(runtime, "running", False) for runtime in runtimes) else None,
               on_usage=usage.append)
    assert error.value.reason == reason
    assert sum(u["completion_tokens"] for u in usage) == 2
    assert time.monotonic() - started < 2
    manager.close()


def test_failed_cancel_file_write_closes_runtime(tmp_path, monkeypatch):
    import app.harness as harness

    class WaitingRuntime(FakeRuntime):
        def run(self, *_args, **_kwargs):
            deadline = time.monotonic() + 2
            while not self.closed and time.monotonic() < deadline:
                time.sleep(0.01)
            if not self.closed:
                raise AssertionError("watchdog failed to close runtime")
            raise RuntimeError("runtime closed")

    original_write = harness._write_json

    def fail_cancel_write(path, value):
        if isinstance(value, dict) and value.get("cancel"):
            raise OSError("disk unavailable")
        original_write(path, value)

    monkeypatch.setattr(harness, "_write_json", fail_cancel_write)
    manager, runtimes = make_manager(tmp_path, factory=WaitingRuntime, timeout=0.1)
    with pytest.raises(HarnessTurnError) as error:
        invoke(manager)
    assert error.value.reason == "timeout"
    assert runtimes[0].closed
    manager.close()


@pytest.mark.parametrize("persistent", [False, True])
def test_cancel_file_replacement_handles_windows_read_contention(tmp_path, monkeypatch, persistent):
    from pathlib import Path
    from app.harness import _write_json

    path = tmp_path / "control.json"
    path.write_text('{"cancel":null}', encoding="utf-8")
    original_replace = Path.replace
    failures = []

    def reader_holds_file(self, target):
        if self == path.with_suffix(".tmp") and (persistent or len(failures) < 2):
            failures.append(True)
            error = PermissionError("Windows reader holds the old file")
            error.winerror = 5
            raise error
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", reader_holds_file)
    started = time.monotonic()
    if persistent:
        with pytest.raises(PermissionError):
            _write_json(path, {"cancel": "manual"})
        assert json.loads(path.read_text(encoding="utf-8")) == {"cancel": None}
    else:
        _write_json(path, {"cancel": "manual"})
        assert json.loads(path.read_text(encoding="utf-8")) == {"cancel": "manual"}
        assert not path.with_suffix(".tmp").exists()
    assert time.monotonic() - started < 1


def test_runner_whiteboard_usage_checkpoint_and_restart(tmp_path):
    runner = ConversationRunner("conv", "cfg", "test", {
        "agent_backend": "dsh", "agents": [{"id": "a0", "name": "甲"}, {"id": "a1", "name": "乙"}],
        "first_speaker": "a0", "total_max_tokens": 7,
        "whiteboard_enabled": True, "whiteboard_editors": ["a0"],
    }, LLMClient(mock=True))
    runner.harness, runtimes = make_manager(tmp_path)
    runner._run()
    payload = runner.to_dict()
    assert payload["paused_reason"] == "limit"
    assert len(payload["messages"]) == 1
    assert payload["total_output_tokens"] == 7
    assert payload["whiteboard"]["content"] == "证据"
    assert payload["harness_state"]["a0"]["history_cursor"] == 1
    assert runtimes[0].closed
    restored = ConversationRunner.from_payload(payload, LLMClient(mock=True))
    assert restored.harness_state == runner.harness_state
    assert restored.agent_backend == "dsh"
    payload["config"].pop("agent_backend")
    assert ConversationRunner.from_payload(payload, LLMClient(mock=True)).agent_backend == "direct"


def test_runner_pause_keeps_speaker_for_retry(tmp_path):
    runner = ConversationRunner("conv", "cfg", "test", {
        "agents": [{"id": "a0", "name": "甲"}, {"id": "a1", "name": "乙"}],
        "first_speaker": "a0", "total_max_tokens": 100,
    }, LLMClient(mock=True))
    runner.harness, _ = make_manager(tmp_path, factory=CancellableRuntime)
    runner.start()
    deadline = time.monotonic() + 2
    while not runner.harness_activity and time.monotonic() < deadline:
        time.sleep(0.01)
    runner.interrupt()
    runner._thread.join(3)
    assert not runner.is_alive()
    assert runner.paused_reason == "manual"
    assert runner.total_output_tokens == 2
    assert runner.messages == []
    assert runner._forced_next_idx == 0
    assert runner.harness_state["a0"]["pending"]
