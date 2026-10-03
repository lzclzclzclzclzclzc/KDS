"""Finite system auxiliary operation with the same receipt-first contract."""
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime


class AuxiliaryState(TypedDict, total=False):
    operation_id: str
    outcome: str


@dataclass
class AuxiliaryContext:
    execute: object
    commit: object


def build_auxiliary_graph(checkpointer=None):
    graph = StateGraph(AuxiliaryState,context_schema=AuxiliaryContext)

    def execute(state, runtime: Runtime[AuxiliaryContext]):
        runtime.context.execute(state['operation_id'])
        return {'outcome':'result_ready'}

    def commit(state, runtime: Runtime[AuxiliaryContext]):
        runtime.context.commit(state['operation_id'])
        return {'outcome':'committed'}

    graph.add_node('execute',execute)
    graph.add_node('commit',commit)
    graph.add_edge(START,'execute')
    graph.add_edge('execute','commit')
    graph.add_edge('commit',END)
    return graph.compile(checkpointer=checkpointer)
