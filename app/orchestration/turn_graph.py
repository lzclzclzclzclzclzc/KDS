from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime


class TurnState(TypedDict, total=False):
    conversation_id: str
    operation_id: str
    selected_agent_id: str
    outcome: str
    route: str
    error: str
    scores: list[dict]
    turn_result_ref: str
    vote_id: str


@dataclass
class TurnContext:
    nodes: object


def build_turn_graph(checkpointer=None):
    graph = StateGraph(TurnState, context_schema=TurnContext)

    def node(name):
        def run(state: TurnState, runtime: Runtime[TurnContext]):
            return getattr(runtime.context.nodes, name)(state)
        return run

    for name in ("load_operation", "check_control", "commit_human", "score_agents",
                 "select_speaker", "execute_turn", "validate_turn", "commit_turn",
                 "end_vote", "record_pause", "return_continue"):
        graph.add_node(name, node(name))
    graph.add_edge(START, "load_operation")
    graph.add_edge("load_operation", "check_control")
    graph.add_conditional_edges("check_control", lambda s: s["route"], {
        "human": "commit_human", "scores": "score_agents", "select": "select_speaker",
        "commit": "commit_turn", "execute": "execute_turn",
        "pause": "record_pause", "done": "return_continue",
    })
    graph.add_edge("commit_human", "return_continue")
    graph.add_edge("score_agents", "select_speaker")
    graph.add_edge("select_speaker", "execute_turn")
    graph.add_conditional_edges("execute_turn", lambda s: s["route"], {
        "valid": "validate_turn", "pause": "record_pause", "done": "return_continue",
    })
    graph.add_edge("validate_turn", "commit_turn")
    graph.add_conditional_edges("commit_turn", lambda s: s["route"], {
        "vote": "end_vote", "continue": "return_continue", "done": "return_continue",
    })
    graph.add_conditional_edges("end_vote", lambda s: s["route"], {
        "pause": "record_pause", "continue": "return_continue",
    })
    graph.add_edge("record_pause", END)
    graph.add_edge("return_continue", END)
    return graph.compile(checkpointer=checkpointer)
