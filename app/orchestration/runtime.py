"""Thread ownership and resource lifecycle for durable finite graphs."""
import copy
import random
import sqlite3
import threading
import time
import traceback
import uuid

from app.engine import ConversationRunner, _now
from app.orchestration.backends import AgentTurnExecutor
from app.orchestration.batch_graph import build_batch_graph, BatchRuntime
from app.orchestration.checkpointer import get_saver
from app.orchestration.nodes import TurnNodes
from app.orchestration.summary_graph import build_summary_graph, SummaryRuntime
from app.orchestration.turn_graph import build_turn_graph, TurnContext
from app.orchestration.vote_graph import build_vote_graph
from app.repositories.orchestration import OrchestrationRepository, RepositoryConflict
from app import config as settings


_limiters = {}
_limiters_lock = threading.Lock()


class OrchestrationPersistenceError(RuntimeError):
    """A failed durable write must never be treated as a model failure."""

    _orchestration_fatal = True


class UnsafeRecoveryError(RuntimeError):
    """An external effect has no durable result that can safely be published."""

    _orchestration_fatal = True


def shared_limiter(size):
    with _limiters_lock:
        return _limiters.setdefault(size, threading.BoundedSemaphore(size))


class GraphRunner(ConversationRunner):
    """Compatibility facade; workflow decisions live in graph nodes.

    The original runner supplies the public state contract and control checks.
    This runtime adds one owner, durable effects and attempt-specific callbacks.
    """
    graph_version = "langgraph-v1"
    schema_version = 1

    def __init__(self, conv_id, config_id, name, config, llm, *, repository=None, saver=None):
        super().__init__(conv_id, config_id, name, config, llm)
        self._lock = threading.RLock()
        self._persist_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self.repository = repository or OrchestrationRepository()
        self.saver = saver if saver is not None else get_saver()
        self.state_rev = 0
        self.runner_epoch = 0
        self._deleted = False
        self._unsafe_operations = set()
        self._allow_uncertain_retry = False
        self.auxiliary_limiter = shared_limiter(settings.AUXILIARY_MAX_CONCURRENCY)
        self.executor = AgentTurnExecutor()
        self.turn_graph = build_turn_graph(self.saver)
        self.score_graph = build_batch_graph(checkpointer=self.saver)
        self.vote_graph = build_vote_graph(checkpointer=self.saver)
        self.summary_graph = build_summary_graph(checkpointer=self.saver)
        self.nodes = TurnNodes(self)

    @classmethod
    def from_payload(cls, conv, llm, *, repository=None, saver=None):
        # Restore business fields with the same legacy defaults, without calling
        # a model or creating historical checkpoints.
        legacy = ConversationRunner.from_payload(conv, llm)
        runner = cls(conv["id"], conv.get("config_id"), conv.get("name", "对话"),
                     legacy.to_dict()["config"], llm, repository=repository, saver=saver)
        infrastructure = {k: runner.__dict__[k] for k in (
            "_lock", "_persist_lock", "_lifecycle_lock", "repository", "saver", "turn_graph",
            "score_graph", "vote_graph", "summary_graph", "nodes", "auxiliary_limiter", "executor")}
        runner.__dict__.update(legacy.__dict__)
        runner.__dict__.update(infrastructure)
        runner.state_rev = int(conv.get("state_rev") or 0)
        runner.runner_epoch = int(conv.get("runner_epoch") or 0)
        runner._deleted = False
        runner._unsafe_operations = set()
        runner._allow_uncertain_retry = False
        runner.pending_human_message = conv.get("pending_human_message")
        runner.pending_human_target = conv.get("pending_human_target")
        return runner

    def to_dict(self, include_tool_logs=True):
        with self._lock:
            data = super().to_dict(include_tool_logs)
            data.update(orchestration_backend="langgraph", schema_version=self.schema_version,
                        graph_version=self.graph_version, runner_epoch=self.runner_epoch, state_rev=self.state_rev,
                        pending_human_message=self.pending_human_message, pending_human_target=self.pending_human_target)
            return data

    def _sync_usage(self):
        record = self.repository.get_conversation(self.id)
        if record is None:
            raise RepositoryConflict("对话已删除")
        self.state_rev = record["state_rev"]
        self.total_prompt_tokens = record.get("total_prompt_tokens", 0)
        self.total_output_tokens = record.get("total_output_tokens", 0)
        return record

    def _persist(self):
        with self._lock:
            if self._deleted:
                return
            self.state_rev = self.repository.save_snapshot(
                self.id, self.to_dict(), expected_rev=self.state_rev, runner_epoch=self.runner_epoch)

    def _claim(self):
        with self._lock:
            self.runner_epoch = self.repository.claim(self.id)
            self._sync_usage()

    def start(self):
        with self._lifecycle_lock:
            if self.is_alive() or self._deleted:
                return
            self._claim()
            try:
                super().start()
            except Exception as exc:
                self._thread = None
                self._pause_segment()
                with self._lock:
                    self.status, self.paused_reason = "paused", "error"
                    self.error = f"讨论线程启动失败：{exc}"
                self._persist_after_failure()
                raise

    def resume(self):
        with self._lifecycle_lock:
            with self._lock:
                if (self._deleted or self.status != "paused" or self._limit_reached_nolock()
                        or self._vote_in_progress or self._summary_in_progress):
                    return False
            if self.is_alive():
                self._thread.join(5)
                if self.is_alive():
                    return False
            self._claim()
            self._allow_uncertain_retry = True
            try:
                resumed = super().resume()
            except Exception as exc:
                self._thread = None
                self._pause_segment()
                with self._lock:
                    self.status, self.paused_reason = "paused", "error"
                    self.error = f"讨论线程启动失败：{exc}"
                self._persist_after_failure()
                resumed = False
            if not resumed:
                self._allow_uncertain_retry = False
            return resumed

    def summarize_now(self):
        with self._lifecycle_lock:
            if self._deleted:
                return False
            try:
                return super().summarize_now()
            except Exception as exc:
                with self._lock:
                    self.status = "completed"
                    self.summary = "总结中断：保存或执行发生异常，请检查状态记录。"
                    self.error = f"总结中断：{exc}"
                    self._summary_in_progress = False
                self._persist_after_failure()
                return True

    def human_say(self, content, target=None):
        with self._lock:
            if self._deleted or self.status not in {"running", "paused"} or self._summary_in_progress:
                return None
            target_idx = self._resolve_agent_idx(target)
            command = self.repository.reserve_command(self.id, {"content": content, "target": target_idx})
            self._sync_usage()
            if self.status == "running":
                self.pending_human_message = content
                self.pending_human_target = target_idx
                self._persist()
                return "reserved"
            self._abandon_outdated_operations()
            child = "human:" + command["command_id"]
            self.repository.create_operation(child, self.id, "human", input=command["payload"],
                                             runner_epoch=self.runner_epoch)
            def apply():
                self._append_message_nolock("human", "人类", content, 0)
                self.messages[-1]["operation_id"] = child
                self.turn += 1
                if target_idx is not None:
                    self._forced_next_idx = target_idx
                self.pending_human_message = self.pending_human_target = None
            self._effect(child, apply, consume_command_id=command["command_id"])
            return "appended"

    def reserve(self, content):
        self.human_say(content)

    def interrupt(self):
        with self._lock:
            if self._deleted:
                return
            super().interrupt()
            self.repository.reserve_command(self.id, {}, kind="interrupt")
            self._sync_usage()
            self._persist()

    def _abandon_outdated_operations(self):
        for op in self.repository.list_operations(self.id, kind="advance", statuses=("prepared", "running", "result_ready", "uncertain", "abandoned"), include_payload=False):
            turn = self.repository.get_operation(op["operation_id"] + ":turn", include_payload=False)
            if turn is None or turn["status"] != "committed":
                self._abandon_operation_tree(op["operation_id"])

    def _abandon_operation_tree(self, operation):
        """Discard public effects from outdated inputs; retain attempts and usage."""
        with self._lock:
            for op in self.repository.list_operation_tree(self.id, operation):
                if op["status"] not in {"committed", "abandoned"}:
                    self.repository.update_operation(op["operation_id"], status="abandoned",
                                                     runner_epoch=self.runner_epoch)

    def _effect(self, operation_id, apply, **kwargs):
        with self._lock:
            op = self.repository.get_operation(operation_id, include_payload=False)
            if self._deleted or op is None or op["status"] == "abandoned":
                raise RepositoryConflict("操作已失效")
            if operation_id in self._unsafe_operations:
                raise UnsafeRecoveryError("本次调用的关键记录保存失败，不能发布迟到结果")
            if op["status"] == "committed":
                return
            if op["runner_epoch"] != self.runner_epoch:
                self.repository.update_operation(operation_id, runner_epoch=self.runner_epoch)
            fields = ("messages", "votes", "heat", "turn", "last_agent_idx", "whiteboard_content",
                      "whiteboard_rev", "whiteboard_last_editor", "harness_state", "harness_activity",
                      "harness_logs", "harness_log_rev", "_rr_index", "_forced_next_idx",
                      "pending_human_message", "pending_human_target", "_end_vote_block_until_turn", "summary")
            before = {key: copy.deepcopy(getattr(self, key)) for key in fields}
            try:
                apply()
                self.state_rev = self.repository.commit(operation_id, self.to_dict(), self.state_rev,
                                                        self.runner_epoch, **kwargs)
            except Exception:
                self.__dict__.update(before)
                raise

    def _usage(self, operation, attempt, source, usage):
        with self._lock:
            event = usage.get("_event_id") or attempt + ":usage"
            try:
                self.repository.record_usage(event, self.id, operation_id=operation, attempt_id=attempt,
                                             source=source, prompt_tokens=usage.get("prompt_tokens", 0),
                                             completion_tokens=usage.get("completion_tokens", 0))
            except Exception as exc:
                self._unsafe_operations.add(operation)
                self._interrupt_requested = True
                raise OrchestrationPersistenceError("模型用量保存失败，已停止推进") from exc
            if not self._deleted:
                self._sync_usage()

    def _progress(self, progress, epoch, operation=None):
        with self._lock:
            try:
                current = self.repository.get_conversation(self.id) if not self._deleted else None
                if operation is not None:
                    op = self.repository.get_operation(operation, include_payload=False)
                    parent = self.repository.get_operation(op["parent_operation_id"], include_payload=False) if op and op["parent_operation_id"] else None
                    if (op is None or op["status"] in {"committed", "abandoned", "result_ready"}
                            or parent and parent["status"] == "abandoned"):
                        return
                if (current is not None and epoch == self.runner_epoch
                        and epoch == int(current.get("runner_epoch", 0))):
                    self._harness_progress(progress)
            except Exception as exc:
                if operation is not None:
                    self._unsafe_operations.add(operation)
                self._interrupt_requested = True
                raise OrchestrationPersistenceError("工具进度保存失败，已停止推进") from exc

    def _adopt_operation(self, operation):
        """Accept durable results, including the window before the op receipt write."""
        with self._lock:
            op = self.repository.get_operation(operation)
            if op is None or op["status"] in {"committed", "abandoned"}:
                return op
            if operation in self._unsafe_operations:
                raise UnsafeRecoveryError("本次调用的关键记录保存失败，不能发布结果")
            attempts = self.repository.list_attempts(operation)
            ready = [attempt for attempt in attempts
                     if attempt["status"] == "result_ready" and attempt.get("result") is not None]
            fields = {"runner_epoch": self.runner_epoch}
            if op.get("result") is None and ready:
                fields.update(status="result_ready", result=ready[-1]["result"])
            elif op.get("result") is None and (
                    any(attempt["status"] in {"running", "uncertain"} for attempt in attempts)):
                raise UnsafeRecoveryError("上次模型或工具调用没有保存确定结果，需先对账或通过人工插话开始新回合")
            elif op["status"] == "uncertain":
                # Roots have no external attempts. An explicitly failed attempt
                # is also distinct from an interrupted/unknown external effect.
                fields["status"] = "prepared"
            if all(op.get(key) == value for key, value in fields.items()):
                return op
            return self.repository.update_operation(operation, **fields)

    def _finalize_auxiliary_child(self, operation):
        with self._lock:
            child = self.repository.get_operation(operation)
            if child is None or child["status"] in {"committed", "abandoned"}:
                return
            if child["status"] == "result_ready":
                self._effect(operation, lambda: None)
                return
            attempts = self.repository.list_attempts(operation)
            if attempts and all(attempt["status"] == "failed" for attempt in attempts):
                self.repository.update_operation(operation, status="abandoned", runner_epoch=self.runner_epoch)

    def _external(self, operation, kind, inputs, call, *, parent=None):
        with self._lock:
            if self._deleted:
                raise RepositoryConflict("对话已删除")
            if parent:
                root = self.repository.get_operation(parent, include_payload=False)
                if root and root["status"] == "abandoned":
                    raise RepositoryConflict("操作已失效")
            op = self.repository.create_operation(operation, self.id, kind, input=inputs,
                                                  parent_operation_id=parent, runner_epoch=self.runner_epoch)
            op = self._adopt_operation(operation)
            if op is None or op["status"] == "abandoned":
                raise UnsafeRecoveryError("操作已失效，不能重新调用模型")
            if op.get("result") is not None and op["status"] in {"result_ready", "committed"}:
                return op["result"]
            attempt = uuid.uuid4().hex
            epoch = self.runner_epoch
            self.repository.create_attempt(attempt, operation, epoch, status="running")
            self.repository.update_operation(operation, status="running", runner_epoch=epoch)
        try:
            result = call(attempt)
            if operation in self._unsafe_operations:
                raise OrchestrationPersistenceError("本次调用的关键记录保存失败，不能发布迟到结果")
            if not result.get("usage_reported"):
                self._usage(operation, attempt, kind, result.get("usage") or {})
            with self._lock:
                self.repository.update_attempt(attempt, status="result_ready", result=result)
                if self._deleted or self.runner_epoch != epoch:
                    raise RepositoryConflict("执行代次已失效")
                self.repository.update_operation(operation, status="result_ready", result=result)
            return result
        except Exception as exc:
            known_usage = getattr(exc, "usage", None)
            if isinstance(known_usage, dict) and (kind != "turn" or self.harness is None):
                try:
                    # Failed auxiliary/direct attempts may still report settled
                    # consumption. Use the same attempt key as successful usage.
                    self._usage(operation, attempt, kind, known_usage)
                except Exception as accounting_error:
                    exc = accounting_error
            fatal = (operation in self._unsafe_operations or getattr(exc, "_orchestration_fatal", False)
                     or isinstance(exc, (sqlite3.Error, RepositoryConflict)))
            try:
                saved_attempt = next((item for item in self.repository.list_attempts(operation)
                                      if item["attempt_id"] == attempt), None)
                if saved_attempt is None or saved_attempt["status"] != "result_ready":
                    self.repository.update_attempt(attempt, status="uncertain" if fatal else "failed", error=str(exc))
                if fatal:
                    self.repository.update_operation(operation, status="uncertain", error={
                        "kind": "persistence", "message": str(exc),
                        "unsafe_usage": operation in self._unsafe_operations,
                    }, runner_epoch=epoch)
            except Exception:
                # A still-running durable attempt becomes uncertain on restart.
                # Preserve the initial diagnostic when storage remains broken.
                pass
            if fatal:
                raise OrchestrationPersistenceError("模型调用记录保存失败，已停止推进") from exc
            raise

    def _graph_config(self, suffix=None):
        thread = "conversation:" + self.id
        if suffix:
            thread += ":" + suffix
        return {"configurable": {"thread_id": thread}, "recursion_limit": 32,
                "max_concurrency": settings.GRAPH_MAX_CONCURRENCY}

    def _prepare_operation(self):
        with self._lock:
            pending = self.repository.list_operations(self.id, kind="advance", statuses=("prepared", "running", "result_ready", "uncertain"))
            if pending:
                op = pending[-1]
                if op["input"].get("graph_version") != self.graph_version:
                    raise UnsafeRecoveryError("图版本不兼容，请先对账未完成操作")
                op = self._adopt_operation(op["operation_id"])
                child = self.repository.get_operation(op["operation_id"] + ":turn", include_payload=False)
                if child is None or child["status"] != "committed":
                    if (op["input"]["message_count"] != len(self.messages)
                            or op["input"]["whiteboard_rev"] != self.whiteboard_rev):
                        self._abandon_operation_tree(op["operation_id"])
                    else:
                        return op
                else:
                    return op
            operation_id = uuid.uuid4().hex
            return self.repository.create_operation(operation_id, self.id, "advance", status="running",
                runner_epoch=self.runner_epoch, input={
                    "seed": random.getrandbits(64), "message_count": len(self.messages),
                    "whiteboard_rev": self.whiteboard_rev, "history": self._history(),
                    "graph_version": self.graph_version,
                    "score_jobs": [{"agent_id": a["id"], "agent_name": a["name"],
                                    "system": self._build_score_system(a), "history_text": self._log_text(),
                                    "turn": self.turn} for a in self.agents],
                })

    def _run(self):
        try:
            while not self._deleted:
                op = self._prepare_operation()
                config = self._graph_config()
                checkpoint = self.turn_graph.get_state(config)
                resumable = bool(checkpoint.next) and checkpoint.values.get("operation_id") == op["operation_id"]
                if op["input"].get("graph_version") != self.graph_version:
                    raise RuntimeError("图版本不兼容，请先对账未完成操作")
                # Durable receipts can rebuild a lost checkpoint; uncertain
                # external calls must never be transparently repeated.
                retry_turn = False
                for child in self.repository.list_operations(self.id, parent_operation_id=op["operation_id"], include_payload=False):
                    if child["kind"] in {"turn", "score"}:
                        try:
                            self._adopt_operation(child["operation_id"])
                        except UnsafeRecoveryError:
                            error = child.get("error")
                            unsafe_usage = isinstance(error, dict) and error.get("unsafe_usage")
                            if (not self._allow_uncertain_retry or not resumable or self.harness is None
                                    or child["kind"] != "turn" or unsafe_usage
                                    or child["operation_id"] in self._unsafe_operations):
                                raise
                            with self._lock:
                                self._abandon_operation_tree(op["operation_id"])
                                if self._forced_next_idx is None:
                                    turn_input = self.repository.get_operation(child["operation_id"])["input"]
                                    self._forced_next_idx = self._resolve_agent_idx(turn_input["agent"]["id"])
                                self._persist()
                            self._allow_uncertain_retry = False
                            retry_turn = True
                            break
                if retry_turn:
                    continue
                data = None if resumable else {"conversation_id": self.id, "operation_id": op["operation_id"]}
                result = self.turn_graph.invoke(data, config, context=TurnContext(self.nodes), durability="sync")
                if result["outcome"] != "continue":
                    break
        except Exception as exc:
            self._pause_segment()
            with self._lock:
                self.status = "paused"
                self.paused_reason = "error"
                self.error = f"讨论已暂停：{exc}\n{traceback.format_exc()}"
            # Keep the original ready result and pending checkpoint for resume.
            self._persist_after_failure()
        finally:
            if self.harness is not None:
                self.harness.close()
            with self._lock:
                self.harness_activity = None
                self._finish_tool_logs_nolock()
            self._persist_after_failure()

    def _persist_after_failure(self):
        try:
            self._persist()
        except Exception as exc:
            # Critical writes have already stopped dispatch. Preserve a visible
            # in-memory error and a local diagnostic without masking it.
            with self._lock:
                self.status = "paused" if self.status != "completed" else "completed"
                self.paused_reason = "error"
                self.error = f"状态保存失败：{exc}"
            import logging
            logging.getLogger(__name__).exception("Conversation persistence failed: %s", self.id)

    def _new_end_vote(self, vote_id):
        return {"id": vote_id, "kind": "end", "question": "有角色提议结束本次对话，是否结束？",
                "options": ["同意结束", "继续讨论"], "votes_per_person": 1,
                "status": "running", "created_at": _now(), "results": {"1": 0, "2": 0},
                "ballots": [], "agreed": False, "error": None}

    def start_vote(self, question, options, votes_per_person):
        with self._lifecycle_lock:
            if self._deleted:
                return None
            try:
                return super().start_vote(question, options, votes_per_person)
            except Exception as exc:
                with self._lock:
                    self._vote_in_progress = False
                    self._vote_thread = None
                    if self.votes and self.votes[-1]["status"] in {"pending", "running"}:
                        self.votes[-1].update(status="error", error=f"投票启动失败：{exc}")
                    self.error = f"投票启动失败：{exc}"
                self._persist_after_failure()
                return None

    def _run_vote_inner(self, vote_id):
        try:
            self._execute_vote(vote_id)
        except Exception as exc:
            with self._lock:
                vote = next((v for v in self.votes if v["id"] == vote_id), None)
                if vote:
                    vote.update(status="error", error=str(exc))
            self._persist_after_failure()

    def _execute_vote(self, vote_id, parent=None):
        operation = (parent + ":vote" if parent else "vote:" + self.id + ":" + vote_id)
        with self._lock:
            vote = next(v for v in self.votes if v["id"] == vote_id)
            if vote["status"] in {"completed", "error"}:
                return bool(vote.get("agreed"))
            jobs = [{"agent_id": a["id"], "agent_name": a["name"],
                     "system": self._build_vote_system(a, vote["question"], vote["options"], vote["votes_per_person"]),
                     "history_text": self._log_text(), "question": vote["question"], "options": vote["options"],
                     "votes_per_person": vote["votes_per_person"]} for a in self.agents]
            op = self.repository.create_operation(operation, self.id, "vote", parent_operation_id=parent,
                runner_epoch=self.runner_epoch, input={"jobs": jobs, "options": vote["options"], "kind": vote.get("kind", "normal")})
            vote["status"] = "running"
            self._persist()

        def call(job):
            def generate(attempt):
                result, usage = self.llm.vote(job["agent_name"], job["system"], job["history_text"],
                                              job["question"], job["options"], job["votes_per_person"])
                return {**result, "usage": usage}
            return self._external(operation + ":" + job["agent_id"], "ballot", job, generate, parent=operation)

        def progress(job, result):
            if result.get("error"):
                return
            with self._lock:
                if self._deleted:
                    return
                saved = {b["agent_id"]: b for b in vote["ballots"]}
                saved[job["agent_id"]] = {"agent_id": job["agent_id"], "agent_name": job["agent_name"],
                    "choices": [str(c) for c in result.get("choices") or []], "reason": result.get("reason") or ""}
                vote["ballots"] = [saved[a["id"]] for a in self.agents if a["id"] in saved]
                vote["results"] = {str(i + 1): sum(b["choices"].count(str(i + 1)) for b in vote["ballots"])
                                   for i in range(len(vote["options"]))}
                self._persist()
        data = self.vote_graph.invoke({"batch_id": operation, **op["input"]},
            self._graph_config("vote:" + vote_id), context=BatchRuntime(call, progress, self.auxiliary_limiter), durability="sync")
        with self._lock:
            def apply():
                vote.update(status=data["status"], error=data.get("error"), ballots=data["ballots"],
                            results=data["results"])
                if vote.get("kind") == "end":
                    vote["agreed"] = data["agreed"]
            self._effect(operation, apply)
            for job in op["input"]["jobs"]:
                child_id = operation + ":" + job["agent_id"]
                self._finalize_auxiliary_child(child_id)
        return bool(data.get("agreed"))

    def _finish(self, status, reason=None):
        operation = "summary:" + uuid.uuid4().hex
        with self._lock:
            self.status = "completed"
            self.ended_at = _now()
            self.ended_reason = reason
            self.repository.create_operation(operation, self.id, "summary", runner_epoch=self.runner_epoch,
                                              input={"log": self._log_text()}, status="running")
            self._persist()
        def generate(log):
            result = self._external(operation + ":model", "summary_model", {"log": log},
                lambda attempt: self._generate_summary(log), parent=operation)
            return result["summary"], result["usage"]
        def commit(result):
            self.repository.update_operation(operation, result=result, status="result_ready")
            self._effect(operation, lambda: setattr(self, "summary", result["summary"]))
            self._finalize_auxiliary_child(operation + ":model")
        self.summary_graph.invoke({"operation_id": operation}, self._graph_config("summary:" + operation),
            context=SummaryRuntime(lambda: self.repository.get_operation(operation)["input"]["log"], generate, commit,
                                   limiter=self.auxiliary_limiter),
            durability="sync")

    def _generate_summary(self, log):
        summary, usage = self.llm.summarize(log)
        return {"summary": summary, "usage": usage}

    def delete(self):
        with self._lifecycle_lock:
            with self._lock:
                self._interrupt_requested = True
                self._deleted = True
                self.runner_epoch += 1
            # Late workers can finish only their local cleanup; repository writes
            # cannot recreate a deleted conversation.
            ok = self.repository.delete_conversation(self.id)
            if self._thread and self._thread.is_alive():
                self._thread.join(5)
            if self._vote_thread and self._vote_thread.is_alive():
                self._vote_thread.join(5)
            if hasattr(self.saver, "delete_conversation"):
                self.saver.delete_conversation(self.id)
            else:
                prefix = "conversation:" + self.id
                threads = {prefix}
                for checkpoint in self.saver.list(None):
                    thread = checkpoint.config["configurable"]["thread_id"]
                    if thread == prefix or thread.startswith(prefix + ":"):
                        threads.add(thread)
                for thread in threads:
                    self.saver.delete_thread(thread)
            return ok
