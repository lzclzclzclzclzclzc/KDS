import time

from app.engine import ConversationRunner
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


def _usage(n=5):
    return {"prompt_tokens": 0, "completion_tokens": n, "total_tokens": n}


# ---- Structured-turn prompt: capability fields ----

def test_only_proposers_get_propose_end_field():
    llm = LLMClient(mock=True)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(end_vote_enabled=True, end_vote_proposers=["a0"]),
        llm,
    )
    proposer_sys = runner._build_system(runner.agents[0])
    other_sys = runner._build_system(runner.agents[1])
    assert '"propose_end"' in proposer_sys
    assert '"propose_end"' not in other_sys


def test_disabled_means_no_proposer():
    llm = LLMClient(mock=True)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(end_vote_enabled=False, end_vote_proposers=["a0"]),
        llm,
    )
    assert runner._is_proposer(runner.agents[0]) is False


# ---- Inline end vote via the propose_end field ----

def _proposing_llm(vote_all_agree):
    """Mock: proposer sets propose_end=true; votes all-agree or all-disagree."""
    llm = LLMClient(mock=True)

    def agent_turn(name, system, history, max_tokens):
        # Only the proposer's system prompt declares the propose_end field.
        propose = '"propose_end"' in system
        return {"speech": "普通发言", "propose_end": propose, "whiteboard_ops": []}, _usage()

    def vote(name, system, history_text, question, options, votes_per_person):
        choice = "1" if vote_all_agree else "2"
        return {"choices": [choice], "reason": "mock"}, _usage(3)

    llm.agent_turn = agent_turn
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
    assert snap["paused_reason"] == "limit"
    end_votes = [v for v in snap["votes"] if v.get("kind") == "end"]
    assert end_votes and end_votes[0]["agreed"] is False


# ---- Whiteboard ----

def test_apply_whiteboard_ops():
    apply = ConversationRunner._apply_whiteboard_ops
    assert apply("", [{"op": "append", "content": "# 标题"}]) == "# 标题"
    assert apply("A", [{"op": "append", "content": "B"}]) == "A\nB"
    assert apply("A", [{"op": "prepend", "content": "B"}]) == "B\nA"
    assert apply("hello world", [{"op": "replace", "find": "world", "replace": "KDS"}]) == "hello KDS"
    # replace with no match is a no-op
    assert apply("abc", [{"op": "replace", "find": "zzz", "replace": "q"}]) == "abc"
    # set overrides
    assert apply("old", [{"op": "set", "content": "new"}]) == "new"
    # op inferred from keys
    assert apply("A", [{"content": "B"}]) == "A\nB"
    # unknown / malformed ignored
    assert apply("A", ["nope", {"op": "frobnicate"}]) == "A"


def _wb_llm(ops_by_name):
    llm = LLMClient(mock=True)

    def agent_turn(name, system, history, max_tokens):
        return {"speech": "发言", "propose_end": False,
                "whiteboard_ops": ops_by_name.get(name, [])}, _usage()

    llm.agent_turn = agent_turn
    return llm


def test_editor_edits_whiteboard():
    llm = _wb_llm({"小明": [{"op": "append", "content": "# 结论"}]})
    runner = ConversationRunner(
        "x", "c", "t",
        _config(total_max_tokens=30, whiteboard_enabled=True,
                whiteboard_format="md", whiteboard_editors=["a0"]),
        llm,
    )
    runner.start()
    _wait_paused(runner)
    snap = runner.to_dict()
    assert "# 结论" in snap["whiteboard"]["content"]
    assert snap["whiteboard"]["rev"] >= 1
    assert snap["whiteboard"]["last_editor"] == "小明"
    # the editing turn is annotated
    assert any(m.get("wb_edited") for m in snap["messages"] if m["speaker"] == "小明")


