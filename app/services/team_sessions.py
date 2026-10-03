"""Team entry points, process ownership and finite LangGraph invocation."""
import atexit
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import config as settings
from app.domain.teams import (normalize_limits,validate_role,validate_team_definition,
                              validate_entry_nodes,narrow_capabilities)
from app.repositories.teams import TeamRepository,TeamConflict,encode,uid
from app.orchestration.agent_graph import AgentContext,build_agent_graph
from app.orchestration.dispatch_graph import DispatchContext,build_dispatch_graph
from app.orchestration.room_graph import AuxiliaryContext,build_auxiliary_graph
from app.orchestration.checkpointer import get_saver,delete_conversation


class CommandChannel:
    """Loopback-only tool endpoint. Tokens bind an activation, never model identity."""
    def __init__(self, service):
        self.service = service
        self.tokens = {}
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def do_POST(self):
                try:
                    if self.path!='/commands':
                        self.send_error(404); return
                    token = self.headers.get('Authorization','').removeprefix('Bearer ')
                    with owner.lock:
                        activation_id = owner.tokens.get(token)
                    if not activation_id:
                        self.send_error(403); return
                    length = int(self.headers.get('Content-Length','0'))
                    if not 0<length<=100000:
                        self.send_error(413); return
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body,dict) or not isinstance(body.get('args'),dict):
                        raise ValueError('工具参数必须是对象')
                    try:
                        result = owner.service.repository.command(activation_id,body.get('tool'),body['args'])
                    except (ValueError,KeyError) as error:
                        # command() has rolled back its transaction. A later
                        # notification failure must never receive this marker:
                        # the command would already have committed then.
                        status=getattr(error,'status_code',400)
                        result={'error':str(error),'kds_command_error':{
                            'kind':'rejected','accepted':False,'status':status,
                            'tool':body.get('tool'),'request_id':body['args'].get('request_id')}}
                    else:
                        owner.service.notify_activation(activation_id)
                        status = 200
                except (ValueError,KeyError) as error:
                    result,status = {'error':str(error)},getattr(error,'status_code',400)
                except Exception:
                    # Do not leak internal state, paths, tokens or tracebacks to a tool.
                    result,status = {'error':'编排命令保存失败'},500
                raw = encode(result).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        # Bun/browser fetch rejects a few historical unsafe ports. Bind a high
        # local port explicitly instead of relying on host ephemeral settings.
        for _ in range(50):
            try:
                self.server = ThreadingHTTPServer(('127.0.0.1',49152+secrets.randbelow(16384)),Handler)
                break
            except OSError:
                continue
        else:
            raise RuntimeError('无法建立本机编排工具通道')
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True,name='kds-team-tools')
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/commands'

    def bind(self, activation_id):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token] = activation_id
        return token

    def unbind(self, token):
        with self.lock:
            self.tokens.pop(token,None)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MockTeamExecutor:
    """Explicit offline mode; uses the same accepted-command and delivery rules."""
    def execute(self, context, command_url=None, credential=None, activity_cb=None,usage_cb=None,cancel_event=None):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError('激活已取消')
        repo = context['_repository']
        task, instance = context['task'],context['instance']
        children = [i for i in repo.list_entities(context['conversation_id'],'agent_instances') if i['parent_id']==instance['id']]
        tasks = [t for t in repo.list_entities(context['conversation_id'],'agent_tasks') if t['parent_task_id']==task['id']]
        if children and not tasks and task.get('kind')!='room_reply':
            task_ids=[]
            for child in children:
                result=repo.command(context['activation_id'],'kds_delegate_task',
                                    {'child_instance_id':child['id'],'goal':task['goal'],
                                     'input':task.get('input',{}),'request_id':'mock-delegate:'+child['id']})
                task_ids.append(result['task_id'])
            output={'action':'wait_children','speech':'已委派子任务，等待正式结果。','wait_for':task_ids,'wait_mode':'all'}
        else:
            output={'action':'complete_task','speech':f"{instance['name']} 已完成：{task['goal']}",
                    'result':{'text':f"{instance['name']} 的离线交付",'child_results':[t.get('result') for t in tasks]}}
        if usage_cb:
            budget=context['output_budget']
            usage_cb({'event_id':'mock-final','prompt_tokens':12,'completion_tokens':24 if budget is None else min(24,budget)})
        output['state']={'task_id':task['id']}
        return output

    def execute_auxiliary(self,kind,context,**kwargs):
        callback=kwargs.get('usage_cb')
        if callback:
            budget=context['output_budget']
            callback({'event_id':'mock-final','prompt_tokens':12,'completion_tokens':24 if budget is None else min(24,budget)})
        if kind=='summary':
            return {'summary':'离线总结：团队任务、正式结果和公开群聊记录已保存。'}
        if kind=='assist':
            return {'reply':'可复用角色，并用父子任务连线明确委派关系。','proposal':None}
        if kind=='score':
            return {'score':50}
        if kind=='vote':
            return {'choice':0}
        return {'reply':'离线辅助结果'}

    def close(self,instance_id=None):
        pass


