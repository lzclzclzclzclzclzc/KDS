"""Team business transactions against real temporary SQLite databases."""
from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db
from app.domain.teams import normalize_limits, validate_role, validate_team_definition
from app.repositories.teams import TeamConflict, TeamRepository


@pytest.fixture
def repo(tmp_path):
    return TeamRepository(tmp_path / "team.db")


def make_run(repo, *, entries=("p",), concurrency=1, tokens=5000, single=100,
             max_activations=12, board=False, role_budget=None):
    role = repo.save_definition("role", validate_role({"name": "Verifier", "system_prompt": "VERIFY",
                                                      "tools": ["read"], "model_config_id": "default",
                                                      "default_budget": role_budget or {}}))
    payload = {"name": "Tree", "shared_background": "SHARED", "nodes": [
        {"id": node_id, "name": node_id, "role_id": role["id"], "role_version": 1,
         "position": {"x": i * 10, "y": 15}, "prompt_supplement": f"NODE-{node_id}"}
        for i, node_id in enumerate(("p", "c", "g", "b", "z"))],
        "edges": [
            {"id": "pc", "type": "task", "source": "p", "target": "c"},
            {"id": "cg", "type": "task", "source": "c", "target": "g"},
            {"id": "pb", "type": "task", "source": "p", "target": "b"},
            {"id": "rcb", "type": "room", "source": "c", "target": "b"},
            {"id": "rbg", "type": "room", "source": "b", "target": "g"}],
        "whiteboard_enabled": board, "whiteboard_editors": ["p", "z"]}
    definition = repo.save_definition("team", validate_team_definition(payload))
    limits = normalize_limits({"single_max_tokens": single, "total_max_tokens": tokens,
                               "summary_max_tokens": 20, "max_concurrency": concurrency,
                               "max_processes": concurrency, "max_activations_per_task": max_activations})
    request = {"request_id": "run-request", "goal": "Deliver", "entry_node_ids": list(entries), "limits": limits}
    run_id = repo.create_run(request, definition, {(role["id"], 1): role})
    snapshot = repo.snapshot(run_id)
    instances = {i["node_id"]: i["id"] for i in snapshot["agents"]}
    return run_id, instances, definition, request, role


def claim(repo, run_id):
    ids = repo.dispatch(run_id, slots=10)
    assert len(ids) == 1, repo.snapshot(run_id)
    return ids[0]


def deliver(repo, activation_id, **payload):
    attempt = repo.start_attempt(activation_id)
    assert attempt["result"] is None
    payload.setdefault("speech", "STEP")
    payload.setdefault("result", "RESULT")
    repo.save_result(activation_id, attempt["attempt_id"], payload)
    clean = repo.validate_result(activation_id, payload)
    repo.commit(activation_id, clean)
    return attempt["attempt_id"]


def delegate(repo, activation_id, instance_id, request_id="delegate"):
    return repo.command(activation_id, "kds_delegate_task", {
        "request_id": request_id, "child_instance_id": instance_id, "goal": "Child work"})


def task(repo, run_id, task_id):
    return next(t for t in repo.snapshot(run_id)["tasks"] if t["id"] == task_id)


def test_schema_bootstrap_and_versions_keep_running_definition_frozen(repo):
    run_id, instances, definition, request, role = make_run(repo)
    db.init_db(repo.db_path)
    assert repo.create_run(request, definition, {(role["id"], 1): role}) == run_id
    assert len(repo.list_runs()) == 1
    assert len(repo.snapshot(run_id)["rooms"]) == 1
    updated = deepcopy(definition)
    updated["nodes"][0]["name"] = "Edited"
    repo.save_definition("team", updated, definition["id"], base_version=1)
    assert repo.snapshot(run_id)["definition"]["nodes"][0]["name"] == "p"
    assert len(repo.list_versions("team", definition["id"])) == 2
    with pytest.raises(TeamConflict):
        repo.save_definition("team", updated, definition["id"], base_version=1)
    activation = claim(repo, run_id)
    context = repo.context(activation)
    assert "NODE-p" in context["system"]
    assert json.loads(context["history"])["shared_background"] == "SHARED"
    assert "harness_state" not in json.loads(context["history"])["children"][0]


def test_delegate_ack_loss_is_idempotent_and_different_arguments_conflict(repo):
    run_id, nodes, *_ = make_run(repo)
    activation = claim(repo, run_id)
    first = delegate(repo, activation, nodes["c"])
    assert delegate(repo, activation, nodes["c"]) == first
    assert len(repo.snapshot(run_id)["tasks"]) == 2
    with pytest.raises(TeamConflict):
        delegate(repo, activation, nodes["b"])
    with pytest.raises(ValueError):
        delegate(repo, activation, nodes["g"], "cross-branch")
    with pytest.raises(ValueError):
        repo.command(activation, "kds_get_task_results", {"request_id": "read-wrong", "task_ids": ["other"]})


def test_tool_process_ignores_phase_snapshots_and_preserves_real_repeated_calls(repo):
    from app.tool_logs import tool_log_update
    run_id, _, *_ = make_run(repo)
    activation = claim(repo, run_id)
    for phase in ("准备 DSH", "执行中", "使用工具"):
        repo.record_activity(activation, {"stage": phase, "steps": 1, "tool_calls": 0})
    assert not repo.snapshot(run_id)["tool_logs"]
    assert repo.snapshot(run_id)["agents"][0]["dsh_phase"] == "使用工具"
    def call_event(call_id, session):
        event = {"type": "tool/call", "seq": 16, "time": 1791040250770,
                 "data": {"step": 1, "callId": call_id, "name": "read", "arguments": {"file_path": "same.txt"}}}
        return {"stage": "使用工具", "tool_log": tool_log_update(event, activation, session_id=session)}
    first = call_event("first", "session-a")
    repo.record_activity(activation, first)
    repo.record_activity(activation, first)
    end = {"type": "tool/result", "seq": 17, "sourceEventSeqs": [16], "time": 1791040250888,
           "data": {"step": 1, "message": {"toolCallId": "first", "source": {"kind": "tool", "callId": "first"},
                                            "content": [{"type": "text", "text": "42"}]}}}
    repo.record_activity(activation, {"stage": "整理工具回执", "tool_log": tool_log_update(end, activation, session_id="session-a")})
    repo.record_activity(activation, first)  # Late duplicate must preserve completion.
    repo.record_activity(activation, call_event("second", "session-a"))
    repo.record_activity(activation, call_event("first", "session-b"))
    logs = repo.list_entities(run_id, "team_tool_logs")
    assert len(logs) == 3 and logs[0]["status"] == "completed" and logs[0]["result"] == "42"
    assert len({log["arguments"] for log in logs}) == 1
    assert len({log["id"] for log in logs}) == 3
    with repo.transaction() as conn:
        owner = conn.execute('SELECT * FROM team_activations WHERE id=?', (activation,)).fetchone()
        conn.execute('INSERT INTO team_tool_logs VALUES (?,?,?,?,?,?)',
                     ('legacy-phase', run_id, owner['instance_id'], owner['task_id'], activation,
                      json.dumps({'stage': '执行中', 'status': None, 'call_id': None, 'detail': 'phase snapshot'})))
    assert len(repo.snapshot(run_id)["tool_logs"]) == len(repo.list_entities(run_id, "team_tool_logs")) == 3
    with repo.reading() as conn:
        assert conn.execute('SELECT count(*) FROM team_tool_logs WHERE run_id=?', (run_id,)).fetchone()[0] == 4


