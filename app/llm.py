import json
import random
import re
from typing import Optional

from app.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MOCK,
    LLM_MODEL,
    LLM_SCORE_MAX_TOKENS,
    LLM_SCORE_TEMPERATURE,
    LLM_TEMPERATURE,
    LLM_TIMEOUT,
)


def _score_from_object(data: dict, default: int) -> int:
    score = data.get("score") if isinstance(data, dict) else None
    if isinstance(score, bool):
        return default
    if isinstance(score, (int, float)):
        return max(0, min(100, int(score)))
    if isinstance(score, str):
        nums = re.findall(r"-?\d+", score)
        if nums:
            return max(0, min(100, int(nums[0])))
    return default


def _parse_score(text: str, default: int = 50) -> int:
    text = (text or "").strip()
    # 1) The whole response is a JSON object like {"score": 65}.
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "score" in data:
                return _score_from_object(data, default)
        except Exception:
            pass
    # 2) A JSON object is embedded somewhere in the text.
    for match in re.finditer(r"\{[^{}]*\}", text):
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and "score" in data:
                return _score_from_object(data, default)
        except Exception:
            continue
    # 3) Fall back to the first integer found.
    nums = re.findall(r"-?\d+", text)
    if nums:
        return max(0, min(100, int(nums[0])))
    return default


def _extract_json(text: str):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except Exception:
        return None


def _parse_vote(text: str, options: list[str], votes_per_person: int) -> tuple[list[str], str]:
    data = _extract_json(text)
    raw = []
    reason = ""
    if isinstance(data, dict):
        reason = str(data.get("reason") or "").strip()
        for key in ("choices", "votes", "selections", "selection"):
            if isinstance(data.get(key), list):
                raw = data[key]
                break
        if not raw:
            for key in ("choice", "vote", "selection"):
                if data.get(key) is not None:
                    raw = [data[key]]
                    break

    n = len(options)
    labels = [str(i + 1) for i in range(n)]
    text_to_label = {str(option).strip(): str(i + 1) for i, option in enumerate(options)}

    def map_item(item):
        if isinstance(item, dict):
            for key in ("choice", "option", "vote", "selection"):
                if key in item:
                    item = item[key]
                    break
            else:
                item = None
        if item is None:
            return None
        value = str(item).strip()
        if value in labels:
            return value
        if value in text_to_label:
            return text_to_label[value]
        match = re.search(r"\d+", value)
        if match and match.group(0) in labels:
            return match.group(0)
        return None

    choices = []
    for item in raw:
        choice = map_item(item)
        if choice:
            choices.append(choice)

    if not choices and votes_per_person > 0:
        choices = [labels[0]] * votes_per_person
    while len(choices) < votes_per_person:
        choices.append(choices[0])
    return choices[:votes_per_person], reason