class AgentNodes:
    def __init__(self, service, runner, activation_id, cancel):
        self.service,self.runner,self.activation_id,self.cancel = service,runner,activation_id,cancel
        self.delivery = None

    def load_operation(self, state):
        op=self.service.repository.operation(state['activation_id'])
        if op['status']=='committed':
            return {'route':'done','outcome':'committed'}
        self.service.repository.context(state['activation_id'])
        return {'route':'validate' if op['result'] else 'execute','result_ref':state['activation_id']}

    def execute(self, state):
        service=self.service
        repo=service.repository
        channel=service.get_channel()
        with service.request_slots:
            if self.cancel.is_set():
                raise TeamConflict('激活已暂停或取消')
            receipt=repo.start_attempt(state['activation_id'])
            if receipt['result'] is not None:
                return {'result_ref':state['activation_id']}
            context=repo.context(state['activation_id'])
            context.update(attempt_id=receipt['attempt_id'],_repository=repo)
            from app.team_harness import TEAM_TOOLS
            context['tools']=list(dict.fromkeys([*context['tools'],*TEAM_TOOLS]))
            context['model_config']=getattr(settings,'TEAM_MODEL_CONFIGS',{}).get(context['model_config'],{})
            token=channel.bind(state['activation_id'])
            try:
                if self.cancel.is_set():
                    raise TeamConflict('激活已暂停或取消')
                result=service.executor.execute(context,command_url=channel.url,credential=token,
                    activity_cb=lambda event:repo.record_activity(state['activation_id'],event),
                    usage_cb=lambda event:repo.record_usage(state['activation_id'],receipt['attempt_id'],event),
                    cancel_event=self.cancel)
                repo.save_result(state['activation_id'],receipt['attempt_id'],result)
            finally:
                channel.unbind(token)
                service.executor.close(context['instance_id'])
        return {'result_ref':state['activation_id']}

    def validate(self, state):
        self.delivery=self.service.repository.validate_result(state['activation_id'],self.service.repository.operation(state['activation_id'])['result'])
        return {'route':'commit'}

    def commit(self, state):
        # Validation may have been checkpointed before process loss; reconstruct it.
        if self.delivery is None:
            self.validate(state)
        outcome=self.service.repository.commit(state['activation_id'],self.delivery)
        return {'outcome':outcome}


