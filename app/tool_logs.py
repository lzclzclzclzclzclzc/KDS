"""Small, display-only records derived from DSH tool events, never model context."""
import hashlib
import json
import re
from datetime import datetime, timezone


MAX_TOOL_LOGS = 200


def tool_log_index(logs):
    """Lightweight entries for collapsed groups; omit argument/result bodies."""
    fields = {"id", "agent_id", "agent_name", "turn", "step", "tool", "status", "started_at", "finished_at"}
    return [{key: value for key, value in entry.items() if key in fields} for entry in logs]


def log_text(value, limit, secrets=()):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    # Cover common credentials in JSON, env files and HTTP headers as well as
    # the configured keys. This is best-effort masking, not arbitrary DLP.
    text = re.sub(
        r'''(?im)(["']?\b(?:[\w-]*(?:api[_-]?key|access[_-]?token|secret|password)|authorization|token)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n,}]+)''',
        r'\1"[已隐藏]"', text,
    )
    return text[:limit] + ("\n…（内容过长，已截取）" if len(text) > limit else "")


def tool_log_update(event, run_id, secrets=()):
    kind = event.get("type")
    if kind not in {"tool/call", "tool/result"}:
        return None
    data = event.get("data") or {}
    message = data.get("message") or {}
    blocks = message.get("content") or []
    nested = [b for b in blocks if b.get("type") == "tool-result"]
    call_id = (data.get("callId") or message.get("toolCallId")
               or (message.get("source") or {}).get("callId")
               or next((b.get("toolCallId") for b in nested if b.get("toolCallId")), None))
    if not call_id:
        call_id = "event-" + str(next(iter(event.get("sourceEventSeqs") or []), event.get("seq")))
    identity = f"{run_id}:{data.get('step')}:{call_id}"
    try:
        timestamp = datetime.fromtimestamp(float(event["time"]) / 1000, timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        timestamp = datetime.now(timezone.utc).isoformat()
    entry = {"id": hashlib.sha256(identity.encode()).hexdigest()[:24], "step": data.get("step")}
    if kind == "tool/call":
        arguments = data.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                pass
        entry.update(tool=log_text(data.get("name") or "未知工具", 100, secrets),
                     arguments=log_text(arguments, 2000, secrets),
                     started_at=timestamp, status="running")
    else:
        texts = []
        for block in blocks:
            content = block.get("content", []) if block.get("type") == "tool-result" else [block]
            texts.extend(b["text"] for b in content
                         if b.get("type") == "text" and isinstance(b.get("text"), str))
        failed = bool(message.get("isError")) or any(b.get("isError") for b in nested)
        entry.update(result=log_text("\n".join(texts) or "（工具未返回文本内容）", 6000, secrets),
                     finished_at=timestamp, status="error" if failed else "completed")
    return entry
