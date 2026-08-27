import uuid

from flask import Blueprint, jsonify, render_template, request

from app.assistant import AssistantManager
from app.config import DEFAULT_SINGLE_MAX_TOKENS
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
            }
        )
    single_max_tokens = int(payload.get("single_max_tokens") or DEFAULT_SINGLE_MAX_TOKENS)
    if single_max_tokens <= 0:
        raise ValueError("单人 max_token 必须大于 0")

    end_vote_enabled = bool(payload.get("end_vote_enabled"))
    raw_proposers = payload.get("end_vote_proposers") or []
    if not isinstance(raw_proposers, list):
        raise ValueError("可发起结束投票的角色必须是数组")
    valid_ids = {a["id"] for a in cleaned_agents} | {a["name"] for a in cleaned_agents}
    end_vote_proposers = [str(p) for p in raw_proposers if str(p) in valid_ids]
    if end_vote_enabled and not end_vote_proposers:
        raise ValueError("启用主动结束投票时，至少需要选定一个可发起的角色")
    try:
        end_vote_cooldown_turns = int(payload.get("end_vote_cooldown_turns") or 3)
    except (TypeError, ValueError):
        end_vote_cooldown_turns = 3
    if end_vote_cooldown_turns < 0:
        end_vote_cooldown_turns = 0

    whiteboard_enabled = bool(payload.get("whiteboard_enabled"))
    whiteboard_format = payload.get("whiteboard_format") or "md"
    if whiteboard_format not in ("md", "html"):
        raise ValueError("白板格式只能是 md 或 html")
    raw_wb_editors = payload.get("whiteboard_editors") or []
    if not isinstance(raw_wb_editors, list):
        raise ValueError("白板可编辑角色必须是数组")
    whiteboard_editors = [str(p) for p in raw_wb_editors if str(p) in valid_ids]
    if whiteboard_enabled and not whiteboard_editors:
        raise ValueError("启用白板时，至少需要选定一个可编辑的角色")

    config = {
        "shared_background": payload.get("shared_background") or "",
        "agents": cleaned_agents,
        "single_max_tokens": single_max_tokens,
        "total_max_tokens": payload.get("total_max_tokens") or None,
        "total_duration_seconds": payload.get("total_duration_seconds") or None,
        "first_speaker": payload.get("first_speaker") or "random",
        "scheduling_mode": payload.get("scheduling_mode") or "round_robin",
        "round_robin_order": payload.get("round_robin_order") or [],
        "scheduler_params": payload.get("scheduler_params") or {},
        "end_vote_enabled": end_vote_enabled,
        "end_vote_proposers": end_vote_proposers,
        "end_vote_cooldown_turns": end_vote_cooldown_turns,
        "whiteboard_enabled": whiteboard_enabled,
        "whiteboard_format": whiteboard_format,
        "whiteboard_editors": whiteboard_editors,
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
        "single_max_tokens": config.get("single_max_tokens") or DEFAULT_SINGLE_MAX_TOKENS,
        "total_max_tokens": config.get("total_max_tokens"),
        "total_duration_seconds": config.get("total_duration_seconds"),
        "first_speaker": config.get("first_speaker"),
        "scheduling_mode": config.get("scheduling_mode"),
        "round_robin_order": config.get("round_robin_order", []),
        "scheduler_params": config.get("scheduler_params", {}),
        "end_vote_enabled": config.get("end_vote_enabled", False),
        "end_vote_proposers": config.get("end_vote_proposers", []),
        "end_vote_cooldown_turns": config.get("end_vote_cooldown_turns", 3),
        "whiteboard_enabled": config.get("whiteboard_enabled", False),
        "whiteboard_format": config.get("whiteboard_format", "md"),
        "whiteboard_editors": config.get("whiteboard_editors", []),
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


@api_bp.post("/api/conversations/<conv_id>/votes")
def conversations_create_vote(conv_id):
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    raw_options = payload.get("options") or []
    if not isinstance(raw_options, list):
        return _err("投票选项必须是数组")
    options = [str(o).strip() for o in raw_options if str(o).strip()]
    if not question:
        return _err("投票题目不能为空")
    if len(options) < 2:
        return _err("投票至少需要 2 个选项")
    try:
        votes_per_person = int(payload.get("votes_per_person") or 1)
    except (TypeError, ValueError):
        return _err("每人票数必须是整数")
    if votes_per_person < 1:
        return _err("每人票数至少为 1")
    if votes_per_person > 20:
        return _err("每人票数不能超过 20")

    runner = RUNNERS.get(conv_id)
    if runner is None:
        record = get_conversation(conv_id)
        if record is None:
            return _err("对话不存在", 404)
        if record.get("status") not in ("paused", "completed"):
            return _err("当前状态不能发起投票", 409)
        runner = ConversationRunner.from_payload(record, _llm)
        RUNNERS[conv_id] = runner

    vote = runner.start_vote(question, options, votes_per_person)
    if vote is None:
        return _err("当前状态不能发起投票", 409)
    return jsonify(vote), 202


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
        if record.get("status") != "paused":
            return _err("该对话已结束，无法再发言", 409)
        runner = ConversationRunner.from_payload(record, _llm)
        RUNNERS[conv_id] = runner
    raw_target = payload.get("target")
    target = str(raw_target) if raw_target else None
    result = runner.human_say(content, target=target)
    if result is None:
        return _err("该对话已结束，无法再发言", 409)
    return jsonify({"ok": True, "mode": result})


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


@api_bp.post("/api/conversations/<conv_id>/limits")
def conversations_update_limits(conv_id):
    payload = request.get_json(silent=True) or {}

    def _parse_limit(key):
        raw = payload.get(key)
        if raw in (None, "", 0, "0"):
            return None
        value = int(raw)
        return value if value > 0 else None

    try:
        total_max_tokens = _parse_limit("total_max_tokens")
        total_duration_seconds = _parse_limit("total_duration_seconds")
    except (TypeError, ValueError):
        return _err("上限必须是整数")
    if total_max_tokens is None and total_duration_seconds is None:
        return _err("总输出 max_token 和总对话时长不能同时为无限，至少设置一个")

    runner = RUNNERS.get(conv_id)
    if runner is None:
        record = get_conversation(conv_id)
        if record is None:
            return _err("对话不存在", 404)
        if record.get("status") != "paused":
            return _err("当前状态不能修改上限", 409)
        runner = ConversationRunner.from_payload(record, _llm)
        RUNNERS[conv_id] = runner

    snapshot = runner.to_dict()
    used_tokens = snapshot.get("total_output_tokens") or 0
    elapsed = snapshot.get("elapsed_seconds") or 0
    if total_max_tokens is not None and total_max_tokens <= used_tokens:
        return _err(f"总输出 max_token 需大于已消耗的 {used_tokens}")
    if total_duration_seconds is not None and total_duration_seconds <= elapsed:
        return _err(f"总对话时长需大于已进行的 {int(elapsed)} 秒")

    if not runner.update_limits(total_max_tokens, total_duration_seconds):
        return _err("当前状态不能修改上限", 409)
    return jsonify(runner.to_dict())


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
