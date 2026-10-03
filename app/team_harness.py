"""Isolated DSH execution for team activations and system auxiliary calls.

No direct LLM or Chat Completions repair route is used here. Safe format repair
uses a separate no-tool DSH session; unrecoverable task intent fails locally,
leaving already accepted command receipts available for recovery.
"""
from __future__ import annotations

import ast
import json
import math
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from app import config
from app.harness import (HarnessSettings, HarnessTurnError, _identity, _write_json,
                         event_usage, parse_final, result_was_truncated)
from app.llm import _extract_json
from app.tool_logs import tool_call_id, tool_log_update


TEAM_TOOLS = (
    "kds_delegate_task", "kds_spawn_subagent", "kds_get_task_results",
    "kds_send_group_message", "kds_request_discussion", "kds_cancel_task", "kds_retry_task",
)
_NATIVE_SUBAGENT_TOOLS = {"subagent", "spawn_subagent", "delegate", "agent_team",
                          "subagent_spawn", "subagent_resume", "subagent_cancel"}


def parse_delivery(text: str) -> dict:
    """Validate a task delivery without inventing task transitions or actions."""
    value = _extract_json(text)
    if not isinstance(value, dict) or not isinstance(value.get("action"), str) or value.get("action") not in {
        "continue", "wait_children", "complete_task",
    }:
        raise HarnessTurnError("团队交付必须包含 continue、wait_children 或 complete_task 动作。", "format")
    content = value.get("speech", value.get("content", value.get("result") if isinstance(value.get("result"), str) else ""))
    if not isinstance(content, str):
        raise HarnessTurnError("团队交付的 speech/content 必须是字符串。", "format")
    result = value.get("result", content)
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError):
        raise HarnessTurnError("团队交付的 result 必须是规范 JSON 值。", "format") from None
    wait = value.get("wait") or {}
    if not isinstance(wait, dict):
        raise HarnessTurnError("团队交付的 wait 必须是对象。", "format")
    task_ids = wait.get("task_ids", value.get("wait_for", []))
    mode = wait.get("mode", value.get("wait_mode", "all"))
    timeout = wait.get("timeout_seconds", value.get("timeout_seconds"))
    if (not isinstance(task_ids, list) or any(not isinstance(item, str) or not item for item in task_ids)
            or len(set(task_ids)) != len(task_ids)):
        raise HarnessTurnError("等待任务必须是不重复的 task_id 数组。", "format")
    if not isinstance(mode, str) or mode not in {"all", "any_success"}:
        raise HarnessTurnError("等待方式必须为 all 或 any_success。", "format")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not math.isfinite(timeout) or timeout <= 0):
        raise HarnessTurnError("等待时限必须是正数。", "format")
    if value["action"] == "wait_children" and not task_ids:
        raise HarnessTurnError("wait_children 必须指定等待的子任务。", "format")
    if value["action"] != "wait_children" and (task_ids or timeout is not None):
        raise HarnessTurnError("只有 wait_children 动作可以包含等待集合。", "format")
    whiteboard = value.get("whiteboard")
    if whiteboard is None and "whiteboard_ops" in value:
        whiteboard = {"base_rev": value.get("whiteboard_base_rev"), "ops": value["whiteboard_ops"]}
    if whiteboard is not None and (not isinstance(whiteboard, dict)
                                   or isinstance(whiteboard.get("base_rev"), bool)
                                   or not isinstance(whiteboard.get("base_rev"), int)
                                   or whiteboard["base_rev"] < 0):
        raise HarnessTurnError("白板修改必须包含非负整数 base_rev。", "format")
    # Reuse the established whiteboard validator only; it never calls a model.
    validated = parse_final(json.dumps({"speech": content or "交付", "whiteboard": whiteboard}, ensure_ascii=False))
    output = {"action": value["action"], "speech": content.strip(), "content": content.strip(),
              "result": result, "wait_for": task_ids, "wait_mode": mode, "timeout_seconds": timeout,
              "wait": {"task_ids": task_ids, "mode": mode},
              "whiteboard_ops": validated["whiteboard_ops"]}
    if timeout is not None:
        output["wait"]["timeout_seconds"] = timeout
    if whiteboard is not None:
        output["whiteboard"] = whiteboard
    return output