def test_three_level_tree_finishes_with_one_execution_and_process_slot(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=1)
    parent = claim(repo, run_id)
    child = delegate(repo, parent, nodes["c"])
    deliver(repo, parent, action="wait_children", wait={"task_ids": [child["task_id"]], "mode": "all"})
    assert repo.snapshot(run_id)["usage"]["reserved_tokens"] == 0
    child_activation = claim(repo, run_id)
    grandchild = delegate(repo, child_activation, nodes["g"])
    deliver(repo, child_activation, action="wait_children", wait={"task_ids": [grandchild["task_id"]]})
    deliver(repo, claim(repo, run_id), action="complete_task", result="grandchild-result")
    next_child = claim(repo, run_id)
    assert repo.context(next_child)["instance_id"] == nodes["c"]
    assert json.loads(repo.context(next_child)["history"])["child_results"][0]["result"] == "grandchild-result"
    deliver(repo, next_child, action="complete_task", result="child-result")
    next_parent = claim(repo, run_id)
    assert repo.context(next_parent)["instance_id"] == nodes["p"]
    attempt_id = deliver(repo, next_parent, action="complete_task", result="root-result")
    messages_before = len(repo.snapshot(run_id)["messages"])
    assert repo.commit(next_parent, {"action": "complete_task", "speech": "duplicate"}) == "committed"
    assert len(repo.snapshot(run_id)["messages"]) == messages_before
    assert repo.dispatch(run_id) == []
    snapshot = repo.snapshot(run_id)
    assert snapshot["status"] == "paused" and snapshot["paused_reason"] == "tasks_finished"
    assert all(t["status"] == "succeeded" for t in snapshot["tasks"])


def test_human_messages_neither_wake_waiting_task_nor_create_idle_task(repo):
    run_id, nodes, *_ = make_run(repo)
    parent = claim(repo, run_id)
    child = delegate(repo, parent, nodes["c"])
    parent_task = repo.context(parent)["task_id"]
    deliver(repo, parent, action="wait_children", wait={"task_ids": [child["task_id"]]})
    for key, target in (("one", nodes["p"]), ("two", nodes["p"]), ("idle", nodes["z"])):
        repo.human_message(run_id, {"request_id": key, "target_type": "agent", "target_id": target, "content": key})
    assert task(repo, run_id, parent_task)["status"] == "waiting_children"
    assert len(repo.snapshot(run_id)["tasks"]) == 2
    assert len(repo.messages(run_id, instance_id=nodes["p"])) == 3  # stage speech + two supplements
    deliver(repo, claim(repo, run_id), action="complete_task")
    resumed_parent = claim(repo, run_id)
    history = json.loads(repo.context(resumed_parent)["history"])
    assert [m["content"] for m in history["messages"] if m["kind"] == "human"] == ["one", "two"]


def test_any_success_wakes_when_all_children_failed_or_cancelled(repo):
    run_id, nodes, *_ = make_run(repo)
    parent = claim(repo, run_id)
    one = delegate(repo, parent, nodes["c"], "one")
    two = delegate(repo, parent, nodes["b"], "two")
    # Cancel only this branch; sibling work remains eligible.
    repo.command(parent, "kds_cancel_task", {"request_id": "cancel", "task_id": two["task_id"]})
    assert task(repo, run_id, one["task_id"])["status"] == "queued"
    deliver(repo, parent, action="wait_children", wait={"task_ids": [one["task_id"], two["task_id"]], "mode": "any_success"})
    failed = claim(repo, run_id)
    repo.fail_activation(failed, ValueError("child failed"))
    resumed = claim(repo, run_id)
    assert repo.context(resumed)["instance_id"] == nodes["p"]
    assert {t["status"] for t in json.loads(repo.context(resumed)["history"])["child_results"]} == {"failed", "cancelled"}