class LLMClient:
    """Thin OpenAI-compatible client with an offline mock mode."""

    def __init__(self, mock: Optional[bool] = None):
        self.model = LLM_MODEL
        self.mock = LLM_MOCK if mock is None else mock
        self.temperature = LLM_TEMPERATURE
        self.score_temperature = LLM_SCORE_TEMPERATURE
        self._client = None
        if not self.mock:
            if not LLM_API_KEY:
                raise RuntimeError(
                    "LLM_API_KEY is empty and LLM_MOCK is false. "
                    "Set LLM_API_KEY in .env, or set LLM_MOCK=true for offline mode."
                )
            from openai import OpenAI

            self._client = OpenAI(
                base_url=LLM_BASE_URL,
                api_key=LLM_API_KEY,
                timeout=LLM_TIMEOUT,
            )

    def _call(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: Optional[int],
        json_mode: bool = False,
    ) -> tuple[str, dict]:
        if self._client is None:
            raise RuntimeError("LLM client is not initialized")
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        # max_tokens=None means "no cap" — omit it so the model uses its default max.
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception:
            # Some OpenAI-compatible providers reject response_format; retry without it.
            if json_mode and "response_format" in kwargs:
                kwargs.pop("response_format", None)
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise
        content = resp.choices[0].message.content or ""
        usage = resp.usage
        usage_dict = {
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "total_tokens": usage.total_tokens if usage else 0,
        }
        return content.strip(), usage_dict

    # ---- Public purpose-specific methods (each has a mock path) ----

    def willingness_score(self, name: str, system: str, history_text: str, round_no: int) -> tuple[int, dict]:
        if self.mock:
            # Deterministic-ish but varied scores so softmax sampling is exercised.
            seed = (sum(ord(c) for c in name) * 37 + round_no * 13) % 100
            value = (seed * 7 + round_no * 11) % 101
            return value, {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1}
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    history_text
                    + "\n\n请评估你当前发言的意愿程度，只输出一个 JSON 对象，格式："
                      '{"score": 0到100的整数}，其中 100 表示非常想发言，0 表示完全不想。'
                ),
            },
        ]
        content, usage = self._call(messages, self.score_temperature, max_tokens=LLM_SCORE_MAX_TOKENS, json_mode=True)
        return _parse_score(content), usage

    def speak(self, name: str, system: str, history: list[dict], max_tokens: int) -> tuple[str, dict]:
        if self.mock:
            content = self._mock_speak(name, history)
            return content, {"prompt_tokens": 0, "completion_tokens": len(content), "total_tokens": len(content)}
        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append(
            {"role": "user", "content": f"轮到 {name} 发言。请直接输出你的发言内容，不要加任何前缀。"}
        )
        content, usage = self._call(messages, self.temperature, max_tokens=max_tokens)
        return content, usage

    def summarize(self, log_text: str) -> tuple[str, dict]:
        if self.mock:
            return self._mock_summary(log_text), {
                "prompt_tokens": 0,
                "completion_tokens": 120,
                "total_tokens": 120,
            }
        system = (
            "你是一个专业的对话总结助手。请客观、简洁地总结这场群聊讨论的过程、"
            "各方的主要观点，以及最终达成的结论或遗留的问题。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": log_text + "\n\n请总结这场对话。"},
        ]
        content, usage = self._call(messages, 0.3, max_tokens=1200)
        return content, usage

    def vote(
        self,
        name: str,
        system: str,
        history_text: str,
        question: str,
        options: list[str],
        votes_per_person: int,
    ) -> tuple[dict, dict]:
        if self.mock:
            return self._mock_vote(name, options, votes_per_person)
        options_text = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "以下是当前对话记录：\n"
                    + history_text
                    + "\n\n请投票：\n"
                    + question
                    + "\n\n选项：\n"
                    + options_text
                    + f"\n\n你有 {votes_per_person} 票。请只输出 JSON 对象："
                      '{"choices": ["选项编号"], "reason": "简短理由"}。'
                ),
            },
        ]
        content, usage = self._call(messages, self.score_temperature, max_tokens=700, json_mode=True)
        choices, reason = _parse_vote(content, options, votes_per_person)
        return {"choices": choices, "reason": reason}, usage

    def assist(self, messages: list[dict]) -> tuple[str, dict]:
        if self.mock:
            return self._mock_assist(messages)
        content, usage = self._call(messages, 0.3, max_tokens=None, json_mode=True)
        return content, usage

    # ---- Mock helpers ----

    @staticmethod
    def _mock_vote(name: str, options: list[str], votes_per_person: int) -> tuple[dict, dict]:
        n = len(options)
        seed = (sum(ord(c) for c in name) * 13 + votes_per_person * 7) % 100
        choices = [str((seed + j) % n + 1) for j in range(votes_per_person)]
        return {"choices": choices, "reason": "mock vote"}, {
            "prompt_tokens": 0,
            "completion_tokens": 12,
            "total_tokens": 12,
        }

    @staticmethod
    def _mock_speak(name: str, history: list[dict]) -> str:
        round_no = len(history) + 1
        phrases = [
            "我觉得这个问题很有意思，我们可以从几个角度看。",
            "我同意前面说的，不过我想补充一个细节。",
            "让我先整理一下目前的思路，再给出我的看法。",
            "这一点我有不同的意见，我认为应该更谨慎一些。",
            "从实际经验来看，我们可能需要先解决最核心的矛盾。",
            "哈哈，大家说得都很有道理，我先记下来。",
            "我倾向于先做个最小方案，再逐步迭代。",
            "能不能请上一个发言的人再展开讲一下？",
        ]
        idx = (len(history) + hash(name) * 3) % len(phrases)
        return f"{phrases[idx]}（第{round_no}轮发言）"

    @staticmethod
    def _mock_summary(log_text: str) -> str:
        count = log_text.count("\n")
        return (
            "【对话总结】\n"
            "本次群聊中，多个参与者围绕给定背景展开了多轮讨论。大家从不同角度提出了观点，"
            f"期间共有约 {max(1, count)} 条发言。讨论在部分议题上形成了初步共识，"
            "但仍有一些开放问题需要后续进一步探讨。最终结果：各方意见已充分交换，"
            "建议根据记录继续深化关键分歧。"
        )

    @staticmethod
    def _mock_assist(messages: list[dict]) -> tuple[str, dict]:
        last_user = ""
        for m in reversed(messages):
            if m["role"] == "user":
                last_user = m["content"]
                break
        reply = f"好的，我已经理解你的要求：{last_user[:40]}。下面是我建议的修改，请确认后应用。"

        names: list[str] = []
        system = messages[0]["content"] if messages else ""
        try:
            names = re.findall(r'"name"\s*:\s*"([^"]+)"', system)
        except Exception:
            names = []

        proposal = {
            "agents": {},
            "single_max_tokens": 300,
            "shared_background": "这是一场围绕共同话题展开的轻松群聊，参与者可以自由表达观点。",
        }
        if names:
            first_name = names[0]
            proposal["agents"][first_name] = {
                "system_prompt": (
                    f"你是一个名为「{first_name}」的群聊参与者。"
                    f"请根据以下要求调整你的说话风格与立场：{last_user[:80]}"
                ),
                "visibility": ["all"],
            }
        return json.dumps({"reply": reply, "proposal": proposal}, ensure_ascii=False), {
            "prompt_tokens": 0,
            "completion_tokens": 80,
            "total_tokens": 80,
        }
