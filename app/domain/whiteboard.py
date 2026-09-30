"""Whiteboard and end-proposal permissions; no persistence or model calls."""


def is_proposer(runner, agent: dict) -> bool:
    if not runner.end_vote_enabled:
        return False
    return agent["id"] in runner.end_vote_proposers or agent["name"] in runner.end_vote_proposers


def can_edit(runner, agent: dict) -> bool:
    if not runner.whiteboard_enabled:
        return False
    return agent["id"] in runner.whiteboard_editors or agent["name"] in runner.whiteboard_editors


def apply_ops(content: str, ops: list) -> str:
    """Apply the existing incremental protocol, ignoring unknown/no-match ops."""
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").lower()
        if not kind:
            if "find" in op or "replace" in op:
                kind = "replace"
            elif "prepend" in op:
                kind = "prepend"
            elif "append" in op or "content" in op:
                kind = "append"
        if kind == "set":
            content = str(op.get("content", ""))
        elif kind == "append":
            text = str(op.get("content", op.get("append", "")) or "")
            if text:
                sep = "\n" if content and not content.endswith("\n") else ""
                content = content + sep + text
        elif kind == "prepend":
            text = str(op.get("content", op.get("prepend", "")) or "")
            if text:
                sep = "\n" if content and not text.endswith("\n") else ""
                content = text + sep + content
        elif kind == "replace":
            find = str(op.get("find", "") or "")
            repl = str(op.get("replace", op.get("with", op.get("content", ""))) or "")
            if find and find in content:
                content = content.replace(find, repl, 1)
    return content
