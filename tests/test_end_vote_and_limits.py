import time

from app.engine import ConversationRunner, END_PROPOSAL_MARKER
from app.llm import LLMClient


def _config(**overrides):
    config = {
        "agents": [
            {"id": "a0", "name": "小明", "system_prompt": "A", "visibility": ["all"]},
            {"id": "a1", "name": "小红", "system_prompt": "B", "visibility": []},
            {"id": "a2", "name": "小刚", "system_prompt": "C", "visibility": []},
        ],
        "shared_background": "讨论",
        "total_max_tokens": None,
        "total_duration_seconds": None,
        "first_speaker": "小明",
        "scheduling_mode": "round_robin",
        "round_robin_order": ["小明", "小红", "小刚"],
        "scheduler_params": {"lam": 2.0, "tau": 1.5, "gamma": 0.7, "forbid_consecutive": True},
    }
    config.update(overrides)
    return config


def _wait_paused(runner, timeout=3.0):
    deadline = time.time() + timeout
    while runner.is_alive() and time.time() < deadline:
        time.sleep(0.01)


# ---- Proposer prompt injection ----

def test_only_proposers_get_end_instruction():
    llm = LLMClient(mock=True)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(end_vote_enabled=True, end_vote_proposers=["a0"]),
        llm,
    )
    proposer_sys = runner._build_system(runner.agents[0])
    other_sys = runner._build_system(runner.agents[1])
    assert END_PROPOSAL_MARKER in proposer_sys
    assert END_PROPOSAL_MARKER not in other_sys


def test_disabled_means_no_proposer():
    llm = LLMClient(mock=True)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(end_vote_enabled=False, end_vote_proposers=["a0"]),
        llm,
    )
    assert runner._is_proposer(runner.agents[0]) is False


def test_extract_end_proposal_strips_marker():
    detected, cleaned = ConversationRunner._extract_end_proposal(
        f"我觉得聊得差不多了。\n{END_PROPOSAL_MARKER}"
    )
    assert detected is True
    assert END_PROPOSAL_MARKER not in cleaned
    assert "聊得差不多了" in cleaned

    detected2, cleaned2 = ConversationRunner._extract_end_proposal("普通发言")
    assert detected2 is False
    assert cleaned2 == "普通发言"


# ---- Inline end vote: unanimous vs not ----

def _proposing_llm(vote_all_agree):
    """Mock LLM: proposer emits the end marker; votes are all-agree or all-disagree."""
    llm = LLMClient(mock=True)

    def speak(name, system, history, max_tokens):
        # First speaker (小明) proposes ending on its first turn.
        if name == "小明" and END_PROPOSAL_MARKER in system:
            return f"我提议结束。\n{END_PROPOSAL_MARKER}", {
                "prompt_tokens": 0, "completion_tokens": 5, "total_tokens": 5,
            }
        return "普通发言", {"prompt_tokens": 0, "completion_tokens": 5, "total_tokens": 5}

    def vote(name, system, history_text, question, options, votes_per_person):
        choice = "1" if vote_all_agree else "2"
        return {"choices": [choice], "reason": "mock"}, {
            "prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 3,
        }

    llm.speak = speak
    llm.vote = vote
    return llm


def test_unanimous_end_vote_pauses_vote_end():
    llm = _proposing_llm(vote_all_agree=True)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(total_max_tokens=100000, end_vote_enabled=True, end_vote_proposers=["a0"]),
        llm,
    )
    runner.start()
    _wait_paused(runner)
    snap = runner.to_dict()
    assert snap["status"] == "paused"
    assert snap["paused_reason"] == "vote_end"
    end_votes = [v for v in snap["votes"] if v.get("kind") == "end"]
    assert end_votes and end_votes[-1]["agreed"] is True
    # vote_end pause is resumable (continue conversation).
    assert snap["can_resume"] is True


def test_non_unanimous_end_vote_continues_with_cooldown():
    llm = _proposing_llm(vote_all_agree=False)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(total_max_tokens=60, end_vote_enabled=True, end_vote_proposers=["a0"],
                end_vote_cooldown_turns=3),
        llm,
    )
    runner.start()
    _wait_paused(runner)
    snap = runner.to_dict()
    # Vote failed -> conversation kept going until the token limit, not vote_end.
    assert snap["paused_reason"] == "limit"
    end_votes = [v for v in snap["votes"] if v.get("kind") == "end"]
    assert end_votes and end_votes[0]["agreed"] is False


# ---- Extend limits + resume from limit pause ----