def test_early_child_result_is_seen_when_parent_later_registers_wait(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    parent = claim(repo, run_id)
    child = delegate(repo, parent, nodes["c"])
    child_activation = claim(repo, run_id)
    deliver(repo, child_activation, action="complete_task")
    parent_task = repo.context(parent)["task_id"]
    deliver(repo, parent, action="wait_children", wait={"task_ids": [child["task_id"]]})
    assert task(repo, run_id, parent_task)["status"] == "queued"
    assert repo.context(claim(repo, run_id))["instance_id"] == nodes["p"]


def test_restart_reuses_definite_receipt_without_new_attempt_at_concurrency_one(repo):
    run_id, *_ = make_run(repo, max_activations=1)
    activation = claim(repo, run_id)
    attempt = repo.start_attempt(activation)["attempt_id"]
    result = {"action": "complete_task", "speech": "saved", "result": "definite"}
    repo.save_result(activation, attempt, result)
    restored = TeamRepository(repo.db_path)
    restored.recover()
    assert restored.snapshot(run_id)["status"] == "paused"
    assert restored.snapshot(run_id)["tasks"][0]["uncertain"] is False
    restored.resume(run_id)
    assert claim(restored, run_id) == activation
    assert restored.operation(activation)["result"] == result
    assert restored.start_attempt(activation)["result"] == result
    assert restored.snapshot(run_id)["usage"]["reserved_tokens"] == 0
    restored.commit(activation, restored.validate_result(activation, result))
    with restored.reading() as conn:
        assert conn.execute("SELECT count(*) FROM orchestration_attempts WHERE operation_id=?", (activation,)).fetchone()[0] == 1


def test_unknown_external_call_is_paused_and_requires_explicit_retry(repo):
    run_id, *_ = make_run(repo)
    activation = claim(repo, run_id)
    repo.start_attempt(activation)
    repo.recover()
    assert repo.snapshot(run_id)["tasks"][0]["uncertain"] is True
    with pytest.raises(TeamConflict):
        repo.resume(run_id)
    repo.resume(run_id, retry_uncertain=True)
    assert claim(repo, run_id) != activation


def test_old_epoch_cannot_publish_but_known_usage_still_counts_once(repo):
    run_id, *_ = make_run(repo)
    activation = claim(repo, run_id)
    attempt = repo.start_attempt(activation)["attempt_id"]
    result = {"action": "complete_task", "speech": "late", "result": "late"}
    repo.pause(run_id)
    assert repo.save_result(activation, attempt, result) is False
    with pytest.raises(TeamConflict):
        repo.commit(activation, result)
    usage = {"event_id": "usage", "prompt_tokens": 4, "completion_tokens": 7}
    assert repo.record_usage(activation, attempt, usage) is True
    assert repo.record_usage(activation, attempt, usage) is False
    assert repo.snapshot(run_id)["usage"]["completion_tokens"] == 7
    assert not any(m["content"] == "late" for m in repo.snapshot(run_id)["messages"])


def test_concurrent_budget_reservations_share_single_session_balance(repo):
    run_id, *_ = make_run(repo, entries=("p", "z"), concurrency=2, tokens=170, single=100)
    with ThreadPoolExecutor(max_workers=2) as workers:
        requests = list(workers.map(lambda _: repo.dispatch(run_id, slots=2), range(2)))
    activations = [a for group in requests for a in group]
    assert len(activations) == len(set(activations)) == 2
    snapshot = repo.snapshot(run_id)
    assert snapshot["usage"]["reserved_tokens"] == 150  # 20 kept for human finalization
    assert sorted(repo.context(a)["output_budget"] for a in activations) == [50, 100]


def test_dynamic_temporary_role_creation_is_atomic_scoped_and_idempotent(repo):
    run_id, nodes, *_ = make_run(repo)
    activation = claim(repo, run_id)
    room_id = repo.snapshot(run_id)["rooms"][0]["id"]
    args = {"request_id": "spawn", "role": {"name": "Boundary", "system_prompt": "CHECK", "tools": []},
            "goal": "Check boundaries", "reason": "Need extra checking"}
    result = repo.command(activation, "kds_spawn_subagent", args)
    assert repo.command(activation, "kds_spawn_subagent", args) == result
    agent = next(a for a in repo.snapshot(run_id)["agents"] if a["id"] == result["instance_id"])
    assert agent["parent_id"] == nodes["p"] and agent["role"]["tools"] == []
    assert agent["role"]["temporary"] is True
    assert len(repo.list_definitions("role")) == 1  # runtime role did not enter global library
    before = len(repo.snapshot(run_id)["agents"])
    with pytest.raises(ValueError):
        repo.command(activation, "kds_spawn_subagent", {**args, "request_id": "illegal-room", "join_room": room_id})
    assert len(repo.snapshot(run_id)["agents"]) == before
    with pytest.raises(ValueError):
        repo.command(activation, "kds_spawn_subagent", {**args, "request_id": "escalate", "role": {**args["role"], "tools": ["bash"]}})
    assert len(repo.snapshot(run_id)["agents"]) == before


def test_concurrent_whiteboard_writes_conflict_without_partial_speech(repo):
    run_id, *_ = make_run(repo, entries=("p", "z"), concurrency=2, board=True)
    activations = repo.dispatch(run_id, slots=2)
    for activation, content in zip(activations, ("first", "second")):
        attempt = repo.start_attempt(activation)["attempt_id"]
        payload = {"action": "complete_task", "speech": content, "result": content,
                   "whiteboard": {"base_rev": 0, "ops": [{"op": "append", "content": content}]}}
        repo.save_result(activation, attempt, payload)
        repo.commit(activation, repo.validate_result(activation, payload))
    snapshot = repo.snapshot(run_id)
    assert snapshot["whiteboard"]["rev"] == 1
    assert snapshot["whiteboard"]["content"] == "first"
    assert not any(m["content"] == "second" for m in snapshot["messages"])
    assert sorted(t["status"] for t in snapshot["tasks"]) == ["queued", "succeeded"]


def test_delete_rejects_old_callbacks_and_does_not_recreate_data(repo):
    run_id, *_ = make_run(repo)
    activation = claim(repo, run_id)
    attempt = repo.start_attempt(activation)["attempt_id"]
    assert repo.delete(run_id) is True
    assert repo.snapshot(run_id) is None
    assert repo.save_result(activation, attempt, {"action": "complete_task"}) is False
    assert repo.record_usage(activation, attempt, {"event_id": "late", "completion_tokens": 10}) is False
    assert repo.list_runs() == []


def test_recovering_paid_receipt_does_not_require_another_output_budget(repo):
    run_id, *_ = make_run(repo, tokens=120, single=100)
    activation = claim(repo, run_id)
    attempt = repo.start_attempt(activation)["attempt_id"]
    result = {"action": "complete_task", "speech": "paid", "result": "paid"}
    repo.record_usage(activation, attempt, {"event_id": "paid", "completion_tokens": 100})
    repo.save_result(activation, attempt, result)
    repo.pause(run_id)
    repo.resume(run_id)
    assert claim(repo, run_id) == activation
    assert repo.snapshot(run_id)["usage"]["reserved_tokens"] == 0
    repo.commit(activation, repo.validate_result(activation, result))
    assert repo.snapshot(run_id)["tasks"][0]["status"] == "succeeded"


def test_role_default_budget_is_tightened_by_parent_grants(repo):
    run_id, nodes, *_ = make_run(repo, single=100, role_budget={"single_max_tokens": 12, "total_max_tokens": 40})
    parent = claim(repo, run_id)
    assert repo.context(parent)["output_budget"] == 12
    child = delegate(repo, parent, nodes["c"])
    assert task(repo, run_id, child["task_id"])["budget"] == {"single_max_tokens": 12, "total_max_tokens": 40}
    with pytest.raises(ValueError):
        repo.command(parent, "kds_delegate_task", {"request_id": "increase", "child_instance_id": nodes["b"],
                                                  "goal": "Child", "budget": {"total_max_tokens": 41}})


def test_room_discussion_has_finite_trigger_and_dynamic_member_no_old_history(repo):
    run_id, nodes, *_ = make_run(repo)
    room_id = repo.snapshot(run_id)["rooms"][0]["id"]
    old_message = repo.human_message(run_id, {"request_id": "room-human", "target_type": "room", "target_id": room_id, "content": "OLD-ROOM"})
    parent = claim(repo, run_id)
    child = delegate(repo, parent, nodes["c"])
    deliver(repo, parent, action="wait_children", wait={"task_ids": [child["task_id"]]})
    # The pending room discussion observes busy c and may first activate b.
    for _ in range(3):
        child_activation = claim(repo,run_id)
        if repo.context(child_activation)["instance_id"] == nodes["c"]:
            break
        deliver(repo,child_activation,action="complete_task")
    assert repo.context(child_activation)["instance_id"] == nodes["c"]
    spawned = repo.command(child_activation, "kds_spawn_subagent", {"request_id": "new-member", "goal": "New work",
                          "role": {"name": "New", "system_prompt": "CHECK", "tools": []}, "join_room": room_id})
    second = repo.command(child_activation, "kds_send_group_message", {"request_id": "new-room-message", "room_id": room_id, "content": "NEW-ROOM"})
    history = repo.messages(run_id, instance_id=spawned["instance_id"])
    assert [m["content"] for m in history] == ["NEW-ROOM"]
    assert old_message["id"] not in {m["id"] for m in history}
    one = repo.command(child_activation, "kds_request_discussion", {"request_id": "discussion-one", "room_id": room_id, "goal": "Finite"})
    two = repo.command(child_activation, "kds_request_discussion", {"request_id": "discussion-two", "room_id": room_id, "goal": "Finite"})
    assert one["discussion_id"] == two["discussion_id"]


def test_retry_creates_new_task_and_keeps_old_result_immutable(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    parent = claim(repo, run_id)
    first = delegate(repo, parent, nodes["c"])
    child = claim(repo, run_id)
    repo.fail_activation(child, ValueError("original failure"))
    original = task(repo, run_id, first["task_id"])
    retry = repo.command(parent, "kds_retry_task", {"request_id": "retry", "task_id": first["task_id"]})
    assert retry["task_id"] != first["task_id"]
    assert task(repo, run_id, retry["task_id"])["retry_of_task_id"] == first["task_id"]
    assert task(repo, run_id, first["task_id"]) == original
    assert repo.command(parent, "kds_retry_task", {"request_id": "retry", "task_id": first["task_id"]}) == retry


def test_auxiliary_reservation_occupies_same_dispatch_capacity(repo):
    run_id, *_ = make_run(repo, concurrency=1)
    auxiliary=repo.prepare_auxiliary(run_id,"assist",{},"assist")
    assert repo.dispatch(run_id,slots=5)==[]
    assert repo.snapshot(run_id)["usage"]["reserved_tokens"]==100
    repo.commit_auxiliary(auxiliary,error="cancelled test auxiliary")
    assert claim(repo,run_id)


def test_task_duration_uses_active_wall_time_and_excludes_pauses(repo,monkeypatch):
    import app.repositories.teams as teams
    now=[1000.0]
    monkeypatch.setattr(teams.time,"time",lambda:now[0])
    run_id,*_=make_run(repo,role_budget={"total_duration_seconds":5})
    activation=claim(repo,run_id)
    now[0]+=3
    assert repo.dispatch(run_id)==[]
    repo.pause(run_id)
    now[0]+=1000
    repo.resume(run_id)
    assert repo.dispatch(run_id)  # the prepared activation can be claimed again
    now[0]+=2
    assert repo.dispatch(run_id)==[]
    snapshot=repo.snapshot(run_id)
    assert snapshot["tasks"][0]["status"]=="failed"
    assert snapshot["tasks"][0]["error"]["type"]=="duration_limit"
    assert snapshot["elapsed_seconds"]==5


def test_heartbeat_progress_survives_restart_without_charging_downtime(repo,monkeypatch):
    import app.repositories.teams as teams
    now=[1000.0]
    monkeypatch.setattr(teams.time,"time",lambda:now[0])
    run_id,*_=make_run(repo)
    claim(repo,run_id)
    now[0]+=4
    repo.dispatch(run_id)
    now[0]+=1000
    repo.recover()
    assert repo.snapshot(run_id)["active_seconds"]==4
    assert repo.snapshot(run_id)["elapsed_seconds"]==4


@pytest.mark.parametrize("budget,target", [({"max_depth":1},"g"),({"max_tasks":2},"g"),({"max_instances":2},"g")])
def test_parent_scope_counts_and_depth_bound_descendant_work(repo,budget,target):
    run_id,nodes,*_=make_run(repo,role_budget=budget)
    parent=claim(repo,run_id)
    child=delegate(repo,parent,nodes["c"])
    deliver(repo,parent,action="wait_children",wait={"task_ids":[child["task_id"]]})
    child_activation=claim(repo,run_id)
    with pytest.raises(TeamConflict):
        delegate(repo,child_activation,nodes[target],"too-deep-or-many")


def test_parent_concurrency_budget_covers_all_siblings(repo):
    run_id,nodes,*_=make_run(repo,concurrency=2,role_budget={"max_concurrency":1})
    parent=claim(repo,run_id)
    one=delegate(repo,parent,nodes["c"],"one")
    two=delegate(repo,parent,nodes["b"],"two")
    deliver(repo,parent,action="wait_children",wait={"task_ids":[one["task_id"],two["task_id"]]})
    assert len(repo.dispatch(run_id,slots=2))==1


def test_role_child_count_cap_is_tighter_than_session_cap(repo):
    run_id,nodes,*_=make_run(repo,role_budget={"max_children":1})
    parent=claim(repo,run_id)
    delegate(repo,parent,nodes["c"],"one")
    with pytest.raises(TeamConflict):
        delegate(repo,parent,nodes["b"],"two")


def test_busy_task_deadline_heartbeat_cancels_children_without_resurrecting_stale_rows(repo,monkeypatch):
    import app.repositories.teams as teams
    now=[1000.0]
    monkeypatch.setattr(teams.time,"time",lambda:now[0])
    run_id,nodes,*_=make_run(repo,role_budget={"total_duration_seconds":5})
    parent=claim(repo,run_id)
    child=delegate(repo,parent,nodes["c"])
    deliver(repo,parent,action="wait_children",wait={"task_ids":[child["task_id"]]})
    child_activation=claim(repo,run_id)
    attempt=repo.start_attempt(child_activation)["attempt_id"]
    now[0]+=5
    snapshot=repo.heartbeat(run_id)
    assert {t["status"] for t in snapshot["tasks"]}=={"failed","cancelled"}
    assert snapshot["usage"]["reserved_tokens"]==0
    assert repo.save_result(child_activation,attempt,{"action":"complete_task","speech":"late"}) is False
    assert repo.operation(child_activation)["status"]=="cancelled"


def test_result_and_usage_receipts_cannot_cross_activation_identity(repo):
    run_id,*_=make_run(repo,entries=("p","z"),concurrency=2)
    first,second=repo.dispatch(run_id,slots=2)
    one=repo.start_attempt(first)["attempt_id"]
    two=repo.start_attempt(second)["attempt_id"]
    with pytest.raises(TeamConflict):
        repo.save_result(first,two,{"action":"complete_task","speech":"wrong"})
    with pytest.raises(TeamConflict):
        repo.record_usage(first,two,{"event_id":"wrong","completion_tokens":10})
    assert repo.snapshot(run_id)["usage"]["completion_tokens"]==0


def test_role_prompt_visibility_is_saved_without_name_permissions(repo):
    role=repo.save_definition("role",validate_role({"name":"公开","system_prompt":"PUBLIC","visibility":["node-a"]}))
    assert role["visibility"]==["node-a"]


def test_summary_attempt_receipt_survives_restart_before_operation_promotion(repo):
    from app.repositories.teams import encode
    run_id,*_=make_run(repo)
    operation=repo.prepare_finalize(run_id,summarize=True)
    attempt=repo.start_auxiliary(operation)["attempt_id"]
    repo.record_usage(operation,attempt,{"event_id":"summary","completion_tokens":10})
    with repo.transaction() as conn:
        conn.execute("UPDATE orchestration_attempts SET status='result_ready',result=? WHERE attempt_id=?",
                     (encode({"summary":"DEFINITE SUMMARY"}),attempt))
    restored=TeamRepository(repo.db_path)
    restored.recover()
    restored.recover_auxiliary()
    snapshot=restored.snapshot(run_id)
    assert snapshot["status"]=="completed"
    assert snapshot["summary"]=="DEFINITE SUMMARY"
    assert snapshot["summary_error"] is None
    assert snapshot["usage"]["completion_tokens"]==10
    assert snapshot["usage"]["reserved_tokens"]==0
    with restored.reading() as conn:
        assert conn.execute("SELECT count(*) FROM orchestration_attempts WHERE operation_id=?",(operation,)).fetchone()[0]==1


def test_session_extension_follows_implicit_budgets_through_existing_descendants(repo):
    run_id,nodes,*_=make_run(repo,tokens=120,single=100)
    parent=claim(repo,run_id)
    child=delegate(repo,parent,nodes['c'])
    deliver(repo,parent,action='wait_children',wait={'task_ids':[child['task_id']]})
    child_activation=claim(repo,run_id)
    grandchild=delegate(repo,child_activation,nodes['g'])
    attempt=repo.start_attempt(child_activation)['attempt_id']
    repo.record_usage(child_activation,attempt,{'event_id':'before-extension','completion_tokens':100})
    payload={'action':'wait_children','speech':'waiting','wait':{'task_ids':[grandchild['task_id']]}}
    repo.save_result(child_activation,attempt,payload)
    repo.commit(child_activation,repo.validate_result(child_activation,payload))
    assert repo.dispatch(run_id)==[]
    assert repo.snapshot(run_id)['paused_reason']=='budget'
    assert all('total_max_tokens' not in t['budget'] for t in repo.snapshot(run_id)['tasks'])
    repo.update_limits(run_id,{**repo.snapshot(run_id)['limits'],'total_max_tokens':500})
    repo.resume(run_id)
    grandchild_activation=claim(repo,run_id)
    assert repo.context(grandchild_activation)['instance_id']==nodes['g']
    assert repo.context(grandchild_activation)['output_budget']==100
    deliver(repo,grandchild_activation,action='complete_task')
    resumed_child=claim(repo,run_id)
    assert repo.context(resumed_child)['instance_id']==nodes['c']
    deliver(repo,resumed_child,action='complete_task')
    assert repo.context(claim(repo,run_id))['instance_id']==nodes['p']


def test_session_extension_keeps_explicit_role_and_request_caps(repo):
    run_id,nodes,*_=make_run(repo,tokens=120,role_budget={'total_max_tokens':40})
    parent=claim(repo,run_id)
    child=repo.command(parent,'kds_delegate_task',{'request_id':'explicit-cap','child_instance_id':nodes['c'],
                       'goal':'Child','budget':{'total_max_tokens':20}})
    deliver(repo,parent,action='wait_children',wait={'task_ids':[child['task_id']]})
    repo.update_limits(run_id,{**repo.snapshot(run_id)['limits'],'total_max_tokens':500})
    child_activation=claim(repo,run_id)
    assert repo.context(child_activation)['output_budget']==20
    assert task(repo,run_id,child['task_id'])['budget']['total_max_tokens']==20
    root_task=next(t for t in repo.snapshot(run_id)['tasks'] if t['parent_task_id'] is None)
    assert root_task['budget']['total_max_tokens']==40


def test_implicit_budget_still_rejects_model_self_report_above_session_grant(repo):
    run_id,nodes,*_=make_run(repo,tokens=120)
    parent=claim(repo,run_id)
    with pytest.raises(ValueError):
        repo.command(parent,'kds_delegate_task',{'request_id':'too-large','child_instance_id':nodes['c'],
                     'goal':'Child','budget':{'total_max_tokens':121}})
    with pytest.raises(ValueError):
        repo.command(parent,'kds_spawn_subagent',{'request_id':'too-large-spawn','goal':'Child',
                     'role':{'name':'New','system_prompt':'Check'},'budget':{'total_max_tokens':121}})
    assert len(repo.snapshot(run_id)['agents'])==5
    assert len(repo.snapshot(run_id)['tasks'])==1


@pytest.mark.parametrize("role_budget", [{}, {"single_max_tokens": None}, {"single_max_tokens": 0}])
def test_unlimited_role_turn_reserves_actual_session_remaining_without_300_fallback(repo, role_budget):
    run_id, *_ = make_run(repo, tokens=1200, single=None, role_budget=role_budget)
    activation = claim(repo, run_id)
    assert repo.context(activation)["output_budget"] == 1180
    assert repo.snapshot(run_id)["usage"]["reserved_tokens"] == 1180


def test_unlimited_turn_uses_remaining_task_cap_and_preserves_finite_parent_grants(repo):
    run_id, *_ = make_run(repo, single=None, role_budget={"total_max_tokens": 80})
    activation = claim(repo, run_id)
    assert repo.context(activation)["output_budget"] == 80
    child = repo.command(activation, "kds_spawn_subagent", {
        "request_id": "unlimited-child", "goal": "Child",
        "role": {"name": "Child", "system_prompt": "CHECK", "default_budget": {"single_max_tokens": None}},
    })
    deliver(repo, activation, action="wait_children", wait={"task_ids": [child["task_id"]], "mode": "all"})
    child_activation = claim(repo, run_id)
    assert repo.context(child_activation)["output_budget"] == 80
    assert task(repo, run_id, child["task_id"])["budget"]["total_max_tokens"] == 80


def test_none_role_turn_does_not_override_explicit_session_single_cap(repo):
    run_id, *_ = make_run(repo, single=41, role_budget={"single_max_tokens": None})
    assert repo.context(claim(repo, run_id))["output_budget"] == 41


def test_null_child_turn_cannot_remove_explicit_parent_single_cap(repo):
    run_id, nodes, *_ = make_run(repo, single=None, role_budget={"single_max_tokens": 40})
    activation = claim(repo, run_id)
    with pytest.raises(ValueError, match="不能提高"):
        repo.command(activation, "kds_delegate_task", {
            "request_id": "relax-cap", "child_instance_id": nodes["c"],
            "goal": "Child", "budget": {"single_max_tokens": None}})
    child = repo.command(activation, "kds_spawn_subagent", {
        "request_id": "inherit-cap", "goal": "Child",
        "role": {"name": "Child", "system_prompt": "CHECK", "default_budget": {"single_max_tokens": 0}},
    })
    assert task(repo, run_id, child["task_id"])["budget"]["single_max_tokens"] == 40


def test_duration_only_unlimited_turn_reserves_a_slot_without_inventing_token_cap(repo):
    run_id, _, definition, request, role = make_run(repo)
    repo.delete(run_id)
    request = {**request, "request_id": "duration-only", "entry_node_ids": ["p", "z"],
               "limits": normalize_limits({"single_max_tokens": 0, "total_max_tokens": None,
                    "total_duration_seconds": 60, "max_concurrency": 2, "max_processes": 2})}
    run_id = repo.create_run(request, definition, {(role["id"], 1): role})
    activations = repo.dispatch(run_id, slots=10)
    assert len(activations) == 2
    assert all(repo.context(a)["output_budget"] is None for a in activations)
    with repo.reading() as conn:
        reservations = conn.execute("SELECT tokens,status FROM budget_reservations WHERE run_id=?", (run_id,)).fetchall()
    assert [(r["tokens"], r["status"]) for r in reservations] == [(0, "reserved"), (0, "reserved")]
    with pytest.raises(TeamConflict, match="并发"):
        repo.prepare_auxiliary(run_id, "score", {}, "pool-full")
    for activation in activations:
        deliver(repo, activation, action="complete_task")
    auxiliary = repo.prepare_auxiliary(run_id, "score", {}, "unlimited-aux")
    assert repo.auxiliary_context(auxiliary)["output_budget"] is None


def test_unlimited_role_reclaims_unused_reservation_before_next_root(repo):
    run_id, *_ = make_run(repo, entries=("p", "z"), concurrency=2, tokens=1000, single=None)
    activations = repo.dispatch(run_id, slots=2)
    assert len(activations) == 1
    first = activations[0]
    attempt = repo.start_attempt(first)["attempt_id"]
    repo.record_usage(first, attempt, {"event_id": "turn-usage", "completion_tokens": 600})
    result = {"action": "complete_task", "speech": "Done", "result": "Done"}
    repo.save_result(first, attempt, result)
    repo.commit(first, repo.validate_result(first, result))
    second = claim(repo, run_id)
    assert repo.context(second)["output_budget"] == 380


def test_spawn_inherits_only_enabled_tools_and_rejects_unknown_tools_atomically(repo, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DSH_TOOLS", ("read", "web_search"))
    run_id, *_ = make_run(repo)
    activation = claim(repo, run_id)
    before = repo.snapshot(run_id)
    for tools in (["web_search"], ["arbitrary_plugin"], ["write"]):
        with pytest.raises(ValueError):
            repo.command(activation, "kds_spawn_subagent", {
                "request_id": "invalid-" + tools[0], "goal": "Child",
                "role": {"name": "Child", "system_prompt": "CHECK", "tools": tools}})
        snapshot = repo.snapshot(run_id)
        assert len(snapshot["agents"]) == len(before["agents"])
        assert len(snapshot["tasks"]) == len(before["tasks"])
    child = repo.command(activation, "kds_spawn_subagent", {
        "request_id": "inherit-tools", "goal": "Child", "role": {"name": "Child", "system_prompt": "CHECK"}})
    agent = next(a for a in repo.snapshot(run_id)["agents"] if a["id"] == child["instance_id"])
    assert agent["role"]["tools"] == ["read"]


def test_source_metadata_persists_in_versions_and_frozen_running_roles(repo):
    metadata = {"preset_key": "supported-role", "sources": [{"title": "Source", "url": "https://example.com/docs"}]}
    role = repo.save_definition("role", validate_role({"name": "Checker", **metadata}))
    definition = repo.save_definition("team", validate_team_definition({"name": "Sourced", **metadata,
        "nodes": [{"id": "root", "role_id": role["id"], "role_version": 1}], "edges": []}))
    run_id = repo.create_run({"request_id": "sourced-run", "goal": "Check", "entry_node_ids": ["root"],
                             "limits": normalize_limits()}, definition, {(role["id"], 1): role})
    updated = validate_role({"name": "Changed", "sources": []})
    repo.save_definition("role", updated, role["id"], base_version=1)
    assert repo.list_versions("role", role["id"])[0]["sources"] == metadata["sources"]
    snapshot = repo.snapshot(run_id)
    for record in (snapshot["definition"], snapshot["agents"][0]["role"]):
        assert record["sources"] == metadata["sources"]
        assert record["preset_key"] == metadata["preset_key"]


def test_team_version_name_is_not_replaced_by_current_head_metadata(repo):
    _, _, definition, *_ = make_run(repo)
    old_name = definition["name"]
    updated = {**definition, "name": "Renamed team"}
    repo.save_definition("team", updated, definition["id"], base_version=1)
    historical = repo.get_definition("team", definition["id"], version=1)
    assert historical["name"] == old_name
    assert historical["version"] == 1
    assert historical["id"] == definition["id"]
    assert repo.get_definition("team", definition["id"])["name"] == "Renamed team"


def test_task_result_query_yields_owned_pending_children_with_deduplicated_receipt(repo):
    run_id, nodes, *_ = make_run(repo)
    parent = claim(repo, run_id)
    first = delegate(repo, parent, nodes['c'], 'first-child')
    second = delegate(repo, parent, nodes['b'], 'second-child')
    args = {'request_id':'query-pending', 'task_ids':[first['task_id'], second['task_id'], first['task_id']]}
    result = repo.command(parent, 'kds_get_task_results', args)
    ids = [first['task_id'], second['task_id']]
    assert [t['id'] for t in result['tasks']] == ids
    assert result['kds_control'] == {'action':'wait_children','task_ids':ids,'mode':'all'}
    assert repo.command(parent, 'kds_get_task_results', args) == result
    with repo.reading() as conn:
        rows = conn.execute("SELECT result FROM team_receipts WHERE request_id='query-pending'").fetchall()
    assert len(rows) == 1 and json.loads(rows[0]['result']) == result
    # Querying preserves state until the graph commits the requested wait.
    assert all(t['status'] == 'queued' for t in result['tasks'])
    assert repo.context(parent)['task']['status'] == 'running'
    deliver(repo, parent, action='wait_children', wait={'task_ids':ids,'mode':'all'})
    assert repo.snapshot(run_id)['usage']['reserved_tokens'] == 0
    assert len(repo.dispatch(run_id, slots=1)) == 1


def test_task_result_query_signal_includes_terminal_children_but_not_empty_or_all_terminal(repo):
    run_id, nodes, *_ = make_run(repo)
    parent = claim(repo, run_id)
    first = delegate(repo, parent, nodes['c'], 'first-child')
    second = delegate(repo, parent, nodes['b'], 'second-child')
    repo.command(parent, 'kds_cancel_task', {'request_id':'cancel-first','task_id':first['task_id']})
    args = {'task_ids':[first['task_id'], second['task_id']]}
    mixed = repo.command(parent, 'kds_get_task_results', {**args,'request_id':'mixed-query'})
    assert [t['status'] for t in mixed['tasks']] == ['cancelled','queued']
    assert mixed['kds_control']['task_ids'] == args['task_ids']
    repo.command(parent, 'kds_cancel_task', {'request_id':'cancel-second','task_id':second['task_id']})
    terminal = repo.command(parent, 'kds_get_task_results', {**args,'request_id':'terminal-query'})
    assert 'kds_control' not in terminal
    assert [t['status'] for t in terminal['tasks']] == ['cancelled','cancelled']
    for payload in ({'task_ids':[]}, {}):
        empty = repo.command(parent, 'kds_get_task_results', {'request_id':'empty-query-'+str(len(payload)), **payload})
        assert empty == {'tasks':[]}


@pytest.mark.parametrize('task_ids', [None, 'task-id', {}, [None], [True], [{}], [''], [' '], ['not-owned']])
def test_task_result_query_rejects_malformed_or_unknown_ids_without_receipt(repo, task_ids):
    run_id, *_ = make_run(repo)
    parent = claim(repo, run_id)
    with pytest.raises(ValueError):
        repo.command(parent, 'kds_get_task_results', {'request_id':'invalid-query','task_ids':task_ids})
    with repo.reading() as conn:
        assert conn.execute("SELECT count(*) FROM team_receipts WHERE request_id='invalid-query'").fetchone()[0] == 0


def test_task_result_query_rejects_another_parents_task_atomically(repo):
    run_id, nodes, *_ = make_run(repo, entries=('p','z'), concurrency=2)
    parents = {repo.context(a)['instance_id']:a for a in repo.dispatch(run_id, slots=2)}
    own = delegate(repo, parents[nodes['p']], nodes['c'], 'owned-child')
    foreign = repo.command(parents[nodes['z']], 'kds_spawn_subagent', {
        'request_id':'other-child','goal':'Other','role':{'name':'Other','system_prompt':'Other'}})
    with pytest.raises(ValueError, match='只能操作'):
        repo.command(parents[nodes['p']], 'kds_get_task_results', {
            'request_id':'cross-parent-query','task_ids':[own['task_id'], foreign['task_id']]})
    with repo.reading() as conn:
        assert conn.execute("SELECT count(*) FROM team_receipts WHERE request_id='cross-parent-query'").fetchone()[0] == 0


def test_task_result_query_replays_original_wait_receipt_across_parent_activations(repo):
    run_id, nodes, *_ = make_run(repo)
    parent = claim(repo, run_id)
    child = delegate(repo, parent, nodes['c'], 'child')
    args = {'request_id':'pending-query','task_ids':[child['task_id']]}
    original = repo.command(parent, 'kds_get_task_results', args)
    deliver(repo, parent, action='wait_children', wait={'task_ids':[child['task_id']],'mode':'all'})
    child_activation = claim(repo, run_id)
    deliver(repo, child_activation, action='complete_task')
    resumed = claim(repo, run_id)
    assert repo.command(resumed, 'kds_get_task_results', args) == original
    current = repo.command(resumed, 'kds_get_task_results', {**args,'request_id':'fresh-query'})
    assert current['tasks'][0]['status'] == 'succeeded'
    assert 'kds_control' not in current
    with pytest.raises(TeamConflict):
        repo.command(resumed, 'kds_get_task_results', {**args,'task_ids':[]})


def scheduling_yield(query, request_id):
    ids = query['kds_control']['task_ids']
    return {'action':'wait_children','speech':'等待子任务。','result':None,
            'wait':{'task_ids':ids,'mode':'all'},
            'scheduling_control':{'kind':'pending_children_yield','request_id':request_id,'task_ids':ids},
            'state':{'pending':False}}


def test_pending_query_yield_at_single_process_releases_slot_for_children(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=1)
    repo.human_message(run_id, {'request_id':'mail','target_id':nodes['p'],'content':'Supplement'})
    parent = claim(repo, run_id)
    parent_task = repo.context(parent)['task_id']
    child = delegate(repo, parent, nodes['c'])
    query = repo.command(parent, 'kds_get_task_results', {'request_id':'query','task_ids':[child['task_id']]})
    deliver(repo, parent, **scheduling_yield(query, 'query'))
    assert task(repo, run_id, parent_task)['status'] == 'waiting_children'
    assert task(repo, run_id, parent_task).get('result') is None
    assert repo.snapshot(run_id)['usage']['reserved_tokens'] == 0
    child_activation = claim(repo, run_id)
    assert repo.context(child_activation)['task_id'] == child['task_id']
    deliver(repo, child_activation, action='complete_task', result='Verified child result')
    resumed = claim(repo, run_id)
    history = json.loads(repo.context(resumed)['history'])
    assert any(m['content']=='Supplement' for m in history['messages'])
    assert history['child_results'][0]['result'] == 'Verified child result'
    deliver(repo, resumed, action='complete_task', result='Verified parent result')
    assert task(repo, run_id, parent_task)['result'] == 'Verified parent result'
    with repo.reading() as conn:
        assert conn.execute('SELECT count(*) FROM mailbox_deliveries WHERE instance_id=? AND consumed_by IS NULL', (nodes['p'],)).fetchone()[0] == 0


def test_pending_query_yield_preserves_input_but_consumes_completed_child_mail(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=3, board=True)
    repo.human_message(run_id, {'request_id':'before-yield','target_id':nodes['p'],'content':'请保留补充要求。'})
    parent = claim(repo, run_id)
    parent_task = repo.context(parent)['task_id']
    first = delegate(repo, parent, nodes['c'], 'first')
    second = delegate(repo, parent, nodes['b'], 'second')
    unqueried = repo.command(parent, 'kds_spawn_subagent', {
        'request_id':'unqueried','goal':'Independent','role':{'name':'Other','system_prompt':'Other'}})
    args = {'request_id':'pending-two','task_ids':[first['task_id'],second['task_id'],first['task_id']]}
    original = repo.command(parent, 'kds_get_task_results', args)
    result = scheduling_yield(original, args['request_id'])
    repo.human_message(run_id, {'request_id':'during-yield','target_id':nodes['p'],'content':'另外一条补充。'})
    child_activations = repo.dispatch(run_id, slots=2)
    assert {repo.context(a)['task_id'] for a in child_activations} == {first['task_id'],second['task_id']}
    for activation in child_activations:
        deliver(repo, activation, action='complete_task', result=repo.context(activation)['task_id'])
    # Both queried children finished before the synthetic wait is committed.
    deliver(repo, parent, **result)
    current = task(repo, run_id, parent_task)
    assert current['status'] == 'queued' and current.get('result') is None
    assert repo.snapshot(run_id)['usage']['reserved_tokens'] == 0
    assert current['wait']['task_ids'] == [first['task_id'],second['task_id']]
    assert task(repo, run_id, unqueried['task_id'])['status'] == 'queued'
    assert repo.snapshot(run_id)['whiteboard']['rev'] == 0
    with repo.reading() as conn:
        assert conn.execute("SELECT count(*) FROM mailbox_deliveries d JOIN team_messages m ON m.id=d.message_id "
                            "WHERE d.instance_id=? AND json_extract(m.payload,'$.kind')='human' AND d.consumed_by IS NULL", (nodes['p'],)).fetchone()[0] == 2
    resumed = repo.dispatch(run_id, slots=1)[0]
    history = json.loads(repo.context(resumed)['history'])
    assert {m['content'] for m in history['messages'] if m['kind']=='human'} == {'请保留补充要求。','另外一条补充。'}
    assert {t['id'] for t in history['child_results']} == {first['task_id'],second['task_id']}
    # Replaying the old pending query keeps its exact scope, while commit reads
    # current terminal states and immediately queues the parent again.
    assert repo.command(resumed, 'kds_get_task_results', args) == original
    deliver(repo, resumed, **result)
    assert task(repo, run_id, parent_task)['status'] == 'queued'
    with repo.reading() as conn:
        assert conn.execute("SELECT count(*) FROM mailbox_deliveries d JOIN team_messages m ON m.id=d.message_id "
                            "WHERE d.instance_id=? AND json_extract(m.payload,'$.kind')='child_result' AND d.consumed_by IS NULL", (nodes['p'],)).fetchone()[0] == 0
    actual = repo.dispatch(run_id, slots=1)[0]
    deliver(repo, actual, action='continue')
    with repo.reading() as conn:
        assert conn.execute("SELECT count(*) FROM mailbox_deliveries WHERE instance_id=? AND consumed_by IS NULL", (nodes['p'],)).fetchone()[0] == 0


@pytest.mark.parametrize('mismatch', ['request_id','wait_scope','missing_receipt','result','whiteboard'])
def test_pending_query_yield_requires_matching_durable_owned_query(repo, mismatch):
    run_id, nodes, *_ = make_run(repo, board=True)
    repo.human_message(run_id, {'request_id':'mail','target_id':nodes['p'],'content':'Do not lose this'})
    parent = claim(repo, run_id)
    first = delegate(repo, parent, nodes['c'], 'first')
    second = delegate(repo, parent, nodes['b'], 'second')
    query = repo.command(parent, 'kds_get_task_results', {'request_id':'query','task_ids':[first['task_id']]})
    raw = scheduling_yield(query, 'query')
    if mismatch == 'request_id':
        raw['scheduling_control']['request_id'] = 'unconfirmed'
    elif mismatch == 'wait_scope':
        raw['wait']['task_ids'] = [second['task_id']]
    elif mismatch == 'result':
        raw['result'] = 'invented delivery'
    elif mismatch == 'whiteboard':
        raw['whiteboard'] = {'base_rev':0,'ops':[{'op':'append','content':'invented artifact'}]}
    if mismatch == 'missing_receipt':
        with repo.transaction() as conn:
            conn.execute("DELETE FROM team_receipts WHERE request_id='query'")
    attempt = repo.start_attempt(parent)['attempt_id']
    repo.save_result(parent, attempt, raw)
    with pytest.raises(TeamConflict):
        repo.commit(parent, repo.validate_result(parent, raw))
    assert repo.operation(parent)['status'] == 'result_ready'
    assert repo.snapshot(run_id)['whiteboard']['rev'] == 0
    with repo.reading() as conn:
        assert conn.execute("SELECT count(*) FROM mailbox_deliveries WHERE instance_id=? AND consumed_by IS NULL", (nodes['p'],)).fetchone()[0] == 1


def test_pending_query_yield_cannot_commit_after_parent_cancels_subtree(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    root = claim(repo, run_id)
    child = delegate(repo, root, nodes['c'])
    repo.human_message(run_id, {'request_id':'mail','target_id':nodes['c'],'content':'Keep queued'})
    parent = claim(repo, run_id)
    grandchild = delegate(repo, parent, nodes['g'])
    query = repo.command(parent, 'kds_get_task_results', {'request_id':'pending','task_ids':[grandchild['task_id']]})
    raw = scheduling_yield(query, 'pending')
    attempt = repo.start_attempt(parent)['attempt_id']
    repo.save_result(parent, attempt, raw)
    clean = repo.validate_result(parent, raw)
    repo.command(root, 'kds_cancel_task', {'request_id':'cancel-parent','task_id':child['task_id']})
    before = len(repo.snapshot(run_id)['messages'])
    with pytest.raises(TeamConflict, match='失效'):
        repo.commit(parent, clean)
    assert task(repo, run_id, child['task_id'])['status'] == 'cancelled'
    assert task(repo, run_id, grandchild['task_id'])['status'] == 'cancelled'
    assert len(repo.snapshot(run_id)['messages']) == before
    with repo.reading() as conn:
        assert conn.execute("SELECT count(*) FROM mailbox_deliveries d JOIN team_messages m ON m.id=d.message_id "
                            "WHERE d.instance_id=? AND json_extract(m.payload,'$.kind')='human' AND d.consumed_by IS NULL", (nodes['c'],)).fetchone()[0] == 1
