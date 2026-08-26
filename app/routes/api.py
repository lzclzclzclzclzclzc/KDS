import uuid

from flask import Blueprint, jsonify, render_template, request

from app.assistant import AssistantManager
from app.db import (
    create_config,
    create_conversation,
    delete_config,
    delete_conversation,
    get_config,
    get_conversation,
    list_configs,
    list_conversations,
    update_config,
)
from app.engine import RUNNERS, ConversationRunner
from app.llm import LLMClient

api_bp = Blueprint("api", __name__)

_llm = LLMClient()
_assistant = AssistantManager(_llm)


def _err(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _clean_config(payload: dict) -> tuple[str, dict]:
    name = (payload.get("name") or "未命名配置").strip() or "未命名配置"
    agents = payload.get("agents") or []
    if not isinstance(agents, list):
        raise ValueError("agents 必须是数组")
    cleaned_agents = []
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            raise ValueError("每个角色必须是对象")
        cleaned_agents.append(
            {
                "id": a.get("id") or f"a{i}",
                "name": (a.get("name") or f"角色{i + 1}").strip(),
                "system_prompt": a.get("system_prompt") or "",
                "visibility": a.get("visibility") or [],
                "max_tokens": int(a.get("max_tokens") or 300),
            }
        )
    config = {
        "shared_background": payload.get("shared_background") or "",
        "agents": cleaned_agents,
        "total_max_tokens": payload.get("total_max_tokens") or None,
        "total_duration_seconds": payload.get("total_duration_seconds") or None,
        "first_speaker": payload.get("first_speaker") or "random",
        "scheduling_mode": payload.get("scheduling_mode") or "round_robin",
        "round_robin_order": payload.get("round_robin_order") or [],
        "scheduler_params": payload.get("scheduler_params") or {},
    }
    if config["total_max_tokens"] is None and config["total_duration_seconds"] is None:
        raise ValueError("总输出 max_token 和总对话时长不能同时为无限，至少设置一个")
    return name, config


@api_bp.get("/")
def index():
    return render_template("index.html")


# ---- Configs ----

@api_bp.get("/api/configs")
def configs_list():
    return jsonify(list_configs())


@api_bp.post("/api/configs")
def configs_create():
    payload = request.get_json(silent=True) or {}
    try:
        name, config = _clean_config(payload)
    except ValueError as exc:
        return _err(str(exc))
    record_id = uuid.uuid4().hex
    record = create_config(record_id, name, config)
    return jsonify(record), 201


@api_bp.get("/api/configs/<record_id>")
def configs_get(record_id):
    record = get_config(record_id)
    if record is None:
        return _err("配置不存在", 404)
    return jsonify(record)


@api_bp.put("/api/configs/<record_id>")
def configs_update(record_id):
    payload = request.get_json(silent=True) or {}
    try:
        name, config = _clean_config(payload)
    except ValueError as exc:
        return _err(str(exc))
    record = update_config(record_id, name, config)
    if record is None:
        return _err("配置不存在", 404)
    return jsonify(record)


@api_bp.delete("/api/configs/<record_id>")
def configs_delete(record_id):
    ok = delete_config(record_id)
    if not ok:
        return _err("配置不存在", 404)
    return jsonify({"ok": True})


# ---- Conversations ----

@api_bp.get("/api/conversations")
def conversations_list():
    items = []
    for c in list_conversations():
        items.append(
            {
                "id": c["id"],
                "config_id": c["config_id"],
                "name": c["name"],
                "status": c["status"],
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
                "turn": c.get("turn", 0),
                "agents": c.get("agents", []),
            }
        )
    return jsonify(items)


@api_bp.post("/api/conversations")
def conversations_create():
    payload = request.get_json(silent=True) or {}
    config_id = payload.get("config_id")
    config = get_config(config_id) if config_id else None
    if config is None:
        return _err("找不到对应的配置，请先保存配置")
    if len(config.get("agents", [])) < 2:
        return _err("至少需要 2 个角色才能开始群聊")
    if not config.get("total_max_tokens") and not config.get("total_duration_seconds"):
        return _err("总输出 max_token 和总对话时长不能同时为无限，至少设置一个")

    conv_id = uuid.uuid4().hex
    name = payload.get("name") or config.get("name", "对话")
    config_payload = {
        "agents": config.get("agents", []),
        "shared_background": config.get("shared_background", ""),
        "total_max_tokens": config.get("total_max_tokens"),
        "total_duration_seconds": config.get("total_duration_seconds"),
        "first_speaker": config.get("first_speaker"),
        "scheduling_mode": config.get("scheduling_mode"),
        "round_robin_order": config.get("round_robin_order", []),
        "scheduler_params": config.get("scheduler_params", {}),
    }
    runner = ConversationRunner(conv_id, config_id, name, config_payload, _llm)
    initial = runner.to_dict()
    create_conversation(conv_id, config_id, name, initial, status="running")
    RUNNERS[conv_id] = runner
    runner.start()
    return jsonify(initial), 201


@api_bp.get("/api/conversations/<conv_id>")
def conversations_get(conv_id):
    if conv_id in RUNNERS:
        return jsonify(RUNNERS[conv_id].to_dict())
    record = get_conversation(conv_id)
    if record is None:
        return _err("对话不存在", 404)
    return jsonify(record)


@api_bp.post("/api/conversations/<conv_id>/reserve")
def conversations_reserve(conv_id):
    payload = request.get_json(silent=True) or {}
    content = (payload.get("content") or "").strip()
    if not content:
        return _err("发言内容不能为空")
    runner = RUNNERS.get(conv_id)
    if runner is None:
        record = get_conversation(conv_id)
        if record is None:
            return _err("对话不存在", 404)
        return _err("该对话已结束，无法再发言", 409)
    runner.reserve(content)
    return jsonify({"ok": True})


@api_bp.post("/api/conversations/<conv_id>/interrupt")
def conversations_interrupt(conv_id):
    runner = RUNNERS.get(conv_id)
    if runner is None:
        return _err("对话不存在", 404)
    runner.interrupt()
    return jsonify({"ok": True})


@api_bp.post("/api/conversations/<conv_id>/resume")
def conversations_resume(conv_id):
    runner = RUNNERS.get(conv_id)
    if runner is None:
        record = get_conversation(conv_id)
        if record is None:
            return _err("对话不存在", 404)
        if record.get("status") != "paused":
            return _err("该对话无法恢复", 409)
        runner = ConversationRunner.from_payload(record, _llm)
        RUNNERS[conv_id] = runner
    if not runner.resume():
        return _err("该对话当前无法恢复（可能已结束或正在运行）", 409)
    return jsonify({"ok": True, "status": runner.to_dict()["status"]})


@api_bp.post("/api/conversations/<conv_id>/summarize")
def conversations_summarize(conv_id):
    runner = RUNNERS.get(conv_id)
    if runner is None:
        record = get_conversation(conv_id)
        if record is None:
            return _err("对话不存在", 404)
        if record.get("status") != "paused":
            return _err("该对话无法总结", 409)
        runner = ConversationRunner.from_payload(record, _llm)
        RUNNERS[conv_id] = runner
    if not runner.summarize_now():
        return _err("该对话当前无法总结（可能已结束或正在运行）", 409)
    return jsonify(runner.to_dict())


@api_bp.delete("/api/conversations/<conv_id>")
def conversations_delete(conv_id):
    runner = RUNNERS.pop(conv_id, None)
    if runner is not None:
        runner.interrupt()
    ok = delete_conversation(conv_id)
    if not ok:
        return _err("对话不存在", 404)
    return jsonify({"ok": True})


# ---- Assistant ----

@api_bp.post("/api/assistant/chat")
def assistant_chat():
    payload = request.get_json(silent=True) or {}
    session_id = payload.get("session_id")
    draft = payload.get("draft") or {}
    if isinstance(draft, str):
        import json as _json

        try:
            draft = _json.loads(draft)
        except Exception:
            draft = {}
    if not isinstance(draft, dict):
        draft = {}
    message = (payload.get("message") or "").strip()
    if not message:
        return _err("消息不能为空")
    result = _assistant.chat(session_id, draft, message)
    return jsonify(result)