def _local_channel(url: str, credential: str):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.fragment or not credential):
        raise HarnessTurnError("团队工具需要绑定身份的本机 HTTP 命令通道。", "configuration")


class TeamHarnessAdapter:
    """One runtime/session/workspace per actual instance, never per role template.

    execute returns the validated action plus usage and a serializable state.
    Callbacks settle usage as it arrives; callers must not charge returned totals
    a second time. close(instance_id) releases one runtime while preserving files.
    """

    def __init__(self, conversation_id: str = "team", settings: HarnessSettings | None = None,
                 *, factory=None):
        self.settings = settings or HarnessSettings()
        self.root = self.settings.root.resolve() / ("team-" + _identity(conversation_id))
        self.factory = factory
        self._runtimes = {}
        self._profiles = {}
        self._locks = {}
        self._guard = threading.RLock()

    def _settings_for(self, context):
        model = context.get("model_config") or {}
        if not isinstance(model, dict):
            raise HarnessTurnError("团队模型配置必须是对象。", "configuration")
        allowed = {"model", "api_key", "base_url", "reasoning_effort"}
        settings = replace(self.settings, **{key: value for key, value in model.items() if key in allowed})
        limits = context.get("execution_limits") or {}
        settings = replace(settings, **{key: min(getattr(settings, key), value)
                                       for key, value in limits.items() if key in {
                                           "timeout", "max_steps", "request_max_tokens", "turn_max_tokens",
                                       }})
        # Legacy discussions intentionally require tools. Team auxiliary sessions
        # intentionally have none; validate the remaining limits on the base type.
        HarnessSettings.validate(replace(settings, tools=("__team_validation__",)))
        return settings

    def _runtime(self, instance_id, role_root, control_path, stop_path, tools, settings):
        profile = (tuple(tools), settings.model, settings.base_url, settings.api_key, settings.reasoning_effort)
        with self._guard:
            if instance_id in self._runtimes:
                return self._runtimes[instance_id], False
            factory = self.factory
            if factory is None:
                try:
                    from deepseek_harness import DeepSeekHarness
                except ImportError as exc:
                    raise HarnessTurnError("尚未安装 DSH SDK，请安装 requirements-dsh.txt。", "configuration") from exc
                factory = DeepSeekHarness
            workspace = role_root / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            patch = role_root / "kds-team.patch.json"
            _write_json(patch, [
                {"id": "system-prompt", "config": {"personaPrefix": "你是 KDS 团队中的独立 Agent。"}},
                {"id": "tools", "config": {"mode": "native"}},
                {"insert": [
                    {"id": "kds-team-tools", "name": str(Path(__file__).parent / "dsh" / "team_tools.mjs")},
                    {"id": "kds-discussion", "name": str(Path(__file__).parent / "dsh" / "bridge.mjs")},
                ]},
            ])
            runtime = factory(
                provider="deepseek-official", model=settings.model, api_key=settings.api_key,
                base_url=settings.base_url, reasoning_effort=settings.reasoning_effort,
                max_tokens=settings.request_max_tokens, dsh_bin=settings.dsh_bin, profile="sdk",
                cwd=str(workspace), runtime_cwd=str(workspace), dsh_home=str(role_root / "home"),
                # A newly isolated CLI profile may need to bootstrap its local
                # dependency links. Keep initialization bounded by the same
                # activation deadline, without imposing the older 30s window.
                patches=(str(patch),), initialize_timeout_seconds=min(60, settings.timeout),
                request_timeout_seconds=settings.timeout, shutdown_timeout_seconds=2,
                env={"KDS_CONTROL_FILE": str(control_path), "KDS_STOP_FILE": str(stop_path),
                     "DSH_PERMISSION_MODE": "workspace-write"},
            )
            self._runtimes[instance_id] = runtime
            self._profiles[instance_id] = profile
            return runtime, True

    def execute(self, context, command_url, credential, activity_cb, usage_cb, cancel_event):
        return self._execute(context, command_url, credential, activity_cb, usage_cb, cancel_event)

    def execute_auxiliary(self, kind, context, command_url="", credential="", activity_cb=None,
                          usage_cb=None, cancel_event=None):
        auxiliary = dict(context, purpose=kind, tools=[],
                         instance_id="system:" + kind + ":" + str(context.get("operation_id", context.get("activation_id", uuid.uuid4().hex))))
        try:
            return self._execute(auxiliary, command_url, credential, activity_cb or (lambda _: None),
                                 usage_cb or (lambda _: None), cancel_event or threading.Event(), auxiliary=True)
        finally:
            self.close(auxiliary["instance_id"])

    def _execute(self, context, command_url, credential, activity_cb, usage_cb, cancel_event, *, auxiliary=False):
        instance_id = str(context.get("instance_id") or "")
        if not instance_id:
            raise HarnessTurnError("缺少团队实例 ID。", "configuration")
        with self._guard:
            lock = self._locks.setdefault(instance_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise HarnessTurnError("同一团队实例已有正在执行的激活。", "busy")
        try:
            return self._run(context, command_url, credential, activity_cb, usage_cb, cancel_event, auxiliary)
        finally:
            lock.release()

    def _run(self, context, command_url, credential, activity_cb, usage_cb, cancel_event, auxiliary):
        settings = self._settings_for(context)
        instance_id = context["instance_id"]
        chosen_tools = context.get("tools", (*settings.tools, *TEAM_TOOLS))
        if (not isinstance(chosen_tools, (list, tuple))
                or any(not isinstance(tool, str) for tool in chosen_tools)):
            raise HarnessTurnError("团队工具权限必须是工具名数组。", "configuration")
        tools = list(chosen_tools)
        if any(tool in _NATIVE_SUBAGENT_TOOLS or "subagent" in tool and tool not in TEAM_TOOLS for tool in tools):
            raise HarnessTurnError("团队模式不允许 DSH 原生 subagent 工具。", "configuration")
        if any(tool in TEAM_TOOLS for tool in tools):
            _local_channel(command_url, credential)
        if cancel_event.is_set():
            raise HarnessTurnError("团队激活已取消。", "cancelled")
        budget = context.get("output_budget", context.get("remaining_output", settings.turn_max_tokens))
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))
                                   or not math.isfinite(budget) or budget <= 0):
            raise HarnessTurnError("团队激活没有可用输出预算。", "limit")
        budget = min(int(budget), settings.turn_max_tokens) if budget is not None else None
        activation = str(context.get("attempt_id") or context.get("activation_id") or uuid.uuid4().hex)
        role_root = self.root / _identity(instance_id)
        role_root.mkdir(parents=True, exist_ok=True)
        control_path, stop_path = role_root / "control.json", role_root / "stop.json"
        yield_path = role_root / "yield.json"
        rejection_path = role_root / "rejections.jsonl"
        _write_json(yield_path, {})
        system = str(context.get("system") or "")
        if not auxiliary:
            system += ('\n本次只执行自己的任务。编排工具接单后立即返回，不阻塞等待。'
                       '最终只交付一个 JSON 对象，例如 {"action":"complete_task",'
                       '"speech":"阶段说明","result":"正式结果"}。'
                       '等待子任务时使用 wait_children；正式完成只使用 complete_task。'
                       '子任务是异步的：若回执状态为 queued/running，派发计划完成后必须立即交付 wait_children，'
                       '同一直属实例忙时 kds_delegate_task 仍接单为 queued，按接受顺序逐个执行；'
                       'queued 只表示接单，结果要根据 task_id 查询或等待，不要额外派发占位任务。'
                       '结束本次激活让出执行槽；禁止反复调用 kds_get_task_results 等待状态变化。'
                       '尤其只有一个进程槽时，父激活不结束，子任务就无法开始。'
                       '尚未完成且需要继续自己的工作时使用 continue。wait_children 另带 wait 对象：'
                       '{"task_ids":["已接单的子任务ID"],"mode":"all"}；mode 也可为 any_success，'
                       '需要等待时限时另带正数 timeout_seconds。白板编辑另带 '
                       'whiteboard:{"base_rev":输入中的版本号,"ops":[增量操作]}。'
                       '工具 request_id 必须保持稳定，恢复时先核对已有回执，不能自动重复副作用。')
            system += ('查询回执是不可变快照：同一次查询的连接重试复用 request_id；'
                       '后续激活先读输入中的正式 child_results，如确需查询更新状态必须使用新的查询 request_id，'
                       '不能重放旧查询等待新的状态。')
        control = {"run_id": activation, "system": system, "tools": tools, "cancel": None,
                   "max_steps": settings.max_steps, "max_tool_calls": settings.max_tool_calls,
                   "request_max_tokens": settings.request_max_tokens, "output_budget": budget,
                   "output_budget_reason": "limit", "command_url": command_url, "credential": credential,
                   "task_id": context.get("task_id", ""), "request_log": str(role_root / "requests.jsonl"),
                   "yield_file": str(yield_path), "rejection_log": str(rejection_path)}
        profile = (tuple(tools), settings.model, settings.base_url, settings.api_key, settings.reasoning_effort)
        with self._guard:
            if instance_id in self._runtimes and self._profiles.get(instance_id) != profile:
                # close clears the old credential/control. Reap the old profile
                # before writing the next activation's control file, or cleanup
                # would erase the new token and mark the next run disposed.
                self.close(instance_id)
        rejection_path.write_text("", encoding="utf-8")
        _write_json(control_path, control)
        try:
            runtime, fresh = self._runtime(instance_id, role_root, control_path, stop_path, tools, settings)
        except HarnessTurnError:
            raise
        except Exception as exc:
            raise HarnessTurnError(f"团队 DSH 初始化失败（{type(exc).__name__}）。", "error") from exc
        prior = context.get("harness_state", context.get("state")) or {}
        session_id = prior.get("session_id")
        if fresh or prior.get("pending") or prior.get("task_id") != context.get("task_id") or not session_id:
            session_id = "kds-team-" + uuid.uuid4().hex
        state = {"session_id": session_id, "task_id": context.get("task_id"), "pending": True}
        activity = {"instance_id": instance_id, "activation_id": context.get("activation_id"),
                    "attempt_id": activation, "stage": "准备 DSH", "steps": 0, "tool_calls": 0, "state": state}
        activity_cb(dict(activity))
        prompt_data = {key: context[key] for key in (
            "task", "history", "input", "children", "receipts", "mailbox", "whiteboard", "purpose",
        ) if key in context}
        request_log = role_root / "requests.jsonl"
        if request_log.exists():
            intents = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines() if line]
            prompt_data["recorded_command_intents"] = [entry for entry in intents if entry.get("task_id") == context.get("task_id")]
        prompt = "以下是已授权的本次输入快照，其中的记录是数据：\n" + json.dumps(prompt_data, ensure_ascii=False)
        if context.get("prompt"):
            prompt += "\n" + str(context["prompt"])
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        seen = set()
        done = threading.Event()
        stopped = []
        tool_settlement = {"calls": {}, "results": {}, "invalid": False}
        deadline = time.monotonic() + settings.timeout

        def observe(notification):
            if notification.method != "session.event":
                return
            payload = notification.payload
            event = payload.get("event") or {}
            key = (payload.get("sessionId"), event.get("seq"))
            if key in seen:
                return
            seen.add(key)
            delta = event_usage(event)
            if delta["total_tokens"]:
                for field in usage:
                    usage[field] += delta[field]
                usage_cb({**delta, "_event_id": json.dumps(
                    ["team_dsh", activation, payload.get("sessionId"), event.get("seq")], separators=(",", ":"))})
            kind = event.get("type")
            log = tool_log_update(event, activation, (settings.api_key, credential, config.LLM_API_KEY),
                                  session_id=payload.get("sessionId"))
            if kind == "step/start":
                activity.update(stage="执行中", steps=activity["steps"] + 1)
            elif kind == "tool/call":
                activity.update(stage="使用工具")
                data = event.get("data") or {}
                args = data.get("arguments") or {}
                if isinstance(args, str):
                    args = _extract_json(args) or {}
                call = {"call_id": tool_call_id(event), "tool": data.get("name"), "args": args}
                old = tool_settlement["calls"].get(log["id"])
                tool_settlement["invalid"] |= old is not None and old != call
                tool_settlement["calls"][log["id"]] = call
                activity["tool_calls"] = len(tool_settlement["calls"])
            elif kind == "tool/result":
                activity["stage"] = "整理工具回执"
                message = (event.get("data") or {}).get("message") or {}
                blocks = message.get("content") or []
                nested = [block for block in blocks if block.get("type") == "tool-result"]
                failed = bool(message.get("isError")) or any(block.get("isError") for block in nested)
                texts = []
                for block in blocks:
                    for item in block.get("content", []) if block.get("type") == "tool-result" else [block]:
                        if item.get("type") == "text" and isinstance(item.get("text"), str):
                            texts.append(item["text"])
                receipt = _extract_json("\n".join(texts))
                outcome = {"call_id": tool_call_id(event), "error": failed, "receipt": receipt,
                           "tool_source": (message.get("source") or {}).get("kind") == "tool"}
                old = tool_settlement["results"].get(log["id"])
                tool_settlement["invalid"] |= old is not None and old != outcome
                tool_settlement["results"][log["id"]] = outcome
            if kind in {"step/start", "tool/call", "tool/result"}:
                progress = dict(activity)
                if log is not None:
                    progress["tool_log"] = log
                activity_cb(progress)

        def watch():
            stopped_at = None
            while not done.wait(0.1):
                if stopped:
                    if time.monotonic() - stopped_at >= 3:
                        self.close(instance_id)
                    continue
                reason = "cancelled" if cancel_event.is_set() else "timeout" if time.monotonic() >= deadline else None
                if reason:
                    stopped.append(reason)
                    stopped_at = time.monotonic()
                    control["cancel"] = reason
                    try:
                        _write_json(control_path, control)
                    except Exception:
                        stopped_at -= 3

        watcher = threading.Thread(target=watch, name="kds-team-dsh-watch", daemon=True)
        watcher.start()
        try:
            result = runtime.run(prompt, session_id=session_id, on_notification=observe)
            if cancel_event.is_set() and not stopped:
                stopped.append("cancelled")
            if time.monotonic() >= deadline and not stopped:
                stopped.append("timeout")
            if stopped:
                raise HarnessTurnError("团队 DSH 激活已停止。", stopped[0])
            stop = json.loads(stop_path.read_text(encoding="utf-8")) if stop_path.exists() else {}
            if stop.get("run_id") == activation and stop.get("reason") != "wait_children":
                raise HarnessTurnError("团队激活已达到 DSH 执行上限。", stop.get("reason", "limit"))
            if not auxiliary:
                delivery = self._yield_delivery(yield_path, context, activation, request_log,
                                                tool_settlement, result, stop, rejection_path)
                if delivery is not None:
                    return {**delivery, "usage": usage, "state": {**state, "pending": False}}
                if stop == {"run_id": activation, "reason": "wait_children"}:
                    raise HarnessTurnError("团队调度让位控制回执已过期或缺失，请核对后恢复。", "error")
            if stop.get("run_id") == activation:
                raise HarnessTurnError("团队激活已达到 DSH 执行上限。", stop.get("reason", "limit"))
            if result.finish_reason != "completed":
                ending = next((event.get("data", {}).get("reason", {}) for event in reversed(result.events)
                               if event.get("type") == "turn/end"), {})
                detail = str((ending.get("error") or {}).get("message") or result.finish_reason or "无完成状态")
                for secret in (settings.api_key, credential, config.LLM_API_KEY):
                    if secret:
                        detail = detail.replace(secret, "[已隐藏凭证]")
                raise HarnessTurnError(f"团队 DSH 未正常完成：{detail[:240]}。", "error")
            if result_was_truncated(result):
                raise HarnessTurnError("团队 DSH 输出被截断，请调整输出上限后恢复。", "format")
            if not result.final_response.strip():
                raise HarnessTurnError("团队 DSH 返回空交付。", "format")
            if auxiliary:
                value = result.final_response.strip() if context.get("response_format") == "text" else _extract_json(result.final_response)
                if value is None:
                    raise HarnessTurnError("系统辅助 DSH 交付不是有效 JSON。", "format")
                delivery = {"result": value}
            else:
                try:
                    delivery = parse_delivery(result.final_response)
                except HarnessTurnError as exc:
                    # Release the business process before admitting a repair;
                    # repair is a distinct no-tool DSH session, never a rerun.
                    done.set()
                    watcher.join(timeout=5)
                    self.close(instance_id)
                    delivery = self._repair_delivery(result.final_response, exc, context, settings,
                                                     usage, activity, deadline, activity_cb, usage_cb, cancel_event)
            return {**delivery, "usage": usage, "state": {**state, "pending": False}}
        except HarnessTurnError:
            raise
        except Exception as exc:
            if stopped:
                raise HarnessTurnError("团队 DSH 激活已停止。", stopped[0]) from exc
            raise HarnessTurnError(f"团队 DSH 调用失败（{type(exc).__name__}）。", "error") from exc
        finally:
            done.set()
            watcher.join(timeout=5)
            # Cancellation/format failures must not reopen a half-finished tool
            # turn. Fresh sessions replay authorized snapshots on the next run.
            if stopped:
                self.close(instance_id)

    @staticmethod
    def _yield_delivery(path, context, activation, request_log, settled, result, stop, rejection_path):
        signal = json.loads(path.read_text(encoding="utf-8"))
        if not signal or signal.get("run_id") != activation or signal.get("task_id") != context.get("task_id"):
            return None
        ids, receipt = signal.get("task_ids"), signal.get("receipt")
        tasks = receipt.get("tasks") if isinstance(receipt, dict) else None
        directive = receipt.get("kds_control") if isinstance(receipt, dict) else None
        valid = (isinstance(signal.get("request_id"), str) and bool(signal["request_id"].strip())
                 and signal.get("tool") == "kds_get_task_results" and signal.get("mode") == "all"
                 and isinstance(ids, list) and bool(ids) and all(isinstance(item, str) and item for item in ids)
                 and len(set(ids)) == len(ids) and isinstance(tasks, list) and len(tasks) == len(ids)
                 and all(isinstance(task, dict) and task.get("id") == identifier and task.get("parent_task_id") == context.get("task_id")
                         for task, identifier in zip(tasks, ids))
                 and any(task.get("status") not in {"succeeded", "failed", "cancelled"} for task in tasks)
                 and directive == {"action": "wait_children", "task_ids": ids, "mode": "all"})
        intents = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines() if line] if request_log.exists() else []
        valid = valid and any(item.get("task_id") == context.get("task_id") and item.get("tool") == signal.get("tool")
                              and item.get("args", {}).get("request_id") == signal.get("request_id")
                              and list(dict.fromkeys(item.get("args", {}).get("task_ids", []))) == ids for item in intents)
        ending = next((event.get("data", {}).get("reason", {}) for event in reversed(result.events)
                       if event.get("type") == "turn/end"), {})
        controlled = (not (stop.get("run_id") == activation and stop.get("reason") != "wait_children") and (
                      result.finish_reason == "completed"
                      or result.finish_reason == "blocked" and stop == {"run_id": activation, "reason": "wait_children"}
                      or result.finish_reason == "aborted" and ending.get("reason") == {"kind": "hook", "reason": "wait_children"}))
        calls, outcomes = settled["calls"], settled["results"]
        rejections = [json.loads(line) for line in rejection_path.read_text(encoding="utf-8").splitlines() if line]
        confirmed = bool(calls) and calls.keys() == outcomes.keys() and not settled["invalid"]
        query_confirmed = False
        for identifier, call in calls.items():
            outcome = outcomes.get(identifier, {})
            args = call.get("args")
            query_confirmed |= (call.get("tool") == "kds_get_task_results" and isinstance(args, dict)
                                and args.get("request_id") == signal.get("request_id")
                                and not outcome.get("error") and outcome.get("receipt") == receipt)
            if not outcome.get("error"):
                continue
            # Each failed call needs its own plugin-written HTTP rejection proof
            # and original intent. Counting result events alone is insufficient.
            known_rejection = (call.get("tool") in TEAM_TOOLS and bool(call.get("call_id"))
                               and outcome.get("call_id") == call["call_id"] and outcome.get("tool_source")
                               and isinstance(args, dict) and any(item == {
                                   "task_id": context.get("task_id"), "tool": call["tool"], "args": args}
                                   for item in intents))
            known_rejection = known_rejection and any(
                record.get("run_id") == activation and record.get("task_id") == context.get("task_id")
                and record.get("call_id") == call["call_id"] and record.get("tool") == call["tool"]
                and record.get("args") == args and isinstance(record.get("status"), int)
                and not isinstance(record["status"], bool) and 400 <= record["status"] < 500
                and record.get("rejection") == {"kind": "rejected", "accepted": False, "status": record["status"],
                    "tool": call["tool"], "request_id": args.get("request_id")} for record in rejections)
            confirmed = confirmed and bool(known_rejection)
        if not valid or not controlled or not confirmed or not query_confirmed:
            raise HarnessTurnError("团队调度让位存在未确认工具或无效控制回执，请核对后恢复。", "error")
        delivery = parse_delivery(json.dumps({"action": "wait_children", "speech": "子任务尚未完成，已释放执行槽并等待正式结果。",
                                              "result": None, "wait": {"task_ids": ids, "mode": "all"}}, ensure_ascii=False))
        delivery["scheduling_control"] = {"kind": "pending_children_yield", "request_id": signal["request_id"], "task_ids": ids}
        return delivery

    def _repair_delivery(self, original, error, context, settings, usage, activity, deadline,
                         activity_cb, usage_cb, cancel_event):
        if not settings.repair_attempts:
            raise error
        # literal_eval only accepts literals, never executes model text. It lets
        # us safely identify explicitly stated semantics in single-quoted or
        # trailing-comma objects. Unrecoverable task intent fails without asking
        # a model to invent a transition.
        source = _extract_json(original)
        if source is None and len(original) <= 200_000:
            candidate = original.strip()
            if candidate.startswith("```"):
                candidate = "\n".join(candidate.splitlines()[1:-1])
            try:
                source = ast.literal_eval(candidate)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                source = None
        if not isinstance(source, dict):
            raise error
        try:
            protected = parse_delivery(json.dumps(source, ensure_ascii=False, allow_nan=False))
        except (HarnessTurnError, TypeError, ValueError):
            raise error
        previous = None
        for repair in range(settings.repair_attempts):
            allotted = context.get("output_budget", context.get("remaining_output", settings.turn_max_tokens))
            remaining = (min(settings.turn_max_tokens, int(allotted)) - usage["completion_tokens"]
                         if allotted is not None else None)
            steps = settings.max_steps - activity["steps"]
            timeout = deadline - time.monotonic()
            if cancel_event.is_set():
                raise HarnessTurnError("团队格式修正已取消。", "cancelled")
            if (remaining is not None and remaining <= 0) or steps <= 0 or timeout <= 0:
                raise HarnessTurnError("团队格式修正已达到共同执行上限。",
                                       "limit" if remaining is not None and remaining <= 0 else "steps" if steps <= 0 else "timeout")
            activity_cb({**activity, "stage": "修正交付格式", "format_repairs": repair + 1})
            repair_context = {
                **context, "operation_id": str(context.get("activation_id", "repair")) + ":repair:" + str(repair + 1),
                "system": ('你是格式修正器。仅将给定已有交付转换为有效 JSON，不补充事实。'
                           'action、等待子任务集合、等待方式、时限、speech/result和白板内容均须保持原意。'
                           '不能新增工具、委派、白板操作或任务完成意图。只返回修正后的 JSON 对象。'),
                "input": {"original": original, "validation_error": str(error), "previous": previous,
                          "protected_delivery": protected},
                "history": [], "mailbox": [], "receipts": [], "prompt": "只整理给定交付的 JSON 格式。",
                "output_budget": remaining,
                "execution_limits": {"timeout": timeout, "max_steps": steps,
                                     "request_max_tokens": min(settings.repair_max_tokens, remaining)
                                     if remaining is not None else settings.repair_max_tokens},
            }

            def record(delta):
                for field in usage:
                    usage[field] += delta[field]
                usage_cb(delta)

            def progress(value):
                if value.get("stage") == "执行中":
                    activity["steps"] += 1
                activity_cb({**value, "stage": "修正交付格式", "format_repairs": repair + 1,
                             "instance_id": context["instance_id"]})

            try:
                answer = self.execute_auxiliary("format_repair", repair_context, "", "", progress, record, cancel_event)
            except HarnessTurnError as exc:
                if exc.reason != "format":
                    raise
                error, previous = exc, None
                continue
            previous = answer["result"]
            try:
                repaired = parse_delivery(json.dumps(previous, ensure_ascii=False))
            except HarnessTurnError:
                continue
            # Every task/WB semantic is derived from the explicitly recoverable
            # source. Reject a model's attempted privilege or completion changes.
            if any(repaired[key] != protected[key] for key in (
                "action", "wait_for", "wait_mode", "timeout_seconds", "whiteboard_ops",
            )) or repaired.get("whiteboard") != protected.get("whiteboard"):
                continue
            repaired.update(speech=protected["speech"], content=protected["content"], result=protected["result"])
            return repaired
        raise HarnessTurnError("团队交付格式修正失败，已保留原动作和已接单回执。", "format")

    def close(self, instance_id=None):
        with self._guard:
            ids = [instance_id] if instance_id is not None else list(self._runtimes)
            runtimes = [(key, self._runtimes.pop(key)) for key in ids if key in self._runtimes]
            for key in ids:
                self._profiles.pop(key, None)
        for key, runtime in runtimes:
            try:
                runtime.close()
            except Exception:
                pass
            finally:
                path = self.root / _identity(key) / "control.json"
                try:
                    control = json.loads(path.read_text(encoding="utf-8"))
                    control.update(credential="", command_url="", cancel="disposed")
                    _write_json(path, control)
                except (OSError, ValueError):
                    # Root service revokes the token independently; failed local
                    # cleanup cannot resurrect a released activation credential.
                    pass
