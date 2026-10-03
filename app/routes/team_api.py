"""HTTP contract for immutable team definitions and scoped observations.

The browser controls whole runs and queues supplemental messages. Agent task
commands use the separate authenticated runtime channel, never these routes.
"""
from functools import wraps

from flask import Blueprint, current_app, jsonify, request

from app.domain.teams import TeamValidationError, validate_team_definition


team_api_bp = Blueprint("team_api", __name__)


def _service():
    service = current_app.extensions.get("team_service")
    if service is not None:
        return service
    from app.services.team_sessions import team_service
    return team_service


def _body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise TeamValidationError("请求正文必须是 JSON 对象")
    return value


def _page(*, events=False):
    values = {}
    for name, default, maximum in (("limit", 200 if events else 100, 1000),
                                   ("after", 0, None), ("before", None, None)):
        raw = request.args.get(name)
        if raw is None:
            if default is not None:
                values[name] = default
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise TeamValidationError(f"{name} 必须是整数") from None
        if value < 0 or (name == "limit" and (value == 0 or value > maximum)):
            raise TeamValidationError(f"{name} 超出允许范围")
        values[name] = value
    return values


def _version_arg():
    raw=request.args.get('version')
    if raw is None:
        return None
    try:
        value=int(raw)
    except ValueError:
        raise TeamValidationError('version 必须是正整数') from None
    if value<1:
        raise TeamValidationError('version 必须是正整数')
    return value


