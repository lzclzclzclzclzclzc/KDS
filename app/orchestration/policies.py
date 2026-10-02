"""Scheduling decisions using the original KDS algorithms and priorities."""

from dataclasses import dataclass
import random
from typing import Protocol

from app.scheduler import willingness_select


@dataclass(frozen=True)
class Selection:
    """A decision to persist before applying the scheduler's new cursor."""

    agent_idx: int
    rr_index: int
    forced_next_idx: int | None = None


class SchedulingPolicy(Protocol):
    def needs_scores(self, runner) -> bool: ...

    def select(self, runner, scores: list[float] | None = None,
               rng: random.Random | None = None) -> Selection: ...


def _forced(runner) -> int | None:
    index = runner._forced_next_idx
    if index is not None and 0 <= index < len(runner.agents):
        return index
    return None


class RoundRobinPolicy:
    def needs_scores(self, runner) -> bool:
        return False

    def select(self, runner, scores=None, rng=None) -> Selection:
        forced = _forced(runner)
        if forced is not None:
            return Selection(forced, runner._rr_index)
        if not runner.order:
            raise ValueError("没有可发言的角色")
        index = runner.order[runner._rr_index % len(runner.order)]
        return Selection(index, runner._rr_index + 1)


class WillingnessPolicy:
    def needs_scores(self, runner) -> bool:
        return bool(runner.agents) and _forced(runner) is None and runner.turn != 0

    def select(self, runner, scores=None, rng=None) -> Selection:
        forced = _forced(runner)
        if forced is not None:
            return Selection(forced, runner._rr_index)
        if not runner.agents:
            raise ValueError("没有可发言的角色")
        if runner.turn == 0:
            return Selection(runner.first_idx, runner._rr_index)
        if scores is None or len(scores) != len(runner.agents):
            raise ValueError("意愿评分必须包含当前批次的全部角色")
        index = willingness_select(
            scores, runner.heat, lam=runner.lam, tau=runner.tau,
            forbid_consecutive=runner.forbid_consecutive,
            last_speaker=runner.last_agent_idx, rng=rng,
        )
        return Selection(index, runner._rr_index)


def policy_for(runner) -> SchedulingPolicy:
    # Two participants always alternate, irrespective of configured mode.
    if len(runner.agents) == 2 or runner.scheduling_mode == "round_robin":
        return RoundRobinPolicy()
    return WillingnessPolicy()
