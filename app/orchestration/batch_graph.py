"""Bounded Send fan-out for auxiliary calls, with a complete-batch barrier."""

from contextlib import nullcontext
from dataclasses import dataclass, field
import sqlite3
import threading
from typing import Annotated, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Overwrite, Send

from app.repositories.orchestration import RepositoryConflict


CRITICAL_ERRORS = (sqlite3.Error, RepositoryConflict)


def is_critical_error(error: Exception) -> bool:
    # The runtime annotates its own persistence/unsafe-recovery exceptions to
    # avoid a circular import between reusable graphs and the runner.
    return isinstance(error, CRITICAL_ERRORS) or bool(getattr(error, "_orchestration_fatal", False))


def merge_results(left: dict, right: dict) -> dict:
    """Merge by participant, accepting identical replay but rejecting conflicts.

    Initialization uses Overwrite, so an old batch cannot supply missing current
    results. Conflicting updates are an error instead of arrival-order dependent.
    """
    merged = dict(left or {})
    for agent_id, result in (right or {}).items():
        if agent_id in merged and merged[agent_id] != result:
            raise ValueError(f"角色 {agent_id} 的批次结果冲突")
        merged[agent_id] = result
    return merged


class BatchState(TypedDict, total=False):
    batch_id: str
    jobs: list[dict]
    results_by_agent: Annotated[dict[str, dict], merge_results]
    ordered_results: list[dict]
    status: str
    error: str | None


class BatchBranch(TypedDict):
    batch_id: str
    job: dict


@dataclass
class BatchRuntime:
    """Process-local resources. None of these enter serialized graph state."""

    call: Callable[[dict], dict]
    on_result: Callable[[dict, dict], None] | None = None
    limiter: object | None = None
    _dispatch_error: Exception | None = field(default=None, init=False, repr=False)
    _dispatch_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def stop_dispatch(self, error: Exception):
        with self._dispatch_lock:
            if self._dispatch_error is None:
                self._dispatch_error = error

    def check_dispatch(self):
        with self._dispatch_lock:
            error = self._dispatch_error
        if error is not None:
            raise error


def initialize_batch(state: BatchState) -> dict:
    batch_id = state.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("批次必须有稳定的 batch_id")
    jobs = state.get("jobs") or []
    ids = [job.get("agent_id") for job in jobs]
    if any(not isinstance(agent_id, str) or not agent_id for agent_id in ids):
        raise ValueError("每个批次任务必须有 agent_id")
    if len(ids) != len(set(ids)):
        raise ValueError("同一批次不能包含重复角色")
    current = {
        agent_id: dict(result)
        for agent_id, result in (state.get("results_by_agent") or {}).items()
        if agent_id in ids and result.get("batch_id") == batch_id
        and result.get("status") != "error" and not result.get("error")
    }
    return {
        "jobs": jobs,
        "results_by_agent": Overwrite(current), "ordered_results": [],
        "status": "running", "error": None,
    }


def dispatch_batch(state: BatchState):
    results = state.get("results_by_agent") or {}
    pending = [job for job in state["jobs"] if job["agent_id"] not in results]
    if not pending:
        return "collect"
    return [Send("call_agent", {"batch_id": state["batch_id"], "job": job}) for job in pending]


def call_agent(state: BatchBranch, runtime: Runtime[BatchRuntime]) -> dict:
    job = dict(state["job"])
    job["batch_id"] = state["batch_id"]
    runtime.context.check_dispatch()
    with runtime.context.limiter if runtime.context.limiter is not None else nullcontext():
        # A branch may have waited for the global permit while another branch
        # lost persistence. Do not start another external request in that case.
        runtime.context.check_dispatch()
        try:
            value = runtime.context.call(job)
            if not isinstance(value, dict):
                raise TypeError("辅助调用结果必须为 JSON 对象")
            result = dict(value)
        except CRITICAL_ERRORS as exc:
            # A lost durable result or revoked execution claim must stop dispatch.
            runtime.context.stop_dispatch(exc)
            raise
        except Exception as exc:
            if is_critical_error(exc):
                runtime.context.stop_dispatch(exc)
                raise
            # Preserve successful sibling calls and any known failed usage.
            result = {"status": "error", "error": str(exc)}
            usage = getattr(exc, "usage", None)
            if isinstance(usage, dict):
                result["usage"] = dict(usage)
    result["batch_id"] = state["batch_id"]
    result["agent_id"] = job["agent_id"]
    if runtime.context.on_result is not None:
        try:
            runtime.context.on_result(job, result)
        except Exception as exc:
            runtime.context.stop_dispatch(exc)
            raise
    return {"results_by_agent": {job["agent_id"]: result}}


def collect_batch(state: BatchState) -> dict:
    results = state.get("results_by_agent") or {}
    jobs = state.get("jobs") or []
    missing = [job["agent_id"] for job in jobs if job["agent_id"] not in results]
    if missing:
        raise RuntimeError(f"批次尚未完成，缺少角色：{', '.join(missing)}")
    ordered = [dict(results[job["agent_id"]]) for job in jobs]
    if any(result.get("batch_id") != state["batch_id"] for result in ordered):
        raise RuntimeError("批次汇合时发现过期结果")
    errors = [str(result.get("error") or "辅助调用失败") for result in ordered
              if result.get("status") == "error" or result.get("error")]
    return {
        "ordered_results": ordered,
        "status": "error" if errors else "completed",
        "error": "\n".join(errors) if errors else None,
    }


def _build_graph(state_schema, collector, *, checkpointer=None):
    builder = StateGraph(state_schema, context_schema=BatchRuntime)
    builder.add_node("initialize_batch", initialize_batch)
    builder.add_node("call_agent", call_agent)
    builder.add_node("collect", collector)
    builder.add_edge(START, "initialize_batch")
    builder.add_conditional_edges("initialize_batch", dispatch_batch, ["call_agent", "collect"])
    builder.add_edge("call_agent", "collect")
    builder.add_edge("collect", END)
    return builder.compile(checkpointer=checkpointer)


def build_batch_graph(*, checkpointer=None):
    return _build_graph(BatchState, collect_batch, checkpointer=checkpointer)
