"""Thin adaptation of the existing direct and DSH role-turn clients."""

from contextlib import nullcontext

from app.harness import HarnessTurnError


class AgentTurnExecutor:
    def __init__(self, limiter=None):
        self.limiter = limiter

    def execute(self, runner, agent: dict, system: str, history: list[dict], *,
                on_progress=None, on_usage=None, should_stop=None):
        """Return (turn, usage, cursor); DSH reports usage during its attempt.

        Direct calls retain their original non-cancellable completion behavior.
        The caller commits direct usage alongside the public result, and must not
        count DSH's aggregate return a second time after its usage callbacks.
        """
        with self.limiter if self.limiter is not None else nullcontext():
            if runner.harness is None:
                turn, usage = runner.llm.agent_turn(
                    agent["name"], system, history, runner.single_max_tokens,
                )
                return turn, usage, None

            with runner._lock:
                remaining_output = (
                    None if runner.total_max_tokens is None else
                    runner.total_max_tokens - runner.total_output_tokens
                )
                state = dict(runner.harness_state.get(agent["id"], {}))
            stop = should_stop or runner._harness_stop_reason
            turn, usage, next_state = runner.harness.run_turn(
                agent=agent, system=system, history=history, state=state,
                remaining_output=remaining_output, should_stop=stop,
                on_progress=on_progress or runner._harness_progress,
                on_usage=on_usage or runner._harness_usage,
            )
            if reason := stop():
                raise HarnessTurnError("本轮工具执行已停止。", reason)
            with runner._lock:
                runner._finish_tool_logs_nolock()
            return turn, usage, next_state
