import json
import re
import uuid
from typing import Optional

from app.llm import LLMClient


def _build_system(draft: dict) -> str:
    compact = {
        "shared_background": draft.get("shared_background", ""),
        "agents": [
            {
                "name": a.get("name"),
                "system_prompt": a.get("system_prompt", ""),
                "visibility": a.get("visibility", []),
                "max_tokens": a.get("max_tokens"),
            }
            for a in draft.get("agents", [])
        ],
    }
    return (
        "你是一个多智能体群聊（“侃大山”）的配置助手。用户正在编辑群聊配置，"
        "你需要帮助用户编写或修改各个角色的 system prompt 以及共享背景。\n\n"
        "当前配置草稿如下（JSON）：\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
        + "\n\n你的职责：\n"
        "1. 与用户多轮对话，澄清需求；\n"
        "2. 根据用户要求，提出对角色 system prompt、可见性、单次发言 token 上限，"
        "或共享背景的具体修改。\n\n"
        "你必须始终只输出一个 JSON 对象，格式如下：\n"
        '{"reply": "给用户的自然语言说明", '
        '"proposal": {"agents": {"<角色名>": {"system_prompt": "...", "visibility": [...], "max_tokens": 数字}}, '
        '"shared_background": "..."}}\n\n'
        "- proposal 可以为 null，表示本轮只是在澄清问题、没有修改建议。\n"
        "- agents 只包含需要修改的角色，键必须使用草稿中已有的角色名。\n"
        "- 只有在需要修改共享背景时，才在 proposal 中给出 shared_background 字段。\n"
        "- 不要新增或删除角色，除非用户明确要求。"
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


class AssistantManager:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.sessions: dict[str, list[dict]] = {}

    def chat(self, session_id: Optional[str], draft: dict, message: str) -> dict:
        if not session_id or session_id not in self.sessions:
            session_id = session_id or uuid.uuid4().hex
            self.sessions[session_id] = [{"role": "system", "content": _build_system(draft)}]
        else:
            # Refresh system context with the latest draft so edits stay grounded.
            self.sessions[session_id][0] = {"role": "system", "content": _build_system(draft)}

        self.sessions[session_id].append({"role": "user", "content": message})
        content, _usage = self.llm.assist(list(self.sessions[session_id]))
        self.sessions[session_id].append({"role": "assistant", "content": content})

        reply = ""
        proposal = None
        try:
            data = _extract_json(content)
            reply = data.get("reply", "")
            proposal = data.get("proposal")
        except Exception:
            reply = content

        return {"session_id": session_id, "reply": reply, "proposal": proposal}