class TeamRunner:
    def __init__(self,service,run_id):
        self.service,self.run_id=service,run_id
        self.wake=threading.Event()
        self.stopped=threading.Event()
        self.lock=threading.Lock()
        self.workers={}
        self.worker_instances={}
        self.thread=None

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                self.wake.set(); return
            self.stopped.clear()
            self.thread=threading.Thread(target=self.run,daemon=True,name='kds-team-'+self.run_id)
            self.thread.start()

    def run(self):
        service=self.service
        try:
            while not self.stopped.is_set():
                self.wake.clear()
                snapshot=service.repository.heartbeat(self.run_id)
                if not snapshot or snapshot['status']!='running':
                    break
                self.cancel_stale(snapshot)
                with self.lock:
                    available=max(0,min(snapshot['limits']['max_concurrency'],snapshot['limits']['max_processes'])-len(self.workers))
                    blocked_instances=tuple(self.worker_instances.values())
                current_tasks={instance['id']:instance.get('current_task_id') for instance in snapshot['agents']}
                ready=any(task['status']=='queued' and task['instance_id'] in current_tasks
                          and task['instance_id'] not in blocked_instances
                          and current_tasks[task['instance_id']] in (None,task['id']) for task in snapshot['tasks'])
                if available and ready:
                    result=service.dispatch_graph.invoke({'run_id':self.run_id,'slots':available,'activations':[]},
                        {'configurable':{'thread_id':f'conversation:{self.run_id}:team-control'}},
                        context=DispatchContext(service.repository,blocked_instances),durability='sync')
                    for activation_id in result['activations']:
                        self.launch(activation_id)
                snapshot=service.repository.snapshot(self.run_id)
                if not snapshot or snapshot['status']!='running':
                    break
                # Sleep on commands/completions or the nearest persisted deadline.
                elapsed=snapshot['elapsed_seconds']
                deadlines=[]
                duration=snapshot['limits'].get('total_duration_seconds')
                if duration is not None:
                    deadlines.append(duration-elapsed)
                deadlines.extend(t.get('wait',{}).get('deadline')-elapsed for t in snapshot['tasks']
                                 if t['status']=='waiting_children' and t.get('wait',{}).get('deadline') is not None)
                deadlines.extend(t.get('created_active_seconds',0)+t['budget']['total_duration_seconds']-elapsed
                                 for t in snapshot['tasks'] if t['status'] not in {'succeeded','failed','cancelled'}
                                 and t.get('budget',{}).get('total_duration_seconds') is not None)
                timeout=max(.02,min([1,*deadlines]))
                self.wake.wait(timeout)
        except Exception as error:
            self.error=str(error)
            try:
                service.repository.pause(self.run_id,'storage_error')
            except Exception:
                pass
        finally:
            self.cancel_workers()

    def launch(self,activation_id):
        instance_id=self.service.repository.operation(activation_id)['instance_id']
        cancel=threading.Event()
        def worker():
            try:
                context=self.service.repository.context(activation_id)
                thread_id=f'conversation:{self.run_id}:agent:{context["instance_id"]}'
                config={'configurable':{'thread_id':thread_id}}
                nodes=AgentNodes(self.service,self,activation_id,cancel)
                checkpoint=self.service.agent_graph.get_state(config)
                compatible=checkpoint and checkpoint.next and checkpoint.values.get('activation_id')==activation_id
                self.service.agent_graph.invoke(None if compatible else {'run_id':self.run_id,'activation_id':activation_id,'route':'','outcome':'','result_ref':''},
                    config,context=AgentContext(nodes),durability='sync')
            except TeamConflict:
                pass
            except Exception as error:
                try:
                    uncertain=isinstance(error,(OSError,TimeoutError)) or getattr(error,'reason',None) in {'error','timeout','cancelled'}
                    self.service.repository.fail_activation(activation_id,error,uncertain=uncertain)
                except Exception:
                    try:
                        self.service.repository.pause(self.run_id,'storage_error')
                    except Exception:
                        pass
            finally:
                with self.lock:
                    self.workers.pop(activation_id,None)
                    self.worker_instances.pop(activation_id,None)
                self.wake.set()
        thread=threading.Thread(target=worker,daemon=True,name='kds-agent-'+activation_id)
        with self.lock:
            self.workers[activation_id]=(thread,cancel)
            self.worker_instances[activation_id]=instance_id
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.workers.pop(activation_id,None)
                self.worker_instances.pop(activation_id,None)
            raise

    def cancel_workers(self):
        with self.lock:
            workers=list(self.workers.values())
        for _,cancel in workers:
            cancel.set()

    def cancel_stale(self,snapshot):
        tasks={task['id']:task for task in snapshot['tasks']}
        instances={instance['id']:instance for instance in snapshot['agents']}
        with self.lock:
            workers=list(self.workers.items())
        for activation_id,(_,cancel) in workers:
            operation=self.service.repository.operation(activation_id)
            task=tasks.get(operation['task_id'])
            instance=instances.get(operation['instance_id'])
            if (not task or task['status'] in {'succeeded','failed','cancelled'} or not instance
                    or operation['runner_epoch']!=snapshot['epoch']
                    or operation['instance_epoch']!=instance['epoch']):
                cancel.set()

    def stop(self):
        self.stopped.set(); self.wake.set(); self.cancel_workers()

    def join(self,timeout=5):
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        with self.lock:
            threads=[worker[0] for worker in self.workers.values()]
        deadline=time.monotonic()+timeout
        for thread in threads:
            thread.join(max(0,deadline-time.monotonic()))
        return not any(t.is_alive() for t in threads)


