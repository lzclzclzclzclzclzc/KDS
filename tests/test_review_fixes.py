import sqlite3
import threading
import time
import uuid
from unittest.mock import patch

from flask import Flask

import app.db as db
import app.engine as engine
from app.engine import ConversationRunner
from app.llm import LLMClient


def _config(**overrides):
    config = {
        "agents": [
            {"id": "a0", "name": "甲"},
            {"id": "a1", "name": "乙"},
            {"id": "a2", "name": "丙"},
        ],
        "single_max_tokens": 40,
        "total_max_tokens": 100,
        "first_speaker": "a0",
        "scheduling_mode": "round_robin",
    }
    config.update(overrides)
    return config


def _runner(config=None, llm=None, conv_id=None):
    return ConversationRunner(
        conv_id or uuid.uuid4().hex,
        "config",
        "测试",
        config or _config(),
        llm or LLMClient(mock=True),
    )


def _api_module():
    # Importing the route module creates an LLM client; keep it offline in tests.
    original_init = LLMClient.__init__
    with patch.object(
        LLMClient, "__init__",
        lambda self, mock=None: original_init(self, mock=True),
    ):
        from app.routes import api
    return api


def test_restart_repairs_resume_fields_and_elapsed_time():
    uri = f"file:review-{uuid.uuid4().hex}?mode=memory&cache=shared"
    keeper = sqlite3.connect(uri, uri=True)
    keeper.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, config_id TEXT, "
        "name TEXT, payload TEXT, status TEXT, created_at TEXT, updated_at TEXT)"
    )

    def memory_connect(_path):
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    try:
        with patch.object(db, "_connect", memory_connect):
            resumable = _runner(_config(total_duration_seconds=200, total_max_tokens=None))
            payload = resumable.to_dict()
            payload.update(active_seconds=10, elapsed_seconds=130, can_resume=False)
            db.create_conversation(resumable.id, resumable.config_id, resumable.name, payload)

            at_limit = _runner(_config(total_duration_seconds=120, total_max_tokens=None))
            payload = at_limit.to_dict()
            payload.update(active_seconds=10, elapsed_seconds=130, can_resume=False)
            db.create_conversation(at_limit.id, at_limit.config_id, at_limit.name, payload)

            interrupted_vote = _runner()
            interrupted_vote.status = "paused"
            payload = interrupted_vote.to_dict()
            payload["votes"] = [{"id": "v1", "status": "running", "error": None}]
            db.create_conversation(
                interrupted_vote.id, interrupted_vote.config_id,
                interrupted_vote.name, payload, status="paused",
            )

            db.mark_stale_running_conversations()
            api = _api_module()
            app = Flask(__name__)
            app.register_blueprint(api.api_bp)
            client = app.test_client()
            restored = client.get(f"/api/conversations/{resumable.id}").get_json()
            limited = client.get(f"/api/conversations/{at_limit.id}").get_json()
            stale_vote = client.get(f"/api/conversations/{interrupted_vote.id}").get_json()

            assert restored["status"] == "paused"
            assert restored["can_resume"] is True
            assert restored["paused_reason"] == "manual"
            assert restored["active_seconds"] == 130
            assert restored["remaining_seconds"] == 70
            assert ConversationRunner.from_payload(restored, LLMClient(mock=True))._active_seconds == 130
            assert limited["can_resume"] is False
            assert limited["paused_reason"] == "limit"
            assert stale_vote["votes"][0]["status"] == "error"
    finally:
        keeper.close()