def test_non_editor_cannot_edit_whiteboard():
    # 小明 (a0) tries to edit but only 小红 (a1) is an editor -> whiteboard stays empty.
    llm = _wb_llm({"小明": [{"op": "append", "content": "偷偷写入"}]})
    runner = ConversationRunner(
        "x", "c", "t",
        _config(total_max_tokens=30, whiteboard_enabled=True,
                whiteboard_format="md", whiteboard_editors=["a1"]),
        llm,
    )
    runner.start()
    _wait_paused(runner)
    snap = runner.to_dict()
    assert snap["whiteboard"]["content"] == ""
    assert snap["whiteboard"]["rev"] == 0


def test_build_system_includes_whiteboard_for_editor_only():
    llm = LLMClient(mock=True)
    runner = ConversationRunner(
        "x", "c", "t",
        _config(whiteboard_enabled=True, whiteboard_format="md", whiteboard_editors=["a0"]),
        llm,
    )
    runner.whiteboard_content = "现有白板内容XYZ"
    editor_sys = runner._build_system(runner.agents[0])
    other_sys = runner._build_system(runner.agents[1])
    assert '"whiteboard"' in editor_sys and "现有白板内容XYZ" in editor_sys
    assert '"whiteboard"' not in other_sys


def test_whiteboard_roundtrip():
    llm = _wb_llm({"小明": [{"op": "append", "content": "落地方案"}]})
    runner = ConversationRunner(
        "x", "c", "t",
        _config(total_max_tokens=30, whiteboard_enabled=True,
                whiteboard_format="html", whiteboard_editors=["a0"]),
        llm,
    )
    runner.start()
    _wait_paused(runner)
    payload = runner.to_dict()
    restored = ConversationRunner.from_payload(payload, llm)
    assert restored.whiteboard_enabled is True
    assert restored.whiteboard_format == "html"
    assert restored.whiteboard_editors == ["a0"]
    assert restored.whiteboard_content == payload["whiteboard"]["content"]
    assert "落地方案" in restored.whiteboard_content


# ---- Extend limits + resume from limit pause ----

def test_limit_pause_blocks_resume_until_extended():
    llm = LLMClient(mock=True)
    runner = ConversationRunner("x", "c", "t", _config(total_max_tokens=50), llm)
    runner.start()
    _wait_paused(runner)
    snap = runner.to_dict()
    assert snap["status"] == "paused"
    assert snap["paused_reason"] == "limit"
    assert snap["can_resume"] is False
    assert runner.resume() is False

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
    assert runner.human_say("请小刚先说", target="小刚") == "appended"
    assert runner._forced_next_idx == 2
    runner.update_limits(runner.to_dict()["total_output_tokens"] + 100000, None)
    assert runner.resume() is True
    time.sleep(0.15)
    runner.interrupt()
    _wait_paused(runner)
    msgs = runner.to_dict()["messages"]
    idx = next(i for i, m in enumerate(msgs) if m["role"] == "human" and m["content"] == "请小刚先说")
    following = [m for m in msgs[idx + 1:] if m["role"] == "agent"]
    assert following, "expected at least one agent turn after the human message"
    assert following[0]["speaker"] == "小刚"
    assert runner._forced_next_idx is None


def test_resolve_agent_idx_by_id_and_name():
    llm = LLMClient(mock=True)
    runner = ConversationRunner("x", "c", "t", _config(total_max_tokens=50), llm)
    assert runner._resolve_agent_idx("a1") == 1
    assert runner._resolve_agent_idx("小刚") == 2
    assert runner._resolve_agent_idx("不存在") is None
    assert runner._resolve_agent_idx(None) is None


def test_human_say_forces_next_speaker_while_running():
    # Slow turn so the reserve lands mid-run; the forced speaker must apply to the
    # turn right AFTER the human message, never to an in-flight agent turn.
    llm = LLMClient(mock=True)
    original = llm.agent_turn

    def slow_turn(name, system, history, max_tokens):
        time.sleep(0.03)
        return original(name, system, history, max_tokens)

    llm.agent_turn = slow_turn
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
