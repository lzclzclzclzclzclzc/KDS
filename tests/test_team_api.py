"""Exercise the browser API boundary with an isolated service (no model calls)."""
from copy import deepcopy

import pytest
from flask import Flask

from app.domain.teams import validate_role, validate_team_definition
from app.repositories.teams import TeamConflict
from app.routes.team_api import team_api_bp


class BrowserService:
    def __init__(self):
        self.roles, self.teams, self.messages = {}, {}, []
        self.run = {"id": "run", "status": "paused", "event_seq": 1,
                    "agents": [{"id": "agent", "status": "waiting_children"}],
                    "tasks": [{"id": "task", "status": "waiting_children"}],
                    "rooms": [{"id": "room", "instance_ids": ["agent", "other"]}]}

    def list_roles(self):
        return list(self.roles.values())

    def create_role(self, body):
        self.roles["role"] = {"id": "role", "version": 1, **validate_role(body)}
        return self.roles["role"]

    def get_role(self, role_id):
        return self.roles.get(role_id)

    def update_role(self, role_id, body):
        if body.get("base_version") != self.roles[role_id]["version"]:
            raise TeamConflict("角色版本已变更")
        self.roles[role_id] = {"id": role_id, "version": 2, **validate_role(body)}
        return self.roles[role_id]

    def archive_role(self, role_id):
        self.roles[role_id]["archived"] = True
        return {"ok": True}

    def list_role_versions(self, role_id):
        return [self.roles[role_id]]

    def list_teams(self):
        return list(self.teams.values())

    def create_team(self, body):
        self.teams["team"] = {"id": "team", "version": 1,
                              "definition": validate_team_definition(body)}
        return self.teams["team"]

    def get_team(self, team_id):
        return self.teams.get(team_id)

    def list_team_versions(self, team_id):
        return [self.teams[team_id]]

    def export_team(self, team_id, version=None):
        return {"schema_version": 1, "definition": self.teams[team_id]["definition"]}

    def import_team(self, body):
        return self.create_team(body)

    def list_runs(self):
        return [self.run]

    def create_run(self, body):
        return {**self.run, "request_id": body["request_id"]}

    def get_snapshot(self, run_id):
        return deepcopy(self.run) if run_id == "run" else None

    def list_agents(self, run_id):
        return self.run["agents"]

    def list_tasks(self, run_id):
        return self.run["tasks"]

    def list_rooms(self, run_id):
        return self.run["rooms"]

    def list_events(self, run_id, after=0, limit=200):
        return {"events": [{"seq": 1, "type": "created"}] if after < 1 else [],
                "event_seq": self.run["event_seq"]}

    def list_messages(self, run_id, **scope):
        return {"messages": self.messages, "scope": scope}

    def send_human_message(self, run_id, body):
        if self.run["status"] == "completed":
            raise TeamConflict("已完成运行只读")
        record = {"id": f"m{len(self.messages)}", "status": "queued", **body}
        self.messages.append(record)
        return record

    def pause(self, run_id):
        self.run["status"] = "paused"
        return self.run

    def resume(self, run_id, retry_uncertain=False):
        self.run["status"] = "running"
        return self.run

    def update_limits(self, run_id, body):
        self.run["limits"] = body
        return self.run

    def finalize(self, run_id, body):
        self.run["status"] = "completed"
        return self.run

    def save_role(self, run_id, instance_id, body):
        return {"id": "saved", "version": 1, "source_instance_id": instance_id, **body}

    def list_tool_logs(self, run_id, instance_id=None):
        return [{"instance_id": instance_id, "detail": "Public tool output"}]

    def run_auxiliary(self, run_id, payload):
        return {"kind": payload["kind"], "result": "Saved system response"}


@pytest.fixture
def client():
    app = Flask(__name__)
    service = BrowserService()
    app.extensions["team_service"] = service
    app.register_blueprint(team_api_bp)
    return app.test_client(), service


def team():
    return {"name": "测试", "nodes": [{"id": "node", "role_id": "role", "role_version": 1}], "edges": []}


def test_roles_and_team_definitions_round_trip(client):
    browser, _ = client
    role = browser.post("/api/roles", json={"name": "角色", "tools": []})
    assert role.status_code == 201
    assert browser.get("/api/roles").get_json()[0]["id"] == "role"
    assert browser.get("/api/roles/role/versions").get_json()[0]["version"] == 1
    saved = browser.post("/api/teams", json=team())
    assert saved.status_code == 201
    assert browser.get("/api/teams").get_json()[0]["id"] == "team"
    export = browser.get("/api/teams/team/export").get_json()
    assert export["schema_version"] == 1
    assert browser.post("/api/teams/import", json=export).status_code == 201