def _endpoint(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            value = fn(*args, **kwargs)
            if value is None:
                return jsonify({"error": "记录不存在"}), 404
            if isinstance(value, tuple):
                payload, status = value
                return jsonify(payload), status
            return jsonify(value)
        except TeamValidationError as exc:
            return jsonify({"error": str(exc), "errors": exc.errors}), 400
        except (KeyError, LookupError) as exc:
            return jsonify({"error": str(exc).strip("'") or "记录不存在"}), 404
        except (ValueError, RuntimeError) as exc:
            default_status = 500 if isinstance(exc, RuntimeError) else 400
            status = getattr(exc, "status_code", getattr(exc, "status", default_status))
            if not isinstance(status, int) or not 400 <= status <= 599:
                status = 400
            return jsonify({"error": str(exc)}), status
    return wrapped


@team_api_bp.get("/api/roles")
@_endpoint
def roles_list():
    return _service().list_roles()


@team_api_bp.get("/api/team-presets")
@_endpoint
def team_presets():
    from app.domain.team_presets import preset_catalog
    from app.domain.team_tools import available_team_tools
    return preset_catalog(available_team_tools())


@team_api_bp.post("/api/team-presets/roles/<key>")
@_endpoint
def preset_role_create(key):
    return _service().create_preset_role(key), 201


@team_api_bp.post("/api/team-presets/teams/<key>")
@_endpoint
def preset_team_create(key):
    body=request.get_json(silent=True)
    if (body is None and request.get_data()) or (body is not None and not isinstance(body,dict)):
        raise TeamValidationError('请求正文必须是 JSON 对象')
    return _service().create_preset_team(key,request_id=(body or {}).get('request_id')), 201


@team_api_bp.get('/api/team-presets/teams/<key>/preview')
@_endpoint
def preset_team_preview(key):
    return _service().preview_preset_team(key)


@team_api_bp.post("/api/roles")
@_endpoint
def roles_create():
    return _service().create_role(_body()), 201


@team_api_bp.get("/api/roles/<role_id>")
@_endpoint
def roles_get(role_id):
    return _service().get_role(role_id)


@team_api_bp.put("/api/roles/<role_id>")
@team_api_bp.post("/api/roles/<role_id>/versions")
@_endpoint
def roles_update(role_id):
    return _service().update_role(role_id, _body())


@team_api_bp.delete("/api/roles/<role_id>")
@_endpoint
def roles_archive(role_id):
    return _service().archive_role(role_id)


@team_api_bp.get("/api/roles/<role_id>/versions")
@_endpoint
def role_versions(role_id):
    return _service().list_role_versions(role_id)


@team_api_bp.get("/api/teams")
@_endpoint
def teams_list():
    return _service().list_teams()


@team_api_bp.post("/api/teams")
@_endpoint
def teams_create():
    return _service().create_team(_body()), 201


@team_api_bp.get("/api/teams/<team_id>")
@_endpoint
def teams_get(team_id):
    return _service().get_team(team_id)


@team_api_bp.put("/api/teams/<team_id>")
@team_api_bp.post("/api/teams/<team_id>/versions")
@_endpoint
def teams_update(team_id):
    return _service().update_team(team_id, _body())


@team_api_bp.get("/api/teams/<team_id>/versions")
@_endpoint
def team_versions(team_id):
    return _service().list_team_versions(team_id)


@team_api_bp.post("/api/teams/validate")
@_endpoint
def teams_validate():
    payload = _body()
    try:
        definition = validate_team_definition(payload)
    except TeamValidationError as exc:
        return {"valid": False, "errors": exc.errors, "roots": [], "rooms": []}
    return {"valid": True, "errors": [], "definition": definition,
            "roots": definition["roots"], "rooms": definition["rooms"]}


@team_api_bp.get("/api/teams/<team_id>/export")
@_endpoint
def team_export(team_id):
    return _service().export_team(team_id, version=request.args.get("version", type=int))


@team_api_bp.get('/api/teams/<team_id>/preview')
@_endpoint
def team_preview(team_id):
    return _service().preview_team(team_id,version=_version_arg())


@team_api_bp.post("/api/teams/import")
@_endpoint
def team_import():
    return _service().import_team(_body()), 201


@team_api_bp.get("/api/team-runs")
@_endpoint
def runs_list():
    return _service().list_runs()


@team_api_bp.post("/api/team-runs")
@_endpoint
def runs_create():
    return _service().create_run(_body()), 201


@team_api_bp.get("/api/team-runs/<run_id>")
@_endpoint
def run_snapshot(run_id):
    return _service().get_snapshot(run_id)


@team_api_bp.get('/api/team-runs/<run_id>/preview')
@_endpoint
def run_preview(run_id):
    return _service().preview_run(run_id)


@team_api_bp.get("/api/team-runs/<run_id>/agents")
@_endpoint
def run_agents(run_id):
    return _service().list_agents(run_id)


@team_api_bp.get("/api/team-runs/<run_id>/tasks")
@_endpoint
def run_tasks(run_id):
    return _service().list_tasks(run_id)


@team_api_bp.get("/api/team-runs/<run_id>/rooms")
@_endpoint
def run_rooms(run_id):
    return _service().list_rooms(run_id)


def _entity(run_id, kind, entity_id):
    records = getattr(_service(), f"list_{kind}")(run_id)
    return next((record for record in records if record.get("id") == entity_id), None)


@team_api_bp.get("/api/team-runs/<run_id>/agents/<entity_id>")
@_endpoint
def run_agent(run_id, entity_id):
    return _entity(run_id, "agents", entity_id)


@team_api_bp.get("/api/team-runs/<run_id>/tasks/<entity_id>")
@_endpoint
def run_task(run_id, entity_id):
    return _entity(run_id, "tasks", entity_id)


@team_api_bp.get("/api/team-runs/<run_id>/rooms/<entity_id>")
@_endpoint
def run_room(run_id, entity_id):
    return _entity(run_id, "rooms", entity_id)


@team_api_bp.get("/api/team-runs/<run_id>/events")
@_endpoint
def run_events(run_id):
    page = _page(events=True)
    page.pop("before", None)
    return _service().list_events(run_id, **page)


def _messages(run_id, **scope):
    page = _page()
    page.pop("after", None)
    return _service().list_messages(run_id, **scope, **page)


@team_api_bp.get("/api/team-runs/<run_id>/messages")
@_endpoint
def run_messages(run_id):
    scopes = {name: request.args[name] for name in ("instance_id", "room_id", "task_id") if request.args.get(name)}
    if len(scopes) > 1:
        raise TeamValidationError("消息历史只能指定一个观察对象")
    return _messages(run_id, **scopes)


@team_api_bp.get("/api/team-runs/<run_id>/agents/<entity_id>/messages")
@_endpoint
def agent_messages(run_id, entity_id):
    if _entity(run_id, "agents", entity_id) is None:
        return None
    return _messages(run_id, instance_id=entity_id)


@team_api_bp.get("/api/team-runs/<run_id>/tasks/<entity_id>/messages")
@_endpoint
def task_messages(run_id, entity_id):
    if _entity(run_id, "tasks", entity_id) is None:
        return None
    return _messages(run_id, task_id=entity_id)


@team_api_bp.get("/api/team-runs/<run_id>/rooms/<entity_id>/messages")
@_endpoint
def room_messages(run_id, entity_id):
    if _entity(run_id, "rooms", entity_id) is None:
        return None
    return _messages(run_id, room_id=entity_id)


@team_api_bp.get("/api/team-runs/<run_id>/agents/<entity_id>/tool-logs")
@_endpoint
def agent_tool_logs(run_id, entity_id):
    if _entity(run_id, "agents", entity_id) is None:
        return None
    return _service().list_tool_logs(run_id, instance_id=entity_id)


@team_api_bp.post("/api/team-runs/<run_id>/auxiliary")
@_endpoint
def run_auxiliary(run_id):
    payload = _body()
    if payload.get("kind") not in ("assist", "score", "vote"):
        raise TeamValidationError("辅助操作只能是 assist、score 或 vote")
    return _service().run_auxiliary(run_id, payload)


@team_api_bp.post("/api/team-runs/<run_id>/messages")
@_endpoint
def run_send_message(run_id):
    return _service().send_human_message(run_id, _body()), 201


@team_api_bp.post("/api/team-runs/<run_id>/pause")
@_endpoint
def run_pause(run_id):
    return _service().pause(run_id)


@team_api_bp.post("/api/team-runs/<run_id>/resume")
@_endpoint
def run_resume(run_id):
    payload = _body() if request.data else {}
    retry_uncertain = payload.get("retry_uncertain", False)
    if not isinstance(retry_uncertain, bool):
        raise TeamValidationError("retry_uncertain 必须是布尔值")
    return _service().resume(run_id, retry_uncertain=retry_uncertain)


@team_api_bp.route("/api/team-runs/<run_id>/limits", methods=["PUT", "POST"])
@_endpoint
def run_limits(run_id):
    return _service().update_limits(run_id, _body())


@team_api_bp.post("/api/team-runs/<run_id>/finalize")
@_endpoint
def run_finalize(run_id):
    return _service().finalize(run_id, _body())


@team_api_bp.post("/api/team-runs/<run_id>/agents/<instance_id>/save-role")
@_endpoint
def save_runtime_role(run_id, instance_id):
    return _service().save_role(run_id, instance_id, _body()), 201
