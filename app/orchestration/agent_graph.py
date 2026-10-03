"""Finite, per-instance activations. Business receipts are the replay boundary."""
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime


class AgentState(TypedDict, total=False):
    run_id: str
    activation_id: str
    result_ref: str
    route: str
    outcome: str


@dataclass
class AgentContext:
    nodes: object


def build_agent_graph(checkpointer=None):
    graph = StateGraph(AgentState,context_schema=AgentContext)

    def bind(name):
        def call(state, runtime: Runtime[AgentContext]):
            return getattr(runtime.context.nodes,name)(state)
        return call

    for name in ('load_operation','execute','validate','commit'):
        graph.add_node(name,bind(name))
    graph.add_edge(START,'load_operation')
    graph.add_conditional_edges('load_operation',lambda s:s['route'],{'execute':'execute','validate':'validate','done':END})
    graph.add_edge('execute','validate')
    graph.add_edge('validate','commit')
    graph.add_edge('commit',END)
    return graph.compile(checkpointer=checkpointer)