class TeamSessionService:
    def __init__(self,repository=None,executor=None,autostart=True,checkpointer=None):
        self.repository=repository or TeamRepository()
        self.autostart=autostart
        from app.orchestration.team_ownership import acquire_team_owner
        self.owner=acquire_team_owner(self.repository.db_path) if autostart else None
        self.closed=False
        if executor is None:
            if settings.LLM_MOCK:
                executor=MockTeamExecutor()
            else:
                from app.team_harness import TeamHarnessAdapter
                executor=TeamHarnessAdapter()
        self.executor=executor
        self.request_slots=threading.BoundedSemaphore(getattr(settings,'TEAM_DSH_MAX_CONCURRENCY',4))
        self.saver=checkpointer if checkpointer is not None else get_saver()
        self.agent_graph=build_agent_graph(self.saver)
        self.dispatch_graph=build_dispatch_graph(self.saver)
        self.auxiliary_graph=build_auxiliary_graph(self.saver)
        self.runners={}
        self.aux_cancels={}
        self.closing_runs={}
        self.lock=threading.Lock()
        self.channel=None
        atexit.register(self.close)

    def get_channel(self):
        with self.lock:
            if self.channel is None:
                self.channel=CommandChannel(self)
            return self.channel

    def start(self,run_id):
        if not self.autostart:
            return
        with self.lock:
            runner=self.runners.setdefault(run_id,TeamRunner(self,run_id))
        runner.start()

    def notify(self,run_id):
        with self.lock:
            runner=self.runners.get(run_id)
        if runner:
            runner.wake.set()

    def notify_activation(self,activation_id):
        op=self.repository.operation(activation_id)
        with self.lock:
            runner=self.runners.get(op['conversation_id'])
        if runner:
            snapshot=self.repository.snapshot(op['conversation_id'])
            if snapshot:
                runner.cancel_stale(snapshot)
        self.notify(op['conversation_id'])

    def list_roles(self): return self.repository.list_definitions('role')
    def create_role(self,payload):
        role=validate_role(payload)
        if role['model_config_id'] not in settings.TEAM_MODEL_CONFIGS:
            raise ValueError('模型配置未在服务器注册')
        return self.repository.save_definition('role',role)
    def get_role(self,role_id,version=None): return self.repository.get_definition('role',role_id,version)
    def update_role(self,role_id,payload):
        role=validate_role(payload)
        if role['model_config_id'] not in settings.TEAM_MODEL_CONFIGS:
            raise ValueError('模型配置未在服务器注册')
        return self.repository.save_definition('role',role,role_id,payload.get('base_version'))
    def archive_role(self,role_id): return self.repository.archive_role(role_id)
    def list_role_versions(self,role_id): return self.repository.list_versions('role',role_id)
    def list_teams(self): return self.repository.list_definitions('team')
    def get_team(self,team_id,version=None): return self.repository.get_definition('team',team_id,version)
    def list_team_versions(self,team_id): return self.repository.list_versions('team',team_id)

    def _definition(self,payload):
        roles={r['id']:r for r in self.list_roles()}
        definition=validate_team_definition(payload,roles)
        for node in definition['nodes']:
            if not self.get_role(node['role_id'],node['role_version']):
                raise ValueError('引用的角色版本不存在')
        return definition

    def create_team(self,payload): return self.repository.save_definition('team',self._definition(payload))
    def update_team(self,team_id,payload): return self.repository.save_definition('team',self._definition(payload),team_id,payload.get('base_version'))

    def create_preset_role(self,key):
        from app.domain.team_presets import get_role_preset
        from app.domain.team_tools import available_team_tools
        tools=[tool['name'] for tool in available_team_tools()]
        role=validate_role(get_role_preset(key,tools)['role'])
        if role['model_config_id'] not in settings.TEAM_MODEL_CONFIGS:
            raise ValueError('模型配置未在服务器注册')
        return self.repository.get_or_create_preset_role(role)

    def preview_preset_team(self,key):
        from app.domain.team_presets import get_role_preset,get_team_preset
        from app.domain.team_tools import available_team_tools
        preset=get_team_preset(key)
        tools=[tool['name'] for tool in available_team_tools()]
        definition=preset['team']
        keys=list(dict.fromkeys(node['role_id'] for node in definition['nodes']))
        roles={key:get_role_preset(key,tools)['role'] for key in keys}
        definition=validate_team_definition(definition,{key:{'id':key,**role} for key,role in roles.items()})
        return {'format':'kds-team-v1','schema_version':1,'definition':definition,
                'roles':[dict(role,id=key,version=1,preset_role_key=key) for key,role in roles.items()],
                'preset_key':key}

    def create_preset_team(self,key,request_id=None):
        preview=self.preview_preset_team(key)
        roles={role['preset_role_key']:validate_role(role) for role in preview['roles']}
        for role in roles.values():
            if role['model_config_id'] not in settings.TEAM_MODEL_CONFIGS:
                raise ValueError('模型配置未在服务器注册')
        return self.repository.save_preset_team(preview['definition'],roles,request_id)

    def preview_team(self,team_id,version=None):
        exported=self.export_team(team_id,version)
        if exported is None:
            return None
        return {**exported,'head_version':self.get_team(team_id)['version']}

    def preview_run(self,run_id):
        return self.repository.preview_run(run_id)

    def export_team(self,team_id,version=None):
        definition=self.get_team(team_id,version)
        if not definition:
            return None
        roles={(n['role_id'],n['role_version']):self.get_role(n['role_id'],n['role_version']) for n in definition['nodes']}
        return {'format':'kds-team-v1','schema_version':1,'definition':definition,'roles':list(roles.values())}

    def import_team(self,payload):
        config_id=payload.get('legacy_config_id',payload.get('config_id'))
        if config_id:
            from app import db
            config=db.get_config(config_id)
            if not config:
                raise KeyError('旧配置不存在')
            roles=[self.create_role(a) for a in config.get('agents',[])]
            nodes=[{'id':uid('node'),'name':r['name'],'role_id':r['id'],'role_version':r['version'],
                    'position':{'x':index*260,'y':100}} for index,r in enumerate(roles)]
            edges=[{'id':uid('edge'),'type':'room','source':a['id'],'target':b['id']} for a,b in zip(nodes,nodes[1:])]
            return self.create_team({'name':config['name'],'nodes':nodes,'edges':edges,'shared_background':config.get('shared_background','')})
        definition=dict(payload.get('definition',payload))
        imported_roles=payload.get('roles',[])
        if not isinstance(imported_roles,list):
            raise ValueError('导入角色必须是数组')
        if imported_roles:
            preview_roles={role.get('id'):{**validate_role(role),'id':role.get('id')} for role in imported_roles}
            validate_team_definition(definition,preview_roles)
            versions={(r.get('id'),r.get('version',1)) for r in imported_roles}
            if any((node['role_id'],node.get('role_version',1)) not in versions for node in definition['nodes']):
                raise ValueError('导入缺少引用的角色版本')
            if any(r['model_config_id'] not in settings.TEAM_MODEL_CONFIGS for r in preview_roles.values()):
                raise ValueError('导入的模型配置未在服务器注册')
        imported={}
        for role in imported_roles:
            original=(role.get('id'),role.get('version',1))
            imported[original]=self.create_role(role)
        if imported:
            definition['nodes']=[dict(n,role_id=imported[(n['role_id'],n.get('role_version',1))]['id'],role_version=1)
                                 for n in definition['nodes']]
        return self.create_team(definition)

    def create_run(self,payload):
        definition=self.get_team(payload.get('team_id'),payload.get('team_version'))
        if not definition:
            raise KeyError('团队版本不存在')
        clean=dict(payload)
        clean['entry_node_ids']=validate_entry_nodes(definition,payload.get('entry_node_ids'))
        if not isinstance(payload.get('goal'),str) or not payload['goal'].strip():
            raise ValueError('运行目标不能为空')
        clean['limits']=normalize_limits(payload.get('limits',definition.get('limits')))
        if len(definition['nodes'])>clean['limits']['max_instances']:
            raise ValueError('静态节点数量已超过实例上限')
        roles={(n['role_id'],n['role_version']):self.get_role(n['role_id'],n['role_version']) for n in definition['nodes']}
        run_id=self.repository.create_run(clean,definition,roles)
        if self.repository.snapshot(run_id) is None:
            raise TeamConflict('该启动请求对应的运行已删除，请使用新的 request_id')
        self.start(run_id)
        return self.get_snapshot(run_id)

    def get_snapshot(self,run_id):
        snapshot=self.repository.snapshot(run_id)
        if snapshot:
            with self.lock:
                runner=self.runners.get(run_id)
            if runner and getattr(runner,'error',None):
                snapshot['error']=runner.error
        return snapshot
    def list_runs(self):
        return [{k:r.get(k) for k in ('id','team_id','team_version','goal','status','created_at','paused_reason')}
                for r in self.repository.list_runs()]
    def list_agents(self,run_id): return self.repository.list_entities(run_id,'agent_instances')
    def list_tasks(self,run_id): return self.repository.list_entities(run_id,'agent_tasks')
    def list_rooms(self,run_id): return self.repository.list_entities(run_id,'team_rooms')
    def list_events(self,run_id,after=0,limit=200): return self.repository.events(run_id,after,limit)
    def list_messages(self,run_id,**kwargs): return self.repository.messages(run_id,**kwargs)
    def list_tool_logs(self,run_id,instance_id=None,task_id=None):
        logs=self.repository.list_entities(run_id,'team_tool_logs')
        return [log for log in logs if (not instance_id or log['instance_id']==instance_id) and (not task_id or log['task_id']==task_id)]

    def send_human_message(self,run_id,payload):
        result=self.repository.human_message(run_id,payload)
        self.notify(run_id)
        return result

    def pause(self,run_id):
        result=self.repository.pause(run_id)
        with self.lock:
            runner=self.runners.get(run_id)
        if runner:
            runner.stop()
        with self.lock:
            cancels=[record for record in self.aux_cancels.values() if record[0]==run_id]
        for _,cancel,_ in cancels:
            cancel.set()
        return result

    def resume(self,run_id,retry_uncertain=False):
        with self.lock:
            runner=self.runners.get(run_id)
        if runner and not runner.join():
            raise TeamConflict('请等待当前工具收尾后继续')
        result=self.repository.resume(run_id,retry_uncertain)
        self.start(run_id)
        return result

    def update_limits(self,run_id,payload):
        current=self.get_snapshot(run_id)
        if not current:
            return None
        result=self.repository.update_limits(run_id,normalize_limits({**current['limits'],**payload.get('limits',payload)}))
        self.notify(run_id)
        return result

    def save_role(self,run_id,instance_id,payload):
        instance=next((i for i in self.list_agents(run_id) if i['id']==instance_id),None)
        if not instance:
            return None
        if not instance['role'].get('temporary'):
            raise ValueError('该实例没有待保存的临时角色')
        role=dict(instance['role'])
        role['name']=payload.get('name') or role['name']
        role['description']='来源：'+run_id+'/'+instance_id
        return self.create_role(role)

    def _invoke_auxiliary(self,op_id):
        initial=self.repository.auxiliary_context(op_id)
        cancel,done=threading.Event(),threading.Event()
        with self.lock:
            if op_id in self.aux_cancels:
                raise TeamConflict('该辅助操作正在执行，请读取已有结果')
            if initial['conversation_id'] in self.closing_runs and initial['purpose']!='summary':
                raise TeamConflict('运行正在完成或删除，请等待当前控制操作结束')
            self.aux_cancels[op_id]=(initial['conversation_id'],cancel,done)
        def execute(operation_id):
            receipt=self.repository.start_auxiliary(operation_id)
            if receipt['result'] is not None:
                return
            context=self.repository.auxiliary_context(operation_id)
            context['attempt_id']=receipt['attempt_id']
            # System transformations usually have small output grants. DeepSeek
            # Messages defaults to high thinking, which can consume the entire
            # grant before a summary/JSON answer. Use the same DSH runtime with
            # thinking off unless the deployment explicitly selected an effort.
            runtime_settings=getattr(self.executor,'settings',None)
            context['model_config']={**settings.TEAM_MODEL_CONFIGS.get('default',{}),**context.get('model_config',{})}
            configured_effort=(context['model_config'].get('reasoning_effort') or
                               getattr(runtime_settings,'reasoning_effort',None) or settings.DSH_REASONING_EFFORT)
            auxiliary_model=(context['model_config'].get('model') or getattr(runtime_settings,'model',None) or settings.DSH_MODEL)
            if configured_effort is not None:
                context['model_config']['reasoning_effort']=configured_effort
            elif auxiliary_model in {'deepseek-v4-flash','deepseek-v4-pro'}:
                context['model_config']['reasoning_effort']='off'
            prompts={'summary':'请总结团队目标、正式交付、未完成事项和主要结论，用中文正文输出。',
                     'assist':'请给出团队配置建议，输出 JSON {"reply":"建议","proposal":null}。',
                     'score':'请评价当前讨论参与意愿，输出 JSON {"score":0到100的数值}。',
                     'vote':'根据 arguments 中的题目和选项投票，输出 JSON {"choice":从0开始的选项序号}。'}
            context['prompt']=prompts[context['purpose']]
            if context['purpose']=='summary':
                context['response_format']='text'
                context['prompt']+=' 只保留关键结论、结果与未完成事项，简短直接，不复述输入记录。'
            with self.request_slots:
                if cancel.is_set():
                    raise TeamConflict('辅助调用已暂停')
                result=self.executor.execute_auxiliary(context['purpose'],context,
                    usage_cb=lambda event:self.repository.record_usage(operation_id,receipt['attempt_id'],event),
                    activity_cb=lambda event:self.repository.record_activity(operation_id,event),
                    cancel_event=cancel)
            if isinstance(result,dict) and 'result' in result:
                result=result['result']
            if context['purpose']=='summary' and isinstance(result,str):
                result={'summary':result}
            self.repository.save_result(operation_id,receipt['attempt_id'],result)
        try:
            config={'configurable':{'thread_id':f'conversation:{initial["conversation_id"]}:system:{op_id}'}}
            checkpoint=self.auxiliary_graph.get_state(config)
            self.auxiliary_graph.invoke(None if checkpoint.next else {'operation_id':op_id,'outcome':''},config,
                                       context=AuxiliaryContext(execute,self.repository.commit_auxiliary),durability='sync')
            return self.repository.operation(op_id)['result']
        except Exception as error:
            self.repository.commit_auxiliary(op_id,error)
            raise
        finally:
            done.set()
            with self.lock:
                self.aux_cancels.pop(op_id,None)

    def run_auxiliary(self,run_id,payload):
        with self.lock:
            if run_id in self.closing_runs:
                raise TeamConflict('运行正在完成或删除')
        op_id=self.repository.prepare_auxiliary(run_id,payload.get('kind'),payload.get('arguments',{}),payload.get('request_id'))
        try:
            result=self._invoke_auxiliary(op_id)
        finally:
            self.notify(run_id)
        return {'operation_id':op_id,'result':result}

    def finalize(self,run_id,payload):
        with self.lock:
            if run_id in self.closing_runs:
                raise TeamConflict('运行的控制操作正在收尾')
            self.closing_runs[run_id]='finalizing'
        try:
            return self._finalize(run_id,payload)
        finally:
            with self.lock:
                self.closing_runs.pop(run_id,None)

    def _finalize(self,run_id,payload):
        snapshot=self.get_snapshot(run_id)
        if not snapshot:
            return None
        if snapshot['status']=='completed':
            return snapshot
        summarize=payload.get('summarize',True)
        if not isinstance(summarize,bool):
            raise ValueError('summarize 必须为布尔值')
        self.pause(run_id)
        with self.lock:
            runner=self.runners.get(run_id)
        if runner and not runner.join():
            raise TeamConflict('请等待当前工具收尾后完成')
        with self.lock:
            auxiliaries=[entry for entry in self.aux_cancels.values() if entry[0]==run_id]
        for _,_,done in auxiliaries:
            if not done.wait(5):
                raise TeamConflict('请等待系统辅助调用收尾后完成')
        op_id=self.repository.prepare_finalize(run_id,summarize)
        if op_id:
            try:
                self._invoke_auxiliary(op_id)
            except Exception as error:
                self.repository.commit_auxiliary(op_id,error)
        return self.get_snapshot(run_id)

    def delete(self,run_id):
        with self.lock:
            if run_id in self.closing_runs:
                raise TeamConflict('运行的控制操作正在收尾')
            self.closing_runs[run_id]='deleting'
        try:
            return self._delete(run_id)
        finally:
            with self.lock:
                self.closing_runs.pop(run_id,None)

    def _delete(self,run_id):
        self.pause(run_id)
        with self.lock:
            runner=self.runners.pop(run_id,None)
        if runner:
            if not runner.join():
                with self.lock:
                    self.runners[run_id]=runner
                raise TeamConflict('请等待当前工具收尾后删除')
        with self.lock:
            auxiliaries=[entry for entry in self.aux_cancels.values() if entry[0]==run_id]
        for _,cancel,done in auxiliaries:
            cancel.set()
            if not done.wait(5):
                raise TeamConflict('请等待系统辅助收尾后删除')
        deleted=self.repository.delete(run_id)
        delete_conversation(run_id,saver=self.saver)
        return deleted

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed=True
            runners=list(self.runners.values())
            auxiliaries=list(self.aux_cancels.values())
            channel,self.channel=self.channel,None
        for _,cancel,_ in auxiliaries:
            cancel.set()
        for runner in runners:
            runner.stop()
        for runner in runners:
            runner.join(2)
        self.executor.close()
        if channel:
            channel.close()
        if self.owner:
            self.owner.close()


class _LazyTeamService:
    def __init__(self):
        self.instance=None
        self.lock=threading.Lock()

    def __getattr__(self,name):
        with self.lock:
            if self.instance is None:
                self.instance=TeamSessionService()
            return getattr(self.instance,name)


team_service=_LazyTeamService()
