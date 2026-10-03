"""One bounded coordination pass; waiting happens outside the graph."""
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime


class DispatchState(TypedDict, total=False):
    run_id: str
    slots: int
    activations: list[str]


@dataclass
class DispatchContext:
    repository: object
    blocked_instance_ids: tuple[str,...] = ()


def build_dispatch_graph(checkpointer=None):
    graph = StateGraph(DispatchState,context_schema=DispatchContext)

    def reconcile_and_claim(state, runtime: Runtime[DispatchContext]):
        return {'activations':runtime.context.repository.dispatch(state['run_id'],state.get('slots',1),
                blocked_instance_ids=runtime.context.blocked_instance_ids)}

    graph.add_node('reconcile_and_claim',reconcile_and_claim)
    graph.add_edge(START,'reconcile_and_claim')
    graph.add_edge('reconcile_and_claim',END)
    return graph.compile(checkpointer=checkpointer)
