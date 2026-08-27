import random
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from app.config import DEFAULT_SINGLE_MAX_TOKENS
from app.db import update_conversation
from app.llm import LLMClient
from app.scheduler import update_heat, willingness_select


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_single_max_tokens(config: dict) -> int:
    agents = config.get("agents") or []
    values = []
    for a in agents:
        raw = a.get("max_tokens") if isinstance(a, dict) else None
        if raw:
            try:
                values.append(int(raw))
            except (TypeError, ValueError):
                continue
    if values and len(set(values)) == 1:
        return values[0]
    return DEFAULT_SINGLE_MAX_TOKENS


def _normalize_agents(agents: list[dict], single_max_tokens: int = DEFAULT_SINGLE_MAX_TOKENS) -> list[dict]:
    normalized = []
    for i, a in enumerate(agents):
        item = dict(a)
        item.setdefault("id", f"a{i}")
        item.setdefault("name", item.get("name") or f"Agent {i + 1}")
        item.setdefault("system_prompt", "")
        item.setdefault("visibility", [])
        item.setdefault("max_tokens", single_max_tokens)
        normalized.append(item)
    return normalized


class ConversationRunner:
    """Runs a multi-agent conversation in a background thread."""

    def __init__(self, conv_id: str, config_id: str, name: str, config: dict, llm: LLMClient):
        self.id = conv_id
        self.config_id = config_id
        self.name = name
        self.llm = llm

        self.single_max_tokens = int(config.get("single_max_tokens") or _derive_single_max_tokens(config))
        self.agents = _normalize_agents(config.get("agents", []), self.single_max_tokens)
        self.shared_background = config.get("shared_background", "")
        self.total_max_tokens = config.get("total_max_tokens") or None
        self.total_duration_seconds = config.get("total_duration_seconds") or None
        self.first_speaker = config.get("first_speaker", "random")
        self.scheduling_mode = config.get("scheduling_mode", "round_robin")
        if len(self.agents) == 2:
            self.scheduling_mode = "round_robin"
        self.round_robin_order = config.get("round_robin_order") or []
        sp = config.get("scheduler_params") or {}
        self.lam = float(sp.get("lam", 2.0))
        self.tau = float(sp.get("tau", 1.5))
        self.gamma = float(sp.get("gamma", 0.7))
        self.forbid_consecutive = bool(sp.get("forbid_consecutive", True))

        self.end_vote_enabled = bool(config.get("end_vote_enabled"))
        self.end_vote_proposers = list(config.get("end_vote_proposers") or [])
        self.end_vote_cooldown_turns = int(config.get("end_vote_cooldown_turns") or 3)

        self.whiteboard_enabled = bool(config.get("whiteboard_enabled"))
        self.whiteboard_format = config.get("whiteboard_format") or "md"
        self.whiteboard_editors = list(config.get("whiteboard_editors") or [])
        self.whiteboard_content = config.get("whiteboard_content") or ""
        self.whiteboard_rev = 0
        self.whiteboard_last_editor: Optional[str] = None

        self.messages: list[dict] = []
        self.heat = [0.0] * len(self.agents)
        self.turn = 0
        self.last_agent_idx: Optional[int] = None
        self.total_output_tokens = 0
        self.total_prompt_tokens = 0
        self.summary = ""
        self.status = "running"
        self.started_at = _now()
        self.ended_at = None
        self.error = None
        self.paused_reason = None
        self.ended_reason = None
        self.votes: list[dict] = []
        self._end_vote_block_until_turn = -1

        self.pending_human_message: Optional[str] = None
        self.pending_human_target: Optional[int] = None
        self._forced_next_idx: Optional[int] = None
        self._interrupt_requested = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

        self._active_seconds = 0.0
        self._segment_start: Optional[float] = None

        self.first_idx = self._resolve_first()
        self.order = self._build_order()
        self._rr_index = 0

    # ---- Setup helpers ----

    def _resolve_first(self) -> int:
        n = len(self.agents)
        if not n:
            return 0
        fs = self.first_speaker
        if fs and fs != "random":
            for i, a in enumerate(self.agents):
                if a["id"] == fs or a["name"] == fs:
                    return i
        return random.randrange(n)

    def _build_order(self) -> list[int]:
        n = len(self.agents)
        if n == 0:
            return []
        if self.scheduling_mode != "round_robin":
            return list(range(n))
        if n == 2:
            return [self.first_idx, 1 - self.first_idx]
        order = []
        for name in self.round_robin_order:
            for i, a in enumerate(self.agents):
                if a["id"] == name or a["name"] == name:
                    order.append(i)
                    break
        if len(order) == n:
            return order
        order = list(range(n))
        random.shuffle(order)
        fs = self.first_speaker
        if fs and fs != "random":
            for i, a in enumerate(self.agents):
                if a["id"] == fs or a["name"] == fs:
                    if i in order:
                        order.remove(i)
                    order.insert(0, i)
                    break
        return order

    # ---- Public API ----

    def start(self) -> None:
        with self._lock:
            self.status = "running"
            self._interrupt_requested = False
            if self._segment_start is None:
                self._segment_start = time.time()
        self._thread = threading.Thread(target=self._run, name=f"conv-{self.id}", daemon=True)
        self._thread.start()

    def resume(self) -> bool:
        old_thread = None
        with self._lock:
            if self.status != "paused" or self._limit_reached_nolock():
                return False
            old_thread = self._thread
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=5.0)
            if old_thread.is_alive():
                return False
        with self._lock:
            if self.status != "paused":
                return False
            self.status = "running"
            self._interrupt_requested = False
            self._segment_start = time.time()
        self._thread = threading.Thread(target=self._run, name=f"conv-{self.id}", daemon=True)
        self._thread.start()
        return True

    def summarize_now(self) -> bool:
        old_thread = None
        with self._lock:
            if self.status not in ("paused",):
                return False
            old_thread = self._thread
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=5.0)
            if old_thread.is_alive():
                return False
        with self._lock:
            if self.status not in ("paused",):
                return False
        self._finish("completed")
        return True

    def reserve(self, content: str) -> None:
        with self._lock:
            self.pending_human_message = content

    def _resolve_agent_idx(self, target: Optional[str]) -> Optional[int]:
        if not target:
            return None
        for i, a in enumerate(self.agents):
            if a["id"] == target or a["name"] == target:
                return i
        return None

    def human_say(self, content: str, target: Optional[str] = None) -> Optional[str]:
        """Human message: queue it while running, append immediately while paused.

        If target names an agent (by id or name), that agent is forced to speak
        on the next agent turn, bypassing the scheduler.
        """
        target_idx = self._resolve_agent_idx(target)
        with self._lock:
            status = self.status
        if status == "running":
            with self._lock:
                self.pending_human_message = content
                self.pending_human_target = target_idx
            return "reserved"
        if status == "paused":
            self._append_message("human", "人类", content, 0)
            with self._lock:
                self.turn += 1
                if target_idx is not None:
                    self._forced_next_idx = target_idx
            self._persist()
            return "appended"
        return None

    def update_limits(
        self, total_max_tokens: Optional[int], total_duration_seconds: Optional[int]
    ) -> bool:
        with self._lock:
            if self.status != "paused":
                return False
            self.total_max_tokens = total_max_tokens
            self.total_duration_seconds = total_duration_seconds
        self._persist()
        return True

    def interrupt(self) -> None:
        with self._lock:
            self._interrupt_requested = True

    def to_dict(self) -> dict:
        with self._lock:
            active_seconds = self._active_seconds
            if self._segment_start is not None:
                active_seconds += time.time() - self._segment_start
            remaining_seconds = None
            if self.total_duration_seconds is not None:
                remaining_seconds = max(0.0, self.total_duration_seconds - active_seconds)
            can_resume = self.status == "paused" and not self._limit_reached_nolock()
            return {
                "id": self.id,
                "config_id": self.config_id,
                "name": self.name,
                "status": self.status,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "agents": [{"id": a["id"], "name": a["name"]} for a in self.agents],
                "messages": [dict(m) for m in self.messages],
                "summary": self.summary,
                "total_output_tokens": self.total_output_tokens,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_max_tokens": self.total_max_tokens,
                "total_duration_seconds": self.total_duration_seconds,
                "scheduling_mode": self.scheduling_mode,
                "turn": self.turn,
                "error": self.error,
                "paused_reason": self.paused_reason,
                "ended_reason": self.ended_reason,
                "can_resume": can_resume,
                "votes": [dict(v) for v in self.votes],
                "whiteboard": {
                    "enabled": self.whiteboard_enabled,
                    "format": self.whiteboard_format,
                    "editors": list(self.whiteboard_editors),
                    "content": self.whiteboard_content,
                    "rev": self.whiteboard_rev,
                    "last_editor": self.whiteboard_last_editor,
                },
                "config": {
                    "agents": [dict(a) for a in self.agents],
                    "shared_background": self.shared_background,
                    "single_max_tokens": self.single_max_tokens,
                    "total_max_tokens": self.total_max_tokens,
                    "total_duration_seconds": self.total_duration_seconds,
                    "first_speaker": self.first_speaker,
                    "scheduling_mode": self.scheduling_mode,
                    "round_robin_order": list(self.round_robin_order),
                    "scheduler_params": {
                        "lam": self.lam,
                        "tau": self.tau,
                        "gamma": self.gamma,
                        "forbid_consecutive": self.forbid_consecutive,
                    },
                    "end_vote_enabled": self.end_vote_enabled,
                    "end_vote_proposers": list(self.end_vote_proposers),
                    "end_vote_cooldown_turns": self.end_vote_cooldown_turns,
                    "whiteboard_enabled": self.whiteboard_enabled,
                    "whiteboard_format": self.whiteboard_format,
                    "whiteboard_editors": list(self.whiteboard_editors),
                },
                "heat": list(self.heat),
                "order": list(self.order),
                "rr_index": self._rr_index,
                "end_vote_block_until_turn": self._end_vote_block_until_turn,
                "forced_next_idx": self._forced_next_idx,
                "last_agent_idx": self.last_agent_idx,
                "active_seconds": self._active_seconds,
                "elapsed_seconds": round(active_seconds, 1),
                "remaining_seconds": (
                    round(remaining_seconds, 1)
                    if remaining_seconds is not None
                    else None
                ),
            }

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @classmethod
    def from_payload(cls, conv: dict, llm: LLMClient) -> "ConversationRunner":
        config = conv.get("config") or {}
        runner = cls(conv["id"], conv.get("config_id"), conv.get("name", "对话"), config, llm)
        runner.messages = [dict(m) for m in conv.get("messages", [])]
        heat = conv.get("heat")
        if heat and len(heat) == len(runner.agents):
            runner.heat = [float(h) for h in heat]
        runner.turn = int(conv.get("turn", 0) or 0)
        runner.last_agent_idx = conv.get("last_agent_idx")
        runner.total_output_tokens = int(conv.get("total_output_tokens", 0) or 0)
        runner.total_prompt_tokens = int(conv.get("total_prompt_tokens", 0) or 0)
        runner.summary = conv.get("summary", "") or ""
        runner.status = conv.get("status", "paused")
        runner.started_at = conv.get("started_at")
        runner.ended_at = conv.get("ended_at")
        runner.error = conv.get("error")
        runner.paused_reason = conv.get("paused_reason")
        runner.ended_reason = conv.get("ended_reason")
        runner.votes = [dict(v) for v in conv.get("votes", [])]
        order = conv.get("order")
        if order and len(order) == len(runner.agents):
            runner.order = [int(x) for x in order]
        runner._rr_index = int(conv.get("rr_index", 0) or 0) % max(1, len(runner.order))
        runner._end_vote_block_until_turn = int(conv.get("end_vote_block_until_turn", -1) or -1)
        fni = conv.get("forced_next_idx")
        runner._forced_next_idx = int(fni) if isinstance(fni, int) else None
        wb = conv.get("whiteboard") or {}
        runner.whiteboard_content = wb.get("content") or ""
        runner.whiteboard_rev = int(wb.get("rev") or 0)
        runner.whiteboard_last_editor = wb.get("last_editor")
        runner._active_seconds = float(conv.get("active_seconds", 0.0) or 0.0)
        runner._segment_start = None
        return runner

    # ---- Context builders ----

    def _build_persona(self, agent: dict) -> str:
        parts = [self.shared_background or "（无共享背景）"]
        parts.append(f"\n\n你的名字是：{agent['name']}")
        parts.append(f"\n\n【你自己的角色设定】\n{agent['system_prompt'] or '（未设定，自然参与即可）'}")

        visible_others = []
        for other in self.agents:
            if other["id"] == agent["id"]:
                continue
            vis = other.get("visibility") or []
            is_visible = (
                vis == "all"
                or (isinstance(vis, list) and ("all" in vis or agent["id"] in vis or agent["name"] in vis))
            )
            if is_visible:
                visible_others.append(other)

        if visible_others:
            parts.append("\n\n【你被告知的其他参与者的角色设定】")
            for other in visible_others:
                parts.append(f"- {other['name']}: {other['system_prompt'] or '（未设定）'}")

        return "\n".join(parts)

    def _is_proposer(self, agent: dict) -> bool:
        if not self.end_vote_enabled:
            return False
        return agent["id"] in self.end_vote_proposers or agent["name"] in self.end_vote_proposers

    def _can_edit_whiteboard(self, agent: dict) -> bool:
        if not self.whiteboard_enabled:
            return False
        return agent["id"] in self.whiteboard_editors or agent["name"] in self.whiteboard_editors

    def _build_system(self, agent: dict) -> str:
        base = self._build_persona(agent) + (
            "\n\n你正在参与一场多人实时群聊。请以你角色的口吻，用中文自然发言。"
        )
        can_end = self._is_proposer(agent)
        can_wb = self._can_edit_whiteboard(agent)

        fields = [
            '"speech"：字符串，你本轮要说的话（用中文，直接说话，不要加「某某说：」之类前缀）',
        ]
        if can_end:
            fields.append(
                '"propose_end"：布尔值。若你认为这场对话可以结束了就设为 true，'
                "这会发起一次全体投票，只有所有参与者都同意才会结束；不想结束就设为 false 或省略"
            )
        if can_wb:
            fields.append(
                '"whiteboard"：对象，用于对共享白板做增量修改；本轮不改就省略或设为 null。'
                '格式为 {"ops": [...]}，每个 op 可为：'
                '{"op":"append","content":"追加到白板末尾的内容"}、'
                '{"op":"replace","find":"白板中已有的原文片段","replace":"替换后的新内容"}、'
                '{"op":"prepend","content":"加到白板开头的内容"}'
            )
        base += (
            "\n\n请只输出一个 JSON 对象，不要输出任何多余文字，包含以下字段：\n"
            + "\n".join(f"- {f}" for f in fields)
        )
        if can_wb:
            fmt = "HTML" if self.whiteboard_format == "html" else "Markdown"
            current = self.whiteboard_content or "（当前白板为空）"
            base += (
                f"\n\n【共享白板】这是本次群聊要沉淀的最终产出物，格式为 {fmt}，"
                "请在合适时机通过 whiteboard 字段逐步完善它。当前白板内容如下：\n"
                f"---\n{current}\n---"
            )
        return base

    @staticmethod
    def _apply_whiteboard_ops(content: str, ops: list) -> str:
        """Apply incremental whiteboard ops. Defensive: unknown/no-match ops are skipped."""
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

    def _build_score_system(self, agent: dict) -> str:
        return self._build_persona(agent) + (
            "\n\n你正在参与一场多人实时群聊。现在需要你评估："
            "基于当前对话内容，你有多想发言。"
        )

    def _history(self) -> list[dict]:
        return [
            {"role": "user", "content": f"{m['speaker']}: {m['content']}"}
            for m in self.messages
        ]

    def _log_text(self) -> str:
        return "\n".join(f"{m['speaker']}: {m['content']}" for m in self.messages)

    # ---- Scheduling ----

    def _willingness_choose(self) -> tuple[int, list[dict]]:
        raw_scores: list[float] = []
        score_details: list[dict] = []
        for a in self.agents:
            system = self._build_score_system(a)
            score, usage = self.llm.willingness_score(a["name"], system, self._log_text(), self.turn)
            raw_scores.append(score)
            score_details.append({"name": a["name"], "score": score})
            self.total_output_tokens += usage["completion_tokens"]
            self.total_prompt_tokens += usage["prompt_tokens"]
        idx = willingness_select(
            raw_scores,
            self.heat,
            lam=self.lam,
            tau=self.tau,
            forbid_consecutive=self.forbid_consecutive,
            last_speaker=self.last_agent_idx,
        )
        return idx, score_details

    # ---- Time / limits ----

    def _elapsed_nolock(self) -> float:
        if self._segment_start is None:
            return self._active_seconds
        return self._active_seconds + (time.time() - self._segment_start)

    def _elapsed(self) -> float:
        with self._lock:
            return self._elapsed_nolock()

    def _pause_segment(self) -> None:
        with self._lock:
            if self._segment_start is not None:
                self._active_seconds += time.time() - self._segment_start
                self._segment_start = None

    def _limit_reached_nolock(self) -> bool:
        if self.total_max_tokens is not None and self.total_output_tokens >= self.total_max_tokens:
            return True
        if self.total_duration_seconds is not None and self._elapsed_nolock() >= self.total_duration_seconds:
            return True
        return False

    def _limit_reached(self) -> bool:
        with self._lock:
            return self._limit_reached_nolock()

    # ---- Main loop ----

    def _run(self) -> None:
        with self._lock:
            if self._segment_start is None:
                self._segment_start = time.time()
        end_voted = False
        try:
            while True:
                with self._lock:
                    if self._interrupt_requested:
                        break
                if self._limit_reached():
                    break

                human_msg = None
                human_target = None
                with self._lock:
                    if self.pending_human_message is not None:
                        human_msg = self.pending_human_message
                        self.pending_human_message = None
                        human_target = self.pending_human_target
                        self.pending_human_target = None

                if human_msg is not None:
                    self._append_message("human", "人类", human_msg, 0)
                    with self._lock:
                        self.turn += 1
                        if human_target is not None:
                            self._forced_next_idx = human_target
                    self._persist()
                    continue

                if not self.agents:
                    break
                scores = None
                forced_idx = None
                with self._lock:
                    if self._forced_next_idx is not None and 0 <= self._forced_next_idx < len(self.agents):
                        forced_idx = self._forced_next_idx
                    self._forced_next_idx = None
                if forced_idx is not None:
                    idx = forced_idx
                elif self.scheduling_mode == "round_robin":
                    idx = self.order[self._rr_index % len(self.order)]
                    self._rr_index += 1
                else:
                    if self.turn == 0:
                        idx = self.first_idx
                    else:
                        idx, scores = self._willingness_choose()

                agent = self.agents[idx]
                system = self._build_system(agent)
                max_tokens = self.single_max_tokens
                system += f"\n\n（其中 speech 字段请控制在约 {max_tokens} tokens 以内。）"
                turn_out, usage = self.llm.agent_turn(agent["name"], system, self._history(), max_tokens)

                speech = turn_out.get("speech") or ""
                proposed_end = bool(turn_out.get("propose_end")) and self._is_proposer(agent)

                # Edit the whiteboard within this same turn (before proceeding),
                # so reads/writes never overlap with the next speaker.
                wb_edited = False
                ops = turn_out.get("whiteboard_ops") or []
                if ops and self._can_edit_whiteboard(agent):
                    with self._lock:
                        new_content = self._apply_whiteboard_ops(self.whiteboard_content, ops)
                        if new_content != self.whiteboard_content:
                            self.whiteboard_content = new_content
                            self.whiteboard_rev += 1
                            self.whiteboard_last_editor = agent["name"]
                            wb_edited = True

                self._append_message(
                    "agent", agent["name"], speech, usage["completion_tokens"],
                    scores=scores, proposed_end=proposed_end, wb_edited=wb_edited,
                )
                self.total_output_tokens += usage["completion_tokens"]
                self.total_prompt_tokens += usage["prompt_tokens"]
                self.heat = update_heat(self.heat, idx, self.gamma)
                self.last_agent_idx = idx
                with self._lock:
                    self.turn += 1
                self._persist()

                if proposed_end and self.turn > self._end_vote_block_until_turn:
                    if self._run_end_vote_inline():
                        end_voted = True
                        break
                    self._end_vote_block_until_turn = self.turn + self.end_vote_cooldown_turns

            with self._lock:
                was_interrupted = self._interrupt_requested
            self._pause_segment()
            with self._lock:
                self.status = "paused"
                if end_voted:
                    self.paused_reason = "vote_end"
                elif was_interrupted:
                    self.paused_reason = "manual"
                else:
                    self.paused_reason = "limit"
                self._interrupt_requested = False
            self._persist()
        except Exception as exc:  # pragma: no cover - defensive
            # Pause recoverably instead of terminally erroring, so a transient
            # LLM failure doesn't kill the whole conversation. resume() only
            # blocks paused_reason=="limit", and summarize_now() accepts any
            # paused state, so the user can retry or summarize afterwards.
            self._pause_segment()
            with self._lock:
                self.error = f"{exc}\n{traceback.format_exc()}"
                self.status = "paused"
                self.paused_reason = "error"
                self._interrupt_requested = False
            self._persist()

    def _finish(self, status: str, reason: Optional[str] = None) -> None:
        with self._lock:
            self.status = status
            self.ended_at = _now()
            self.ended_reason = reason
        log_text = self._log_text()
        if log_text.strip():
            try:
                content, usage = self.llm.summarize(log_text)
                self.summary = content
                self.total_output_tokens += usage["completion_tokens"]
                self.total_prompt_tokens += usage["prompt_tokens"]
            except Exception as exc:
                self.summary = f"总结失败：{exc}"
        else:
            self.summary = "本次对话没有任何发言。"
        self._persist()

    def _run_end_vote_inline(self) -> bool:
        """Run a unanimous end-conversation vote inline in the main loop.

        Returns True only if every agent voted to end. Recorded in self.votes
        with kind="end" so the transcript renders it like other votes.
        """
        if not self.agents:
            return False
        options = ["同意结束", "继续讨论"]
        question = "有角色提议结束本次对话，是否结束？"
        vote_id = f"v{len(self.votes) + 1}"
        with self._lock:
            self.votes.append(
                {
                    "id": vote_id,
                    "kind": "end",
                    "question": question,
                    "options": options,
                    "votes_per_person": 1,
                    "status": "running",
                    "created_at": _now(),
                    "results": {"1": 0, "2": 0},
                    "ballots": [],
                    "agreed": False,
                    "error": None,
                }
            )
        self._persist()

        results = {"1": 0, "2": 0}
        ballots = []
        try:
            for agent in self.agents:
                system = self._build_vote_system(agent, question, options, 1)
                result, usage = self.llm.vote(
                    agent["name"], system, self._log_text(), question, options, 1
                )
                self.total_output_tokens += usage["completion_tokens"]
                self.total_prompt_tokens += usage["prompt_tokens"]
                choices = result.get("choices") or []
                reason = result.get("reason", "") or ""
                for choice in choices:
                    key = str(choice)
                    if key in results:
                        results[key] += 1
                ballots.append(
                    {
                        "agent_id": agent["id"],
                        "agent_name": agent["name"],
                        "choices": [str(c) for c in choices],
                        "reason": reason,
                    }
                )
                with self._lock:
                    v = next((x for x in self.votes if x["id"] == vote_id), None)
                    if v is not None:
                        v["ballots"] = [dict(b) for b in ballots]
                        v["results"] = dict(results)
                self._persist()

            agreed = len(self.agents) > 0 and results["1"] >= len(self.agents)
            with self._lock:
                v = next((x for x in self.votes if x["id"] == vote_id), None)
                if v is not None:
                    v["status"] = "completed"
                    v["ballots"] = [dict(b) for b in ballots]
                    v["results"] = dict(results)
                    v["agreed"] = agreed
            self._persist()
            return agreed
        except Exception as exc:
            with self._lock:
                v = next((x for x in self.votes if x["id"] == vote_id), None)
                if v is not None:
                    v["status"] = "error"
                    v["error"] = f"{exc}\n{traceback.format_exc()}"
            self._persist()
            return False

    def _build_vote_system(self, agent: dict, question: str, options: list[str], votes_per_person: int) -> str:
        options_text = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        return self._build_persona(agent) + (
            "\n\n你正在参与群聊中的投票。请基于当前对话内容和你自己的角色立场投票。"
            "\n投票题目：\n" + question +
            "\n\n选项：\n" + options_text +
            f"\n\n你有 {votes_per_person} 票，可以对不同选项任意分配，也可以重复投给同一选项。"
            '请只输出 JSON 对象，格式：{"choices": ["1", "2"], "reason": "简短理由"}，'
            "choices 数组长度应等于你的票数，每项是选项编号。"
        )

    def start_vote(self, question: str, options: list[str], votes_per_person: int) -> Optional[dict]:
        question = (question or "").strip()
        options = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not question or len(options) < 2 or int(votes_per_person) < 1:
            return None
        vote_id = f"v{len(self.votes) + 1}"
        with self._lock:
            if self.status == "running":
                return None
            vote = {
                "id": vote_id,
                "question": question,
                "options": options,
                "votes_per_person": int(votes_per_person),
                "status": "pending",
                "created_at": _now(),
                "results": {},
                "ballots": [],
                "error": None,
            }
            self.votes.append(vote)
        self._persist()
        thread = threading.Thread(
            target=self._run_vote,
            args=(vote_id,),
            name=f"vote-{self.id}-{vote_id}",
            daemon=True,
        )
        thread.start()
        return dict(vote)

    def _run_vote(self, vote_id: str) -> None:
        with self._lock:
            vote = next((v for v in self.votes if v["id"] == vote_id), None)
            if vote is None:
                return
            vote["status"] = "running"
            vote["ballots"] = []
            vote["error"] = None
        self._persist()

        results = {str(i + 1): 0 for i in range(len(vote["options"]))}
        ballots = []
        try:
            for agent in self.agents:
                system = self._build_vote_system(
                    agent,
                    vote["question"],
                    vote["options"],
                    vote["votes_per_person"],
                )
                result, usage = self.llm.vote(
                    agent["name"],
                    system,
                    self._log_text(),
                    vote["question"],
                    vote["options"],
                    vote["votes_per_person"],
                )
                choices = result.get("choices") or []
                reason = result.get("reason", "") or ""
                for choice in choices:
                    key = str(choice)
                    if key in results:
                        results[key] += 1
                ballots.append(
                    {
                        "agent_id": agent["id"],
                        "agent_name": agent["name"],
                        "choices": [str(c) for c in choices],
                        "reason": reason,
                    }
                )
                with self._lock:
                    vote = next((v for v in self.votes if v["id"] == vote_id), None)
                    if vote is not None:
                        vote["ballots"] = [dict(b) for b in ballots]
                        vote["results"] = dict(results)
                self._persist()

            with self._lock:
                vote = next((v for v in self.votes if v["id"] == vote_id), None)
                if vote is not None:
                    vote["status"] = "completed"
                    vote["ballots"] = [dict(b) for b in ballots]
                    vote["results"] = dict(results)
            self._persist()
        except Exception as exc:
            with self._lock:
                vote = next((v for v in self.votes if v["id"] == vote_id), None)
                if vote is not None:
                    vote["status"] = "error"
                    vote["error"] = f"{exc}\n{traceback.format_exc()}"
            self._persist()

    def _append_message(self, role: str, speaker: str, content: str, tokens: int, scores: Optional[list[dict]] = None, proposed_end: bool = False, wb_edited: bool = False) -> None:
        with self._lock:
            message = {
                "role": role,
                "speaker": speaker,
                "content": content,
                "tokens": tokens,
                "ts": _now(),
                "round": self.turn,
            }
            if scores is not None:
                message["scores"] = scores
            if proposed_end:
                message["proposed_end"] = True
            if wb_edited:
                message["wb_edited"] = True
            self.messages.append(message)

    def _persist(self) -> None:
        try:
            update_conversation(self.id, self.to_dict(), status=self.status)
        except Exception:
            # Persistence failures should not kill the conversation thread.
            pass


RUNNERS: dict[str, ConversationRunner] = {}
