"""Business steps used by the finite turn graph.

Only operation references cross graph checkpoints. Public changes are committed
through the repository; model calls never hold the conversation lock.
"""
import random
from dataclasses import asdict

from langgraph.types import Overwrite

from app.harness import HarnessTurnError
from app.orchestration.policies import policy_for
from app.orchestration.batch_graph import BatchRuntime
from app.scheduler import update_heat


class TurnNodes:
    def __init__(self, runner):
        self.runner = runner

    def operation(self, state):
        return self.runner.repository.get_operation(state["operation_id"])

    def stale(self, state):
        op = self.operation(state)
        return op is None or op["status"] == "abandoned" or self.runner._deleted

    def load_operation(self, state):
        op = self.operation(state)
        if op is None:
            raise RuntimeError("找不到讨论操作记录")
        turn = self.runner.repository.get_operation(state["operation_id"] + ":turn")
        return {"outcome": "continue", "error": "", "scores": [],
                "selected_agent_id": turn["input"]["agent"]["id"] if turn else "",
                "turn_result_ref": turn["operation_id"] if turn else "", "vote_id": ""}

    def check_control(self, state):
        r = self.runner
        if self.stale(state):
            return {"route": "done"}
        with r._lock:
            if r._interrupt_requested or r.status != "running":
                return {"route": "pause", "error": "", "outcome": "manual"}
            if r._limit_reached_nolock() or not r.agents:
                return {"route": "pause", "outcome": "limit"}
            turn = r.repository.get_operation(state["operation_id"] + ":turn")
            if turn is not None:
                # Reconstruct a missing checkpoint from its durable receipt.
                turn = r._adopt_operation(turn["operation_id"])
                if turn["status"] in {"result_ready", "committed"}:
                    return {"route": "commit"}
                return {"route": "execute"}
            if r.pending_human_message is not None:
                return {"route": "human"}
            return {"route": "scores" if policy_for(r).needs_scores(r) else "select"}

    def commit_human(self, state):
        r = self.runner
        child = state["operation_id"] + ":human"
        with r._lock:
            command = r.repository.get_pending_command(r.id)
            if command is None or r.pending_human_message is None:
                return {}
            r.repository.create_operation(child, r.id, "human", parent_operation_id=state["operation_id"],
                                          runner_epoch=r.runner_epoch, input=command["payload"])

            def apply():
                r._append_message_nolock("human", "人类", r.pending_human_message, 0)
                r.messages[-1]["operation_id"] = child
                r.turn += 1
                if r.pending_human_target is not None:
                    r._forced_next_idx = r.pending_human_target
                r.pending_human_message = r.pending_human_target = None
            r._effect(child, apply, consume_command_id=command["command_id"])
        return {}

    def score_agents(self, state):
        r = self.runner
        if self.stale(state):
            return {"scores": []}
        op = self.operation(state)
        jobs = op["input"]["score_jobs"]
        cached = {}
        for job in jobs:
            child = r.repository.get_operation(state["operation_id"] + ":score:" + job["agent_id"])
            if (child and child["status"] in {"result_ready", "committed"}
                    and child.get("result") is not None):
                cached[job["agent_id"]] = {**child["result"], "batch_id": state["operation_id"],
                                            "agent_id": job["agent_id"]}

        def call(job):
            return r._external(state["operation_id"] + ":score:" + job["agent_id"], "score", job,
                               lambda attempt: self._score(job), parent=state["operation_id"])
        batch = r.score_graph.invoke(
            {"batch_id": state["operation_id"], "jobs": jobs, "results_by_agent": Overwrite(cached)},
            r._graph_config("scores:" + state["operation_id"]),
            context=BatchRuntime(call=call, limiter=r.auxiliary_limiter), durability="sync",
        )
        ordered = batch["ordered_results"]
        errors = [x.get("error") for x in ordered if x.get("error")]
        if errors:
            raise RuntimeError(errors[0])
        for job in jobs:
            r._effect(state["operation_id"] + ":score:" + job["agent_id"], lambda: None)
        return {"scores": [{"name": j["agent_name"], "score": out["score"]}
                           for j, out in zip(jobs, ordered)]}

    def _score(self, job):
        score, usage = self.runner.llm.willingness_score(
            job["agent_name"], job["system"], job["history_text"], job["turn"])
        return {"score": score, "usage": usage}

    def select_speaker(self, state):
        r = self.runner
        if self.stale(state):
            return {"selected_agent_id": ""}
        root = self.operation(state)
        child = state["operation_id"] + ":selection"
        op = r.repository.get_operation(child)
        if op is not None and op.get("result"):
            selection = op["result"]
        else:
            with r._lock:
                raw_scores = [x["score"] for x in state.get("scores", [])] or None
                selection = asdict(policy_for(r).select(r, raw_scores, rng=random.Random(root["input"]["seed"])))
                # Persist randomness before consuming the scheduling cursor.
                r.repository.create_operation(child, r.id, "selection", parent_operation_id=state["operation_id"],
                                              runner_epoch=r.runner_epoch, result=selection, status="result_ready")
        with r._lock:
            def apply():
                r._rr_index = selection["rr_index"]
                r._forced_next_idx = selection["forced_next_idx"]
            r._effect(child, apply)
            agent = r.agents[selection["agent_idx"]]
            turn_id = state["operation_id"] + ":turn"
            system = r._build_system(agent)
            system += f"\n\n（其中 speech 字段请控制在约 {r.single_max_tokens} tokens 以内。）"
            r.repository.create_operation(turn_id, r.id, "turn", parent_operation_id=state["operation_id"],
                                          runner_epoch=r.runner_epoch, input={
                                              "agent": agent, "system": system, "history": root["input"]["history"],
                                              "message_count": root["input"]["message_count"],
                                              "whiteboard_rev": root["input"]["whiteboard_rev"],
                                              "scores": state.get("scores") or None,
                                          })
        return {"selected_agent_id": agent["id"], "turn_result_ref": turn_id}

    def execute_turn(self, state):
        r = self.runner
        if self.stale(state):
            return {"route": "done"}
        turn_id = state["turn_result_ref"]
        op = r.repository.get_operation(turn_id)
        if op["status"] == "committed":
            return {"route": "valid"}
        with r._lock:
            if r.harness is not None and r._harness_stop_reason():
                return {"route": "pause", "outcome": r._harness_stop_reason()}
        data = op["input"]

        def call(attempt):
            epoch = r.runner_epoch
            turn_out, usage, harness_state = r.executor.execute(
                r, data["agent"], data["system"], data["history"],
                on_usage=lambda delta: r._usage(turn_id, attempt, "dsh", delta),
                on_progress=lambda progress: r._progress(progress, epoch, turn_id),
            )
            return {"turn_out": turn_out, "usage": usage, "harness_state": harness_state,
                    "usage_reported": r.harness is not None}
        try:
            r._external(turn_id, "turn", data, call, parent=state["operation_id"])
            return {"route": "valid"}
        except HarnessTurnError as exc:
            with r._lock:
                idx = r._resolve_agent_idx(state["selected_agent_id"])
                r._forced_next_idx = idx
            return {"route": "pause", "outcome": exc.reason, "error": str(exc)}

    def validate_turn(self, state):
        # Backends preserve direct's parser and Harness's strict validation.
        # Permission checks belong to the atomic public commit below.
        return {}

    def commit_turn(self, state):
        r = self.runner
        if self.stale(state):
            return {"route": "done"}
        turn_id = state["turn_result_ref"]
        op = r.repository.get_operation(turn_id)
        result = op["result"]
        agent = op["input"]["agent"]
        with r._lock:
            if op["status"] != "committed":
                if (len(r.messages) != op["input"]["message_count"]
                        or r.whiteboard_rev != op["input"]["whiteboard_rev"]):
                    r._abandon_operation_tree(state["operation_id"])
                    return {"route": "done"}
                out = result["turn_out"]
                proposed = bool(out.get("propose_end")) and r._is_proposer(agent)
                should_vote = proposed and r.turn + 1 > r._end_vote_block_until_turn
                vote_id = "v" + str(len(r.votes) + 1) if should_vote else ""
                r.repository.update_operation(turn_id, output={"vote_id": vote_id})

                def apply():
                    edited = False
                    if r._can_edit_whiteboard(agent):
                        content = r._apply_whiteboard_ops(r.whiteboard_content, out.get("whiteboard_ops") or [])
                        if content != r.whiteboard_content:
                            r.whiteboard_content = content
                            r.whiteboard_rev += 1
                            r.whiteboard_last_editor = agent["name"]
                            edited = True
                    r._append_message_nolock("agent", agent["name"], out.get("speech") or "",
                                             result["usage"]["completion_tokens"],
                                             scores=op["input"].get("scores"), proposed_end=proposed, wb_edited=edited)
                    r.messages[-1].update(agent_id=agent["id"], operation_id=turn_id)
                    if result.get("harness_state") is not None:
                        r.harness_state[agent["id"]] = result["harness_state"]
                    idx = r._resolve_agent_idx(agent["id"])
                    r.heat = update_heat(r.heat, idx, r.gamma)
                    r.last_agent_idx = idx
                    r.turn += 1
                    r.harness_activity = None
                    r._finish_tool_logs_nolock()
                    if vote_id:
                        r.votes.append(r._new_end_vote(vote_id))
                r._effect(turn_id, apply)
            vote_id = (r.repository.get_operation(turn_id).get("output") or {}).get("vote_id", "")
        return {"route": "vote" if vote_id else "continue", "vote_id": vote_id}

    def end_vote(self, state):
        r = self.runner
        if self.stale(state):
            return {"route": "continue"}
        agreed = r._execute_vote(state["vote_id"], parent=state["operation_id"])
        if agreed:
            return {"route": "pause", "outcome": "vote_end"}
        cooldown = state["operation_id"] + ":cooldown"
        turn = r.repository.get_operation(state["turn_result_ref"])["input"]["message_count"] + 1
        r.repository.create_operation(cooldown, r.id, "cooldown", parent_operation_id=state["operation_id"],
                                      runner_epoch=r.runner_epoch)
        r._effect(cooldown, lambda: setattr(r, "_end_vote_block_until_turn", turn + r.end_vote_cooldown_turns))
        return {"route": "continue"}

    def record_pause(self, state):
        r = self.runner
        reason = state.get("outcome") or "error"
        if reason not in {"manual", "limit", "vote_end"}:
            reason = "error"
        with r._lock:
            if r._interrupt_requested:
                reason = "manual"
            r._pause_segment()
            r.status = "paused"
            r.paused_reason = reason
            r.error = state.get("error") if reason == "error" else None
            r._interrupt_requested = False
            if not r._deleted:
                r._effect(state["operation_id"], lambda: None)
        return {"outcome": "paused"}

    def return_continue(self, state):
        r = self.runner
        if not self.stale(state):
            r._effect(state["operation_id"], lambda: None)
        return {"outcome": "continue"}