def test_limit_pause_blocks_resume_until_extended():
    llm = LLMClient(mock=True)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(total_max_tokens=50),
        llm,
    )
    runner.start()
    _wait_paused(runner)
    snap = runner.to_dict()
    assert snap["status"] == "paused"
    assert snap["paused_reason"] == "limit"
    assert snap["can_resume"] is False
    # Resume refused while still over the limit.
    assert runner.resume() is False

    # Extend the cap above what was consumed, then resume works.
    used = snap["total_output_tokens"]
    assert runner.update_limits(used + 100000, None) is True
    assert runner.to_dict()["can_resume"] is True
    assert runner.resume() is True
    runner.interrupt()
    _wait_paused(runner)


def test_only_manual_summarize_completes():
    llm = LLMClient(mock=True)
    runner = ConversationRunner("x", "c", "t", _config(total_max_tokens=50), llm)
    runner.start()
    _wait_paused(runner)
    # Reaching the limit never auto-completes.
    assert runner.to_dict()["status"] == "paused"
    assert runner.summarize_now() is True
    done = runner.to_dict()
    assert done["status"] == "completed"
    assert done["summary"]


# ---- Human speech while paused ----

def test_human_say_appends_while_paused():
    llm = LLMClient(mock=True)
    runner = ConversationRunner("x", "c", "t", _config(total_max_tokens=50), llm)
    runner.start()
    _wait_paused(runner)
    before = len(runner.to_dict()["messages"])
    assert runner.human_say("人类插话") == "appended"
    msgs = runner.to_dict()["messages"]
    assert len(msgs) == before + 1
    assert msgs[-1]["role"] == "human"
    assert msgs[-1]["content"] == "人类插话"


# ---- Round-trip keeps new fields ----

def test_from_payload_keeps_end_vote_config():
    llm = LLMClient(mock=True)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(total_max_tokens=50, end_vote_enabled=True, end_vote_proposers=["a0"],
                end_vote_cooldown_turns=4),
        llm,
    )
    runner.start()
    _wait_paused(runner)
    payload = runner.to_dict()
    restored = ConversationRunner.from_payload(payload, llm)
    assert restored.end_vote_enabled is True
    assert restored.end_vote_proposers == ["a0"]
    assert restored.end_vote_cooldown_turns == 4


# ---- Human designates the next speaker ----

def test_human_say_forces_next_speaker_while_paused():
    llm = LLMClient(mock=True)
    runner = ConversationRunner("x", "c", "t", _config(total_max_tokens=50), llm)
    runner.start()
    _wait_paused(runner)
    # While paused, queue a human message targeting 小刚 (a2).
    assert runner.human_say("请小刚先说", target="小刚") == "appended"
    assert runner._forced_next_idx == 2
    # Extend so the loop can run one more agent turn, then resume.
    runner.update_limits(runner.to_dict()["total_output_tokens"] + 100000, None)
    assert runner.resume() is True
    # Let exactly the forced turn happen, then stop.
    time.sleep(0.15)
    runner.interrupt()
    _wait_paused(runner)
    msgs = runner.to_dict()["messages"]
    # Find the human message, the next agent message must be 小刚.
    idx = next(i for i, m in enumerate(msgs) if m["role"] == "human" and m["content"] == "请小刚先说")
    following = [m for m in msgs[idx + 1:] if m["role"] == "agent"]
    assert following, "expected at least one agent turn after the human message"
    assert following[0]["speaker"] == "小刚"
    # The forced slot is consumed (not sticky).
    assert runner._forced_next_idx is None


def test_resolve_agent_idx_by_id_and_name():
    llm = LLMClient(mock=True)
    runner = ConversationRunner("x", "c", "t", _config(total_max_tokens=50), llm)
    assert runner._resolve_agent_idx("a1") == 1
    assert runner._resolve_agent_idx("小刚") == 2
    assert runner._resolve_agent_idx("不存在") is None
    assert runner._resolve_agent_idx(None) is None


def test_human_say_forces_next_speaker_while_running():
    # Slow speak so the reserve lands mid-run; the forced speaker must apply to
    # the turn right AFTER the human message, never to an in-flight agent turn.
    llm = LLMClient(mock=True)
    original = llm.speak

    def slow_speak(name, system, history, max_tokens):
        time.sleep(0.03)
        return original(name, system, history, max_tokens)

    llm.speak = slow_speak
    runner = ConversationRunner(
        "x", "c", "t",
        _config(total_max_tokens=1000000, total_duration_seconds=3600),
        llm,
    )
    runner.start()
    time.sleep(0.1)
    assert runner.human_say("点名小刚", target="小刚") == "reserved"
    time.sleep(0.2)
    runner.interrupt()
    _wait_paused(runner)

    msgs = runner.to_dict()["messages"]
    hidx = next(i for i, m in enumerate(msgs)
                if m["role"] == "human" and m["content"] == "点名小刚")
    following = [m for m in msgs[hidx + 1:] if m["role"] == "agent"]
    assert following, "expected an agent turn after the human message"
    assert following[0]["speaker"] == "小刚"

