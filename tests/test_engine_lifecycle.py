import time

from app.engine import ConversationRunner
from app.llm import LLMClient


def _config():
    return {
        "agents": [
            {"name": "小明", "system_prompt": "A", "visibility": ["all"], "max_tokens": 40},
            {"name": "小红", "system_prompt": "B", "visibility": [], "max_tokens": 40},
            {"name": "小刚", "system_prompt": "C", "visibility": [], "max_tokens": 40},
        ],
        "shared_background": "讨论",
        "total_max_tokens": None,
        "total_duration_seconds": None,
        "first_speaker": "小明",
        "scheduling_mode": "willingness",
        "round_robin_order": [],
        "scheduler_params": {"lam": 2.0, "tau": 1.5, "gamma": 0.7, "forbid_consecutive": True},
    }


def _slow_llm():
    llm = LLMClient(mock=True)
    original = llm.speak

    def speak(name, system, history, max_tokens):
        time.sleep(0.02)
        return original(name, system, history, max_tokens)

    llm.speak = speak
    return llm


def test_interrupt_pauses_then_resume_and_summarize():
    llm = _slow_llm()
    runner = ConversationRunner("x", "c", "测试", _config(), llm)
    runner.start()
    time.sleep(0.1)
    runner.interrupt()
    while runner.is_alive():
        time.sleep(0.01)

    assert runner.to_dict()["status"] == "paused"
    assert runner.to_dict()["summary"] == ""

    assert runner.resume() is True
    time.sleep(0.08)
    runner.interrupt()
    while runner.is_alive():
        time.sleep(0.01)
    assert runner.to_dict()["status"] == "paused"

    assert runner.summarize_now() is True
    assert runner.to_dict()["status"] == "completed"
    assert runner.to_dict()["summary"]


def test_willingness_scores_are_recorded():
    llm = _slow_llm()
    runner = ConversationRunner("x", "c", "测试", _config(), llm)
    runner.start()
    time.sleep(0.2)
    runner.interrupt()
    while runner.is_alive():
        time.sleep(0.01)

    scored = [m for m in runner.to_dict()["messages"] if m.get("scores")]
    assert scored
    assert all(len(m["scores"]) == 3 for m in scored)


def test_from_payload_roundtrip_keeps_state():
    llm = _slow_llm()
    runner = ConversationRunner("x", "c", "测试", _config(), llm)
    runner.start()
    time.sleep(0.12)
    runner.interrupt()
    while runner.is_alive():
        time.sleep(0.01)

    payload = runner.to_dict()
    restored = ConversationRunner.from_payload(payload, llm)
    assert restored.status == "paused"
    assert len(restored.messages) == len(payload["messages"])
    assert restored.heat == payload["heat"]
    assert restored.total_output_tokens == payload["total_output_tokens"]