def test_definition_preview_has_structured_errors_and_no_mutation(client):
    browser, service = client
    assert browser.post("/api/teams/validate", json=team()).get_json()["roots"] == ["node"]
    value = team()
    value["edges"] = [{"id": "bad", "type": "task", "source": "node", "target": "node"}]
    result = browser.post("/api/teams/validate", json=value)
    assert result.status_code == 200
    assert result.get_json()["valid"] is False
    assert result.get_json()["errors"][0]["edge_ids"] == ["bad"]
    assert service.teams == {}


def test_version_conflict_is_409_and_missing_record_is_404(client):
    browser, _ = client
    browser.post("/api/roles", json={"name": "角色"})
    assert browser.put("/api/roles/role", json={"name": "新", "base_version": 0}).status_code == 409
    assert browser.put("/api/roles/role", json={"name": "新", "base_version": 1}).status_code == 200
    assert browser.get("/api/roles/missing").status_code == 404
    assert browser.get("/api/team-runs/missing").status_code == 404


@pytest.mark.parametrize("body", [None, [], "text", True])
def test_mutations_require_json_object(client, body):
    browser, _ = client
    response = browser.post("/api/roles", json=body)
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_agent_supplement_preserves_waiting_state_and_every_accepted_message(client):
    browser, service = client
    before = deepcopy(service.run["tasks"])
    for content in ("信息一", "信息二"):
        response = browser.post("/api/team-runs/run/messages",
                                json={"instance_id": "agent", "content": content})
        assert response.status_code == 201
        assert response.get_json()["status"] == "queued"
    assert service.run["tasks"] == before
    assert [m["content"] for m in service.messages] == ["信息一", "信息二"]
    assert browser.post("/api/team-runs/run/finalize", json={}).status_code == 200
    assert browser.post("/api/team-runs/run/messages", json={"instance_id": "agent", "content": "迟到"}).status_code == 409


def test_scoped_history_and_incremental_event_pagination(client):
    browser, _ = client
    result = browser.get("/api/team-runs/run/rooms/room/messages?before=9&limit=20")
    assert result.status_code == 200
    assert result.get_json()["scope"] == {"room_id": "room", "before": 9, "limit": 20}
    assert browser.get("/api/team-runs/run/agents/missing/messages").status_code == 404
    assert browser.get("/api/team-runs/run/events?after=1").get_json()["events"] == []
    assert browser.get("/api/team-runs/run/messages?instance_id=agent&room_id=room").status_code == 400
    for query in ("limit=0", "limit=1001", "after=-1", "before=bad"):
        assert browser.get(f"/api/team-runs/run/messages?{query}").status_code == 400


def test_control_routes_and_no_browser_task_management_routes(client):
    browser, _ = client
    assert browser.post("/api/team-runs/run/resume").get_json()["status"] == "running"
    assert browser.post("/api/team-runs/run/pause").get_json()["status"] == "paused"
    assert browser.put("/api/team-runs/run/limits", json={"total_max_tokens": 42}).get_json()["limits"]["total_max_tokens"] == 42
    assert browser.post("/api/team-runs/run/agents/agent/save-role", json={"name": "保存"}).status_code == 201
    for route in ("/api/team-runs/run/tasks", "/api/team-runs/run/tasks/task/cancel",
                  "/api/team-runs/run/agents/agent/delegate"):
        assert browser.post(route, json={}).status_code in {404, 405}


def test_versions_and_auxiliary_routes_match_browser_contract(client):
    browser, _ = client
    browser.post("/api/roles", json={"name": "角色"})
    assert browser.post("/api/roles/role/versions", json={"name": "第二版", "base_version": 1}).status_code == 200
    assert browser.get("/api/team-runs/run/agents/agent/tool-logs").get_json()[0]["instance_id"] == "agent"
    assert browser.get("/api/team-runs/run/agents/missing/tool-logs").status_code == 404
    for kind in ("assist", "score", "vote"):
        assert browser.post("/api/team-runs/run/auxiliary", json={"kind": kind}).get_json()["kind"] == kind
    assert browser.post("/api/team-runs/run/auxiliary", json={"kind": "unbounded"}).status_code == 400
