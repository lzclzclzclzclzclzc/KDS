from app.engine import ConversationRunner
from app.llm import LLMClient


def _config():
    return {
        "agents": [
            {"id": "a0", "name": "小明", "system_prompt": "你是一个乐观的程序员。", "visibility": ["all"], "max_tokens": 60},
            {"id": "a1", "name": "小红", "system_prompt": "你是一个谨慎的产品经理。", "visibility": [], "max_tokens": 60},
            {"id": "a2", "name": "小刚", "system_prompt": "你是一个爱抬杠的设计师。", "visibility": ["a0"], "max_tokens": 60},
        ],
        "shared_background": "一起讨论周末团建去哪。",
        "total_max_tokens": 500,
        "total_duration_seconds": None,
        "first_speaker": "小明",
        "scheduling_mode": "willingness",
        "round_robin_order": [],
        "scheduler_params": {"lam": 2.0, "tau": 1.5, "gamma": 0.7, "forbid_consecutive": True},
    }


def test_engine_runs_and_summarizes_with_mock():
    llm = LLMClient(mock=True)
    assert llm.mock is True
    runner = ConversationRunner("test1", "cfg1", "测试对话", _config(), llm)
    # Run synchronously by invoking the private loop in this thread.
    runner._run()
    state = runner.to_dict()
    assert state["status"] == "paused"
    assert state["paused_reason"] == "limit"
    assert len(state["messages"]) >= 2
    assert state["summary"] == ""
    assert state["total_output_tokens"] > 0

    assert runner.summarize_now() is True
    summarized = runner.to_dict()
    assert summarized["status"] == "completed"
    assert summarized["summary"]
