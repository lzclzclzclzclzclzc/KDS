"""The same frozen participant batch for ordinary and unanimous end voting."""

from typing import TypedDict

from .batch_graph import BatchRuntime, BatchState, _build_graph, collect_batch


class VoteState(BatchState, total=False):
    kind: str
    options: list[str]
    ballots: list[dict]
    results: dict[str, int]
    agreed: bool


VoteRuntime = BatchRuntime


def collect_vote(state: VoteState) -> dict:
    batch = collect_batch(state)
    counts = {str(index + 1): 0 for index in range(len(state.get("options") or []))}
    ballots = []
    jobs = state.get("jobs") or []
    for job, record in zip(jobs, batch["ordered_results"]):
        if record.get("status") == "error" or record.get("error"):
            continue
        # Accept flattened auxiliary results and the explicit result envelope.
        value = record.get("result", record)
        choices = [str(choice) for choice in (value.get("choices") or [])]
        for choice in choices:
            if choice in counts:
                counts[choice] += 1
        ballots.append({
            "agent_id": job["agent_id"],
            "agent_name": job.get("agent_name", job.get("name", job["agent_id"])),
            "choices": choices, "reason": value.get("reason") or "",
        })
    # Abstention or a failed/missing ballot cannot become unanimous agreement.
    agreed = (
        state.get("kind") == "end" and batch["status"] == "completed"
        and bool(jobs) and len(ballots) == len(jobs)
        and all(ballot["choices"] == ["1"] for ballot in ballots)
    )
    return {**batch, "ballots": ballots, "results": counts, "agreed": agreed}


def build_vote_graph(*, checkpointer=None):
    return _build_graph(VoteState, collect_vote, checkpointer=checkpointer)
