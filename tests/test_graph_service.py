import uuid
from unittest.mock import patch

import pytest
from flask import Flask

from app import db
from app.engine import RUNNERS, RUNNERS_LOCK, ConversationRunner
from app.llm import LLMClient
from app.services.conversations import conversation_service
from app.orchestration.checkpointer import close_savers


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import app.orchestration.checkpointer as checkpoint
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "business.db")
    monkeypatch.setattr(checkpoint, "LANGGRAPH_CHECKPOINT_PATH", tmp_path / "checkpoint.db")
    db.init_db(db.DB_PATH)
    init = LLMClient.__init__
    with patch.object(LLMClient, "__init__", lambda self, mock=None: init(self, mock=True)):
        from app.routes import api
    monkeypatch.setattr(api, "_llm", LLMClient(mock=True))
    app = Flask(__name__)
    app.register_blueprint(api.api_bp)
    ids = []
    yield app.test_client(), api, ids
    with RUNNERS_LOCK:
        for conv_id in ids:
            runner = RUNNERS.pop(conv_id, None)
            if runner:
                runner.interrupt()
                if runner._thread:
                    runner._thread.join(10)
                if runner._vote_thread:
                    runner._vote_thread.join(10)
    close_savers(tmp_path / "checkpoint.db")


def test_api_default_graph_and_explicit_legacy(api_client):
    client, api, ids = api_client
    cfg = {"agents": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}],
           "total_max_tokens": 1, "first_speaker": "a"}
    db.create_config("cfg", "配置", cfg)
    for backend in (None, "legacy"):
        request = {"config_id": "cfg"}
        if backend:
            request["orchestration_backend"] = backend
        response = client.post("/api/conversations", json=request)
        assert response.status_code == 201, response.get_json()
        value = response.get_json()
        ids.append(value["id"])
        assert value["orchestration_backend"] == (backend or "langgraph")
        RUNNERS[value["id"]]._thread.join(10)
        get = client.get("/api/conversations/" + value["id"])
        assert get.get_json()["status"] == "paused"
        assert "harness_logs" not in get.get_json()
    assert client.post("/api/conversations", json={"config_id": "cfg", "orchestration_backend": "bad"}).status_code == 400


def test_legacy_import_and_bidirectional_migration_preserve_reservation(api_client):
    client, api, ids = api_client
    conv_id = uuid.uuid4().hex
    ids.append(conv_id)
    runner = ConversationRunner(conv_id, "cfg", "旧历史", {"agent_backend": "direct",
        "agents": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}], "total_max_tokens": 100}, api._llm)
    runner.status = "paused"
    runner.pending_human_message = "带入预约"
    runner.pending_human_target = 1
    saved = runner.to_dict()
    saved.pop("orchestration_backend")
    db.create_conversation(conv_id, "cfg", "旧历史", saved, "paused")
    loaded, _ = api._load_runner(conv_id, ("paused",))
    assert type(loaded) is ConversationRunner
    response = client.post(f"/api/conversations/{conv_id}/orchestration", json={"orchestration_backend": "langgraph"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["pending_human_message"] == "带入预约"
    assert response.get_json()["agent_backend"] == "direct"
    graph = RUNNERS[conv_id]
    assert graph.repository.get_pending_command(conv_id)["payload"]["target"] == 1
    response = client.post(f"/api/conversations/{conv_id}/orchestration", json={"orchestration_backend": "legacy"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["pending_human_target"] == 1
    assert RUNNERS[conv_id].resume()
    RUNNERS[conv_id]._thread.join(10)
    assert RUNNERS[conv_id].messages[0]["content"] == "带入预约"


def test_delete_cleans_graph_business_and_checkpoints(api_client):
    client, api, ids = api_client
    conv_id = uuid.uuid4().hex
    runner = conversation_service.create_runner(conv_id, "cfg", "删除", {
        "agents": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}], "total_max_tokens": 1}, api._llm)
    db.create_conversation(conv_id, "cfg", "删除", runner.to_dict())
    RUNNERS[conv_id] = runner
    runner.start()
    runner._thread.join(10)
    assert list(runner.saver.list(None))
    response = client.delete(f"/api/conversations/{conv_id}")
    assert response.status_code == 200, response.get_json()
    assert db.get_conversation(conv_id) is None
    assert runner.repository.list_operations(conv_id) == []
    assert list(runner.saver.list(None)) == []
    assert client.get(f"/api/conversations/{conv_id}").status_code == 404
