"""Finite manual-summary workflow using process-local service callbacks."""

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from .batch_graph import CRITICAL_ERRORS, is_critical_error


class SummaryState(TypedDict, total=False):
    operation_id: str
    public_log: str
    summary: str
    usage: dict
    status: str
    error: str | None


@dataclass
class SummaryRuntime:
    load_public_log: Callable[[], str]
    generate_summary: Callable[[str], tuple[str, dict]]
    commit_summary: Callable[[dict], None]
    on_result: Callable[[dict], None] | None = None
    limiter: object | None = None


def load_public_log(state: SummaryState, runtime: Runtime[SummaryRuntime]) -> dict:
    return {"public_log": runtime.context.load_public_log(), "status": "running", "error": None}


def generate_summary(state: SummaryState, runtime: Runtime[SummaryRuntime]) -> dict:
    # A model failure still completes the conversation under the existing KDS
    # contract; a persistence failure remains a hard orchestration error.
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    error = None
    if not state["public_log"].strip():
        content = "本次对话没有任何发言。"
    else:
        try:
            with runtime.context.limiter if runtime.context.limiter is not None else nullcontext():
                content, usage = runtime.context.generate_summary(state["public_log"])
        except CRITICAL_ERRORS:
            raise
        except Exception as exc:
            if is_critical_error(exc):
                raise
            content, error = f"总结失败：{exc}", str(exc)
            known_usage = getattr(exc, "usage", None)
            if isinstance(known_usage, dict):
                usage = dict(known_usage)
    result = {"summary": content, "usage": usage, "error": error, "status": "completed"}
    if runtime.context.on_result is not None:
        runtime.context.on_result(result)
    return result


def commit_summary(state: SummaryState, runtime: Runtime[SummaryRuntime]) -> dict:
    runtime.context.commit_summary({
        "operation_id": state.get("operation_id"), "summary": state["summary"],
        "usage": state["usage"], "status": state["status"], "error": state.get("error"),
    })
    return {}


def build_summary_graph(*, checkpointer=None):
    builder = StateGraph(SummaryState, context_schema=SummaryRuntime)
    builder.add_node("load_public_log", load_public_log)
    builder.add_node("generate_summary", generate_summary)
    builder.add_node("commit_summary", commit_summary)
    builder.add_edge(START, "load_public_log")
    builder.add_edge("load_public_log", "generate_summary")
    builder.add_edge("generate_summary", "commit_summary")
    builder.add_edge("commit_summary", END)
    return builder.compile(checkpointer=checkpointer)
