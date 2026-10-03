"""Whole team workflows on independent databases, no paid model calls."""
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import pytest
from flask import Flask
from langgraph.checkpoint.memory import InMemorySaver

from app.repositories.teams import TeamRepository,TeamConflict
from app.services.team_sessions import TeamSessionService,MockTeamExecutor
from app.routes.team_api import team_api_bp


@pytest.fixture
def service(tmp_path):
    service=TeamSessionService(TeamRepository(tmp_path/'business.db'),MockTeamExecutor(),checkpointer=InMemorySaver())
    yield service
    service.close()


def configured(service,nodes=4,**limits):
    role=service.create_role({'name':'可复用校验员','system_prompt':'检查目标并正式交付。','tools':['read']})
    ns=[{'id':str(i),'name':'实例 '+str(i),'role_id':role['id'],'role_version':1,
         'position':{'x':i*220,'y':20}} for i in range(nodes)]
    edges=[{'id':'t'+str(i),'type':'task','source':str((i-1)//2),'target':str(i)} for i in range(1,nodes)]
    if nodes>=3:
        edges.append({'id':'room','type':'room','source':'1','target':'2'})
    team=service.create_team({'name':'测试树','nodes':ns,'edges':edges})
    run=service.create_run({'team_id':team['id'],'team_version':1,'entry_node_ids':['0'],
                          'goal':'核对嵌套目标','request_id':'start','limits':{'max_concurrency':1,'max_processes':1,**limits}})
    return team,run


def wait_until(service,run_id,predicate,timeout=8):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        snapshot=service.get_snapshot(run_id)
        if predicate(snapshot):
            return snapshot
        time.sleep(.02)
    runner=service.runners.get(run_id)
    raise AssertionError((service.get_snapshot(run_id),getattr(runner,'error',None)))


def test_three_levels_single_slot_and_frozen_versions(service):
    team,run=configured(service)
    snapshot=wait_until(service,run['id'],lambda s:s['status']=='paused')
    assert snapshot['paused_reason']=='tasks_finished'
    assert len(snapshot['tasks'])==4
    assert all(task['status']=='succeeded' for task in snapshot['tasks'])
    assert len({task['instance_id'] for task in snapshot['tasks']})==4
    assert snapshot['usage']['completion_tokens']==6*24
    assert snapshot['usage']['reserved_tokens']==0
    assert snapshot['status']!='completed'
    changed={**team,'base_version':1,'name':'下次生效'}
    updated=service.update_team(team['id'],changed)
    assert updated['version']==2
    assert service.get_snapshot(run['id'])['definition']['name']=='测试树'


def test_http_end_to_end_roles_tree_events_and_manual_completion(service):
    app=Flask(__name__)
    app.extensions['team_service']=service
    app.register_blueprint(team_api_bp)
    client=app.test_client()
    team,run=configured(service)
    snapshot=wait_until(service,run['id'],lambda s:s['status']=='paused')
    agent=snapshot['agents'][0]
    msg=client.post(f"/api/team-runs/{run['id']}/messages",json={'target_type':'agent','target_id':agent['id'],'content':'补充信息','request_id':'one'})
    assert msg.status_code==201
    assert msg.json['queued']
    assert len(service.list_tasks(run['id']))==4
    events=client.get(f"/api/team-runs/{run['id']}/events?after=0").json
    assert events['events'] and events['next_after']<=events['event_seq']
    result=client.post(f"/api/team-runs/{run['id']}/finalize",json={'summarize':True})
    assert result.status_code==200
    assert result.json['status']=='completed'
    assert '离线总结' in result.json['summary']
    assert result.json['usage']['completion_tokens']==168
    assert client.post(f"/api/team-runs/{run['id']}/messages",json={'target_type':'agent','target_id':agent['id'],'content':'新信息'}).status_code==409


def test_summary_failure_preserves_completion(service):
    _,run=configured(service,nodes=1)
    wait_until(service,run['id'],lambda s:s['status']=='paused')
    def failed(*args,**kwargs):
        raise RuntimeError('辅助调用失败')
    service.executor.execute_auxiliary=failed
    result=service.finalize(run['id'],{'summarize':True})
    assert result['status']=='completed'
    assert result['summary_error']=='辅助调用失败'


def test_automatic_room_replies_stop_and_human_agent_messages_never_start_work(service):
    _,run=configured(service,nodes=3)
    snapshot=wait_until(service,run['id'],lambda s:s['status']=='paused')
    initial=len(snapshot['tasks'])
    room=snapshot['rooms'][0]
    service.send_human_message(run['id'],{'target_type':'room','target_id':room['id'],'content':'讨论一次','request_id':'room'})
    service.resume(run['id'])
    snapshot=wait_until(service,run['id'],lambda s:s['status']=='paused')
    assert len(snapshot['tasks'])==initial+2
    assert len(service.list_messages(run['id'],room_id=room['id']))==3
    instance=snapshot['agents'][0]
    service.send_human_message(run['id'],{'target_type':'agent','target_id':instance['id'],'content':'保留到下一次合法激活','request_id':'agent'})
    service.resume(run['id'])
    snapshot=wait_until(service,run['id'],lambda s:s['status']=='paused')
    assert len(snapshot['tasks'])==initial+2


def test_local_channel_is_bound_to_live_activation_and_rejects_stale_credentials(service):
    service.autostart=False
    _,run=configured(service,nodes=2)
    activation=service.repository.dispatch(run['id'])[0]
    service.repository.start_attempt(activation)
    channel=service.get_channel()
    token=channel.bind(activation)
    context=service.repository.context(activation)
    child=json.loads(context['history'])['children'][0]
    body=json.dumps({'tool':'kds_delegate_task','args':{'child_instance_id':child['id'],'goal':'子任务','request_id':'stable'}}).encode()
    request=Request(channel.url,data=body,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urlopen(request) as response:
        first=json.load(response)
    with urlopen(request) as response:
        assert json.load(response)==first
    service.pause(run['id'])
    with pytest.raises(HTTPError) as exc:
        urlopen(request)
    assert exc.value.code==409
    channel.unbind(token)
    with pytest.raises(HTTPError) as exc:
        urlopen(request)
    assert exc.value.code==403


def test_auxiliary_uses_same_executor_and_ledger(service):
    service.autostart=False
    _,run=configured(service,nodes=1)
    service.pause(run['id'])
    result=service.run_auxiliary(run['id'],{'kind':'assist','arguments':{'goal':'配置建议'},'request_id':'assist'})
    assert result['result']['reply']
    assert service.get_snapshot(run['id'])['usage']['completion_tokens']==24
    assert service.run_auxiliary(run['id'],{'kind':'assist','arguments':{'goal':'配置建议'},'request_id':'assist'})==result
    assert service.get_snapshot(run['id'])['usage']['completion_tokens']==24


def test_full_execution_pool_still_cancels_at_wall_clock_limit(service):
    from app.harness import HarnessTurnError
    started,stopped=threading.Event(),threading.Event()
    def blocked(context,**kwargs):
        started.set()
        assert kwargs['cancel_event'].wait(4)
        stopped.set()
        raise HarnessTurnError('时长到限，调用已停止','cancelled')
    service.executor.execute=blocked
    _,run=configured(service,nodes=1,total_duration_seconds=10)
    assert started.wait(2)
    elapsed=service.get_snapshot(run['id'])['elapsed_seconds']
    service.update_limits(run['id'],{'total_duration_seconds':elapsed+.15})
    snapshot=wait_until(service,run['id'],lambda s:s['status']=='paused')
    assert snapshot['paused_reason']=='limit'
    assert stopped.wait(2)
    assert snapshot['tasks'][0]['uncertain']
    assert snapshot['usage']['reserved_tokens']==0


def test_observed_agent_mail_changes_from_queued_to_consumed_only_after_commit(service):
    service.autostart=False
    _,run=configured(service,nodes=1)
    instance=run['agents'][0]
    service.send_human_message(run['id'],{'target_type':'agent','target_id':instance['id'],'content':'冻结输入中的补充','request_id':'mail'})
    before=service.list_messages(run['id'],instance_id=instance['id'])[0]
    assert before['delivery_status']=='pending' and before['queued'] and before['consumed_at'] is None
    activation=service.repository.dispatch(run['id'])[0]
    receipt=service.repository.start_attempt(activation)
    result={'action':'complete_task','speech':'交付'}
    service.repository.save_result(activation,receipt['attempt_id'],result)
    assert service.list_messages(run['id'],instance_id=instance['id'])[0]['queued']
    service.repository.commit(activation,service.repository.validate_result(activation,result))
    after=service.list_messages(run['id'],instance_id=instance['id'])[0]
    assert after['delivery_status']=='consumed' and not after['queued'] and after['consumed_at']


def test_application_slot_is_held_until_dsh_process_is_closed_across_runs(service):
    close_started,release_close,second_started=threading.Event(),threading.Event(),threading.Event()
    first_instance=[]
    class RetainedProcessExecutor(MockTeamExecutor):
        def execute(self,context,**kwargs):
            if not first_instance:
                first_instance.append(context['instance_id'])
            elif context['instance_id']!=first_instance[0]:
                second_started.set()
            return super().execute(context,**kwargs)
        def close(self,instance_id=None):
            if first_instance and instance_id==first_instance[0]:
                close_started.set()
                assert release_close.wait(4)
    service.executor=RetainedProcessExecutor()
    service.request_slots=threading.BoundedSemaphore(1)
    service.autostart=False
    team,first=configured(service,nodes=1)
    second=service.create_run({'team_id':team['id'],'team_version':1,'entry_node_ids':['0'],
                              'goal':'第二个独立运行','request_id':'second-run'})
    service.autostart=True
    try:
        service.start(first['id'])
        assert close_started.wait(2)
        service.start(second['id'])
        assert not second_started.wait(.15)
        release_close.set()
        assert second_started.wait(2)
        wait_until(service,second['id'],lambda s:s['status']=='paused')
    finally:
        release_close.set()


@pytest.mark.parametrize('cause',['cancel','duration'])
def test_same_instance_queue_waits_for_cancelled_workers_process_close(service,cause):
    first_started,close_started=threading.Event(),threading.Event()
    release_close,second_started=threading.Event(),threading.Event()
    first_instance=[]
    children=[]
    class QueuedExecutor(MockTeamExecutor):
        def execute(self,context,**kwargs):
            repo=context['_repository']
            if context['instance']['node_id']=='0':
                if not children:
                    child=next(i for i in repo.list_entities(context['conversation_id'],'agent_instances')
                               if i['parent_id']==context['instance_id'])
                    for name in ('first','second'):
                        args={'request_id':'delegate-'+name,'child_instance_id':child['id'],'goal':name}
                        if cause=='duration' and name=='first':
                            args['budget']={'total_duration_seconds':.4}
                        children.append(repo.command(context['activation_id'],'kds_delegate_task',args)['task_id'])
                    wait={'task_ids':children[:],'mode':'all'}
                    if cause=='cancel':
                        wait['timeout_seconds']=.1
                    return {'action':'wait_children','speech':'等待队列','result':None,'wait':wait}
                current={t['id']:t for t in repo.list_entities(context['conversation_id'],'agent_tasks')}
                if cause=='cancel' and current[children[0]]['status'] not in {'failed','cancelled','succeeded'}:
                    assert first_started.wait(2)
                    repo.command(context['activation_id'],'kds_cancel_task',{'request_id':'cancel-first','task_id':children[0]})
                    service.notify_activation(context['activation_id'])
                if current[children[1]]['status']!='succeeded':
                    return {'action':'wait_children','speech':'继续等待','result':None,
                            'wait':{'task_ids':children[:],'mode':'all'}}
                return {'action':'complete_task','speech':'父任务完成','result':'parent'}
            if context['task']['goal']=='first':
                first_instance.append(context['instance_id'])
                first_started.set()
                assert kwargs['cancel_event'].wait(4)
                return {'action':'complete_task','speech':'迟到交付','result':'must not commit'}
            second_started.set()
            return {'action':'complete_task','speech':'排队任务完成','result':'second'}
        def close(self,instance_id=None):
            if first_instance and instance_id==first_instance[0]:
                close_started.set()
                assert release_close.wait(4)
    service.executor=QueuedExecutor()
    service.request_slots=threading.BoundedSemaphore(2)
    service.autostart=False
    _,run=configured(service,nodes=2,max_concurrency=2,max_processes=2,single_max_tokens=100)
    service.autostart=True
    try:
        service.start(run['id'])
        assert first_started.wait(2)
        closed=close_started.wait(2)
        observed=service.get_snapshot(run['id'])
        assert closed, (observed['status'],observed.get('paused_reason'),
                        [(t['goal'],t['status'],t.get('wait'),t.get('error')) for t in observed['tasks']],
                        getattr(service.runners[run['id']],'error',None))
        assert not second_started.wait(.2), '旧进程退出前，同实例第二个任务被启动'
        snapshot=service.get_snapshot(run['id'])
        current={t['id']:t for t in snapshot['tasks']}
        assert current[children[0]]['status']==('cancelled' if cause=='cancel' else 'failed')
        assert current[children[1]]['status']=='queued'
        assert current[children[0]].get('result') is None
        release_close.set()
        assert second_started.wait(2)
        final=wait_until(service,run['id'],lambda s:s['status']=='paused')
        assert next(t for t in final['tasks'] if t['id']==children[1])['result']=='second'
        assert all(m['content']!='迟到交付' for m in final['messages'])
        assert final['usage']['reserved_tokens']==0
    finally:
        release_close.set()


def test_dispatch_worker_exclusion_keeps_other_instances_eligible(service):
    service.autostart=False
    _,run=configured(service,nodes=3,max_concurrency=3,max_processes=3,single_max_tokens=100)
    repo=service.repository
    parent=repo.dispatch(run['id'])[0]
    instances={i['node_id']:i['id'] for i in run['agents']}
    accepted=[]
    for name,target in (('first','1'),('second','1'),('independent','2')):
        accepted.append(repo.command(parent,'kds_delegate_task',{
            'request_id':name,'goal':name,'child_instance_id':instances[target]})['task_id'])
    result={'action':'wait_children','speech':'等待三个结果','result':None,'wait':{'task_ids':accepted,'mode':'all'}}
    attempt=repo.start_attempt(parent)['attempt_id']
    repo.save_result(parent,attempt,result)
    repo.commit(parent,repo.validate_result(parent,result))
    eligible=repo.dispatch(run['id'],slots=3,blocked_instance_ids=(instances['1'],))
    assert len(eligible)==1 and repo.context(eligible[0])['task_id']==accepted[2]
    queued={t['id']:t for t in repo.snapshot(run['id'])['tasks']}
    assert all(queued[identifier]['status']=='queued' and queued[identifier]['activation_count']==0 for identifier in accepted[:2])
    # Removing the runtime-only exclusion admits the first queued task, never
    # both tasks for the same instance in one coordination pass.
    first=repo.dispatch(run['id'],slots=2)
    assert len(first)==1 and repo.context(first[0])['task_id']==accepted[0]


def test_waiting_instance_queue_does_not_invoke_empty_dispatch_graph(service,monkeypatch):
    grandchild_started,release_grandchild=threading.Event(),threading.Event()
    second_started=threading.Event()
    dispatches=[]
    invoke=service.dispatch_graph.invoke
    def observed_dispatch(*args,**kwargs):
        dispatches.append(time.monotonic())
        return invoke(*args,**kwargs)
    monkeypatch.setattr(service.dispatch_graph,'invoke',observed_dispatch)
    class WaitingQueueExecutor(MockTeamExecutor):
        def execute(self,context,**kwargs):
            repo=context['_repository']
            node=context['instance']['node_id']
            owned=[t for t in repo.list_entities(context['conversation_id'],'agent_tasks')
                   if t['parent_task_id']==context['task_id']]
            if node in {'0','1'} and context['task']['goal']!='second' and not owned:
                child=next(i for i in repo.list_entities(context['conversation_id'],'agent_instances')
                           if i['parent_id']==context['instance_id'])
                goals=('first','second') if node=='0' else ('grandchild',)
                ids=[repo.command(context['activation_id'],'kds_delegate_task',{
                     'request_id':goal,'goal':goal,'child_instance_id':child['id']})['task_id'] for goal in goals]
                return {'action':'wait_children','speech':'等待直属结果','result':None,
                        'wait':{'task_ids':ids,'mode':'all'}}
            if context['task']['goal']=='grandchild':
                grandchild_started.set()
                assert release_grandchild.wait(4)
            if context['task']['goal']=='second':
                second_started.set()
            return {'action':'complete_task','speech':'完成','result':context['task']['goal']}
    service.executor=WaitingQueueExecutor()
    service.request_slots=threading.BoundedSemaphore(2)
    service.autostart=False
    _,run=configured(service,nodes=4,max_concurrency=2,max_processes=2,single_max_tokens=100)
    service.autostart=True
    try:
        service.start(run['id'])
        assert grandchild_started.wait(2)
        waiting=wait_until(service,run['id'],lambda s:any(t['goal']=='first' and t['status']=='waiting_children' for t in s['tasks']))
        assert any(t['goal']=='second' and t['status']=='queued' for t in waiting['tasks'])
        before=len(dispatches)
        # Span a heartbeat while the only queued task belongs to the waiting
        # instance. Deadline reconciliation continues without empty graph calls.
        assert not second_started.wait(1.2)
        assert len(dispatches)==before
        release_grandchild.set()
        assert second_started.wait(2)
        final=wait_until(service,run['id'],lambda s:s['status']=='paused')
        assert all(t['status']=='succeeded' for t in final['tasks'])
    finally:
        release_grandchild.set()


@pytest.mark.parametrize('failure',['wrapped_transport','harness_timeout'])
def test_transport_loss_and_harness_timeout_pause_uncertain_work(service,failure):
    from app.harness import HarnessTurnError
    def stopped(*args,**kwargs):
        if failure=='harness_timeout':
            raise HarnessTurnError('模型请求超时','timeout')
        try:
            raise OSError('runtime transport disappeared after request')
        except OSError as cause:
            raise HarnessTurnError('DSH调用失败','error') from cause
    service.executor.execute=stopped
    _,run=configured(service,nodes=1)
    snapshot=wait_until(service,run['id'],lambda s:s['status']=='paused')
    assert snapshot['tasks'][0]['status']=='paused'
    assert snapshot['tasks'][0]['uncertain'] is True
    with pytest.raises(TeamConflict):
        service.resume(run['id'])


def test_auxiliary_waiting_to_start_cannot_execute_after_manual_completion(service,monkeypatch):
    service.autostart=False
    _,run=configured(service,nodes=1)
    waiting,release=threading.Event(),threading.Event()
    original=service.repository.start_auxiliary
    external=[]
    def before_start(op_id):
        waiting.set()
        assert release.wait(8)
        return original(op_id)
    monkeypatch.setattr(service.repository,'start_auxiliary',before_start)
    original_execute=service.executor.execute_auxiliary
    def observed(*args,**kwargs):
        external.append(args[0])
        return original_execute(*args,**kwargs)
    service.executor.execute_auxiliary=observed
    errors=[]
    def auxiliary():
        try:
            service.run_auxiliary(run['id'],{'kind':'assist','request_id':'racing-assist'})
        except Exception as error:
            errors.append(error)
    thread=threading.Thread(target=auxiliary)
    thread.start()
    assert waiting.wait(3)
    try:
        with pytest.raises(TeamConflict):
            service.finalize(run['id'],{'summarize':False})
    finally:
        release.set()
        thread.join(8)
    assert not thread.is_alive()
    assert external==[]
    assert service.finalize(run['id'],{'summarize':False})['status']=='completed'


def test_concurrent_same_auxiliary_request_reuses_one_graph_and_external_call(service):
    service.autostart=False
    _,run=configured(service,nodes=1)
    service.pause(run['id'])
    started,release=threading.Event(),threading.Event()
    calls=[]
    original=service.executor.execute_auxiliary
    def waiting(*args,**kwargs):
        calls.append(args[0])
        started.set()
        assert release.wait(5)
        return original(*args,**kwargs)
    service.executor.execute_auxiliary=waiting
    results,errors=[],[]
    def request():
        try:
            results.append(service.run_auxiliary(run['id'],{'kind':'assist','request_id':'same'}))
        except Exception as error:
            errors.append(error)
    first=threading.Thread(target=request)
    second=threading.Thread(target=request)
    first.start()
    assert started.wait(3)
    second.start()
    # Ensure the second caller enters its ownership/receipt path before release.
    time.sleep(.1)
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert all(isinstance(error,TeamConflict) and error.status_code==409 for error in errors)
    assert len(results)+len(errors)==2 and results
    assert all(result==results[0] for result in results)
    assert calls==['assist']
    assert service.run_auxiliary(run['id'],{'kind':'assist','request_id':'same'})==results[0]
    assert service.get_snapshot(run['id'])['usage']['completion_tokens']==24