def test_concurrent_revival_reuses_one_runner():
    api = _api_module()
    saved = _runner().to_dict()
    saved["status"] = "paused"
    conv_id = saved["id"]
    loaded = []
    calls = []

    def get_record(_id):
        calls.append(_id)
        time.sleep(0.02)
        return saved

    with patch.object(api, "get_conversation", side_effect=get_record):
        threads = [threading.Thread(target=lambda: loaded.append(api._load_runner(conv_id, ("paused",))[0]))
                   for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            assert all(not thread.is_alive() for thread in threads)
            assert len(calls) == 1
            assert len(loaded) == 2 and loaded[0] is loaded[1]
        finally:
            with engine.RUNNERS_LOCK:
                engine.RUNNERS.pop(conv_id, None)


def test_running_snapshot_persists_current_active_segment():
    runner = _runner()
    runner._active_seconds = 10
    runner._segment_start = 100
    with patch("app.engine.time.time", return_value=130):
        snapshot = runner.to_dict()
    assert snapshot["active_seconds"] == 40
    assert ConversationRunner.from_payload(snapshot, LLMClient(mock=True))._active_seconds == 40


def test_random_first_speaker_survives_restart_before_first_turn():
    with patch("app.engine.random.randrange", side_effect=[0, 2]):
        runner = _runner(_config(first_speaker="random", scheduling_mode="willingness"))
        restored = ConversationRunner.from_payload(runner.to_dict(), LLMClient(mock=True))
    assert runner.first_idx == 0
    assert restored.first_idx == 0


def test_willingness_scores_share_one_log_snapshot():
    runner = _runner(_config(scheduling_mode="willingness"))
    runner.turn = 1
    runner.messages.append({"speaker": "甲", "content": "话题"})
    with patch.object(runner, "_log_text", wraps=runner._log_text) as log_text:
        runner._willingness_choose()
    assert log_text.call_count == 1
    assert runner.total_output_tokens == len(runner.agents)


def test_turn_snapshot_cannot_observe_half_updated_state():
    llm = LLMClient(mock=True)
    llm.agent_turn = lambda *_: (
        {"speech": "回复", "propose_end": False, "whiteboard_ops": []},
        {"completion_tokens": 5, "prompt_tokens": 2},
    )
    runner = _runner(_config(total_max_tokens=5), llm)
    entered = threading.Event()
    release = threading.Event()
    snapshot_ready = threading.Event()
    snapshots = []
    original = engine.update_heat

    def blocked_heat(heat, index, gamma):
        entered.set()
        assert release.wait(2)
        return original(heat, index, gamma)

    with patch.object(engine, "update_conversation", return_value=None), patch.object(
        engine, "update_heat", side_effect=blocked_heat
    ):
        try:
            runner.start()
            assert entered.wait(2)
            reader = threading.Thread(
                target=lambda: (snapshots.append(runner.to_dict()), snapshot_ready.set())
            )
            reader.start()
            assert not snapshot_ready.wait(0.05)
        finally:
            release.set()
            if runner._thread is not None:
                runner._thread.join(2)
            if "reader" in locals():
                reader.join(2)
    assert snapshot_ready.is_set()
    assert snapshots[0]["turn"] == 1
    assert len(snapshots[0]["messages"]) == 1
    assert snapshots[0]["total_output_tokens"] == 5
    assert snapshots[0]["total_prompt_tokens"] == 2


def test_mock_speech_does_not_use_process_hash():
    with patch("builtins.hash", side_effect=AssertionError("unstable hash used")):
        speech = LLMClient._mock_speak("甲", [])
    assert speech


def test_human_message_is_atomic_with_resume():
    llm = LLMClient(mock=True)
    llm.agent_turn = lambda *_: (
        {"speech": "回复", "propose_end": False, "whiteboard_ops": []},
        {"completion_tokens": 5, "prompt_tokens": 0},
    )
    runner = _runner(_config(total_max_tokens=5), llm)
    runner.status = "paused"
    entered = threading.Event()
    release = threading.Event()
    original = runner._append_message_nolock

    def append(*args, **kwargs):
        if args[0] == "human":
            entered.set()
            assert release.wait(2)
        original(*args, **kwargs)

    runner._append_message_nolock = append
    results = []
    with patch.object(engine, "update_conversation", return_value=None):
        human = threading.Thread(target=lambda: results.append(runner.human_say("插话")))
        human.start()
        assert entered.wait(2)
        resume = threading.Thread(target=lambda: results.append(runner.resume()))
        resume.start()
        release.set()
        human.join(2)
        resume.join(2)
        assert not human.is_alive() and not resume.is_alive()
        if runner._thread is not None:
            runner._thread.join(2)
    assert "appended" in results and True in results
    assert [(m["role"], m["round"]) for m in runner.messages] == [
        ("human", 0), ("agent", 1),
    ]


def test_vote_blocks_resume_summary_and_second_vote_and_counts_usage():
    entered = threading.Event()
    release = threading.Event()
    llm = LLMClient(mock=True)
    histories = []

    def vote(*args):
        histories.append(args[2])
        entered.set()
        assert release.wait(2)
        return {"choices": ["1"], "reason": "同意"}, {
            "completion_tokens": 12, "prompt_tokens": 7,
        }

    llm.vote = vote
    runner = _runner(llm=llm)
    runner.status = "paused"
    with patch.object(engine, "update_conversation", return_value=None):
        try:
            assert runner.start_vote("问题", ["是", "否"], 1) is not None
            assert entered.wait(2)
            assert runner.resume() is False
            assert runner.summarize_now() is False
            assert runner.start_vote("重复", ["是", "否"], 1) is None
            assert runner.human_say("中途插话") == "appended"
        finally:
            release.set()
            runner._vote_thread.join(2)
    assert not runner._vote_thread.is_alive()
    assert runner.votes[0]["status"] == "completed"
    assert histories == ["", "", ""]
    assert runner.total_output_tokens == 36
    assert runner.total_prompt_tokens == 21


def test_invalid_end_vote_abstains():
    llm = LLMClient(mock=True)
    llm.mock = False
    llm._call = lambda *_args, **_kwargs: (
        "无法按要求投票", {"completion_tokens": 2, "prompt_tokens": 1}
    )
    runner = _runner(llm=llm)
    with patch.object(engine, "update_conversation", return_value=None):
        assert runner._run_end_vote_inline() is False
    assert runner.votes[0]["status"] == "completed"
    assert runner.votes[0]["agreed"] is False
    assert runner.votes[0]["results"]["1"] == 0


def test_scheduler_parameters_are_validated():
    clean = _api_module()._clean_config
    for params in (
        {"tau": 0}, {"tau": -1}, {"tau": float("nan")},
        {"gamma": 0}, {"gamma": -1}, {"gamma": 1.1},
        {"lam": float("inf")},
    ):
        try:
            clean(_config(scheduler_params=params))
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid scheduler params: {params}")
    _, config = clean(_config(scheduler_params={"lam": 2, "tau": 0.5, "gamma": 1}))
    assert config["scheduler_params"]["tau"] == 0.5
    assert config["scheduler_params"]["gamma"] == 1.0
    for invalid in ([], "bad"):
        try:
            clean(_config(scheduler_params=invalid))
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid scheduler params: {invalid}")

    app = Flask(__name__)
    app.register_blueprint(_api_module().api_bp)
    response = app.test_client().post(
        "/api/configs", json=_config(scheduler_params={"tau": 0})
    )
    assert response.status_code == 400
    assert "tau" in response.get_json()["error"]


def test_legacy_agent_token_limits_map_to_uniform_limit():
    clean = _api_module()._clean_config
    legacy = _config()
    legacy.pop("single_max_tokens")
    for agent in legacy["agents"]:
        agent["max_tokens"] = 60
    _, normalized = clean(legacy)
    assert normalized["single_max_tokens"] == 60
    assert all("max_tokens" not in agent for agent in normalized["agents"])

    legacy["agents"][1]["max_tokens"] = 80
    try:
        clean(legacy)
    except ValueError as exc:
        assert "single_max_tokens" in str(exc)
    else:
        raise AssertionError("different legacy limits were silently ignored")
