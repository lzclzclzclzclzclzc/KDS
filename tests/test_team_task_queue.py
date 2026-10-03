"""A role accepts durable work independently of its single execution slot."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db
from app.repositories import teams
from app.repositories.teams import TeamConflict, TeamRepository
from test_team_repository import claim, delegate, deliver, make_run, repo, task


def assign(repo, parent, instance, request, goal):
    return repo.command(parent, 'kds_delegate_task', {
        'request_id': request, 'child_instance_id': instance,
        'goal': goal, 'input': {'assignment': goal}, 'acceptance': goal + '-accepted'})


@pytest.mark.parametrize('first_state', ['queued', 'running', 'waiting_children'])
def test_same_role_accepts_fifo_work_without_interrupting_its_current_task(repo, first_state):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    parent = claim(repo, run_id)
    first = assign(repo, parent, nodes['c'], 'first-job', 'First')
    first_activation = None
    grandchild = None
    if first_state != 'queued':
        first_activation = claim(repo, run_id)
    if first_state == 'waiting_children':
        grandchild = delegate(repo, first_activation, nodes['g'], 'nested-work')
        deliver(repo, first_activation, action='wait_children',
                wait={'task_ids': [grandchild['task_id']], 'mode': 'all'})
    before = next(a for a in repo.snapshot(run_id)['agents'] if a['id'] == nodes['c'])
    second = assign(repo, parent, nodes['c'], 'second-job', 'Second')
    assert second['status'] == 'queued' and second['task_id'] != first['task_id']
    assert assign(repo, parent, nodes['c'], 'second-job', 'Second') == second
    after = next(a for a in repo.snapshot(run_id)['agents'] if a['id'] == nodes['c'])
    assert (after['current_task_id'], after['epoch'], after['status']) == (
        before['current_task_id'], before['epoch'], before['status'])
    assert [t['id'] for t in repo.snapshot(run_id)['tasks'] if t['instance_id'] == nodes['c']] == [
        first['task_id'], second['task_id']]
    ids = [first['task_id'], second['task_id']]
    deliver(repo, parent, action='wait_children', wait={'task_ids': ids, 'mode': 'all'})
    if first_state == 'waiting_children':
        nested = claim(repo, run_id)
        assert repo.context(nested)['task_id'] == grandchild['task_id']
        assert task(repo, run_id, second['task_id'])['status'] == 'queued'
        deliver(repo, nested, action='complete_task')
        first_activation = claim(repo, run_id)
    elif first_state == 'queued':
        first_activation = claim(repo, run_id)
    assert repo.context(first_activation)['task_id'] == first['task_id']
    assert repo.dispatch(run_id, slots=10) == []
    # A continuation owns the role until the whole first task ends.
    deliver(repo, first_activation, action='continue')
    continued = claim(repo, run_id)
    assert repo.context(continued)['task_id'] == first['task_id']
    assert task(repo, run_id, second['task_id'])['activation_count'] == 0
    deliver(repo, continued, action='complete_task', result='First result')
    next_activation = claim(repo, run_id)
    second_history = json.loads(repo.context(next_activation)['history'])
    assert second_history['task']['id'] == second['task_id']
    assert second_history['task']['goal'] == 'Second'
    assert second_history['task']['input'] == {'assignment': 'Second'}
    assert second_history['task']['acceptance'] == 'Second-accepted'
    assert repo.dispatch(run_id, slots=10) == []
    deliver(repo, next_activation, action='complete_task', result='Second result')
    resumed = claim(repo, run_id)
    results = {t['id']: t['result'] for t in json.loads(repo.context(resumed)['history'])['child_results']}
    assert results == {first['task_id']: 'First result', second['task_id']: 'Second result'}
    deliver(repo, resumed, action='complete_task')


@pytest.mark.parametrize('clock', ['same', 'backwards'])
def test_fifo_uses_acceptance_order_when_clocks_tie_or_move_backwards(repo, monkeypatch, clock):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    parent = claim(repo, run_id)
    original_uid = teams.uid
    ids = iter(['task_z_first', 'task_a_second'])
    monkeypatch.setattr(teams, 'uid', lambda prefix: next(ids) if prefix == 'task' else original_uid(prefix))
    monkeypatch.setattr(db, '_now', lambda: '2026-10-03T10:00:00.000000+00:00')
    first = delegate(repo, parent, nodes['c'], 'first-job')
    if clock == 'backwards':
        monkeypatch.setattr(db, '_now', lambda: '2026-10-02T10:00:00.000000+00:00')
    second = delegate(repo, parent, nodes['c'], 'second-job')
    deliver(repo, parent, action='wait_children', wait={'task_ids': [first['task_id'], second['task_id']]})
    first_activation = claim(repo, run_id)
    assert repo.context(first_activation)['task_id'] == 'task_z_first'
    deliver(repo, first_activation, action='complete_task')
    assert repo.context(claim(repo, run_id))['task_id'] == 'task_a_second'


def test_queue_limit_rejection_rolls_back_and_replay_does_not_consume_capacity(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    parent = claim(repo, run_id)
    repo.update_limits(run_id, {**repo.snapshot(run_id)['limits'], 'max_queue_length': 1})
    first = delegate(repo, parent, nodes['c'], 'first-job')
    assert delegate(repo, parent, nodes['c'], 'first-job') == first
    with pytest.raises(TeamConflict, match='队列'):
        delegate(repo, parent, nodes['c'], 'second-job')
    with repo.reading() as conn:
        assert conn.execute("SELECT count(*) FROM team_receipts WHERE request_id='second-job'").fetchone()[0] == 0
    assert len(repo.snapshot(run_id)['tasks']) == 2
    first_activation = claim(repo, run_id)
    second = delegate(repo, parent, nodes['c'], 'second-job')
    assert second['status'] == 'queued'
    assert repo.context(first_activation)['task_id'] == first['task_id']


def test_cancel_queued_work_preserves_active_work_lease_and_reservation(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    parent = claim(repo, run_id)
    first = delegate(repo, parent, nodes['c'], 'first-job')
    active = claim(repo, run_id)
    second = delegate(repo, parent, nodes['c'], 'second-job')
    before = next(a for a in repo.snapshot(run_id)['agents'] if a['id'] == nodes['c'])
    reservation = repo.snapshot(run_id)['usage']['reserved_tokens']
    repo.command(parent, 'kds_cancel_task', {'request_id': 'cancel-queued', 'task_id': second['task_id']})
    after = next(a for a in repo.snapshot(run_id)['agents'] if a['id'] == nodes['c'])
    assert (after['current_task_id'], after['epoch']) == (before['current_task_id'], before['epoch'])
    assert task(repo, run_id, first['task_id'])['status'] == 'running'
    assert task(repo, run_id, second['task_id'])['status'] == 'cancelled'
    assert repo.operation(active)['status'] == 'prepared'
    assert repo.snapshot(run_id)['usage']['reserved_tokens'] == reservation
    deliver(repo, parent, action='wait_children', wait={'task_ids': [first['task_id'], second['task_id']]})
    deliver(repo, active, action='complete_task', result='Still valid')
    resumed = claim(repo, run_id)
    results = json.loads(repo.context(resumed)['history'])['child_results']
    assert {t['status'] for t in results} == {'succeeded', 'cancelled'}


def test_queue_is_persisted_across_explicit_pause_and_repository_recovery(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    parent = claim(repo, run_id)
    first = delegate(repo, parent, nodes['c'], 'first-job')
    second = delegate(repo, parent, nodes['c'], 'second-job')
    deliver(repo, parent, action='wait_children', wait={'task_ids': [first['task_id'], second['task_id']]})
    deliver(repo, claim(repo, run_id), action='complete_task', result='Persisted first')
    repo.pause(run_id)
    restored = TeamRepository(repo.db_path)
    restored.recover()
    assert restored.dispatch(run_id, slots=10) == []
    assert task(restored, run_id, second['task_id'])['status'] == 'queued'
    restored.resume(run_id)
    next_activation = claim(restored, run_id)
    assert restored.context(next_activation)['task_id'] == second['task_id']
    deliver(restored, next_activation, action='complete_task', result='Persisted second')
    resumed = claim(restored, run_id)
    assert delegate(restored, resumed, nodes['c'], 'second-job') == second
    assert len(restored.snapshot(run_id)['tasks']) == 3


def test_expired_queued_task_does_not_end_another_task_on_the_same_role(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=2)
    parent = claim(repo, run_id)
    first = delegate(repo, parent, nodes['c'], 'first-job')
    active = claim(repo, run_id)
    second = repo.command(parent, 'kds_delegate_task', {
        'request_id': 'short-job', 'child_instance_id': nodes['c'], 'goal': 'Expiring work',
        'budget': {'total_duration_seconds': 1}})
    before = next(a for a in repo.snapshot(run_id)['agents'] if a['id'] == nodes['c'])
    with repo.transaction() as conn:
        row = conn.execute('SELECT payload FROM agent_tasks WHERE id=?', (second['task_id'],)).fetchone()
        payload = json.loads(row[0])
        payload['created_active_seconds'] = -10
        conn.execute('UPDATE agent_tasks SET payload=? WHERE id=?', (json.dumps(payload), second['task_id']))
    repo.heartbeat(run_id)
    after = next(a for a in repo.snapshot(run_id)['agents'] if a['id'] == nodes['c'])
    assert task(repo, run_id, second['task_id'])['status'] == 'failed'
    assert task(repo, run_id, first['task_id'])['status'] == 'running'
    assert (before['epoch'], before['current_task_id']) == (after['epoch'], after['current_task_id'])
    deliver(repo, active, action='complete_task', result='Unaffected first')


def test_concurrent_acceptance_and_ack_replays_preserve_one_role_execution(repo):
    run_id, nodes, *_ = make_run(repo, concurrency=3)
    parent = claim(repo, run_id)
    requests = ['job-a', 'job-b', 'job-a', 'job-c', 'job-b', 'job-d']
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda request: delegate(repo, parent, nodes['c'], request), requests))
    assert len({reply['task_id'] for reply in replies}) == 4
    assert replies[0] == replies[2] and replies[1] == replies[4]
    accepted_order = [event['task_id'] for event in repo.events(run_id)['events']
                      if event['type'] == 'task_queued' and event['instance_id'] == nodes['c']]
    assert len(accepted_order) == 4
    assert [t['id'] for t in repo.snapshot(run_id)['tasks'] if t['instance_id'] == nodes['c']] == accepted_order
    deliver(repo, parent, action='wait_children', wait={'task_ids': accepted_order})
    for expected in accepted_order:
        activation = claim(repo, run_id)
        assert repo.context(activation)['task_id'] == expected
        assert repo.dispatch(run_id, slots=10) == []
        deliver(repo, activation, action='complete_task', result=expected)
    resumed = claim(repo, run_id)
    assert len(json.loads(repo.context(resumed)['history'])['child_results']) == 4
