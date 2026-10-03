"""Short SQLite transactions for team entities, commands and activation receipts.

No model calls, resource waits or complete conversation snapshots occur here.
"""
import copy
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app import db
from app.config import DB_PATH
from app.tool_logs import is_tool_log, merge_tool_log

TERMINAL = {'succeeded', 'failed', 'cancelled'}
ACTIVE = {'prepared', 'running', 'result_ready'}


def uid(prefix):
    return prefix + '_' + uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def unpack(row):
    if row is None:
        return None
    result = dict(row)
    payload = json.loads(result.pop('payload', '{}'))
    return {**payload, **result}


class TeamConflict(ValueError):
    status_code = 409


class TeamRepository:
    graph_version = 'team-v1'

    def __init__(self, db_path=None):
        self.db_path = Path(db_path or DB_PATH)
        db.init_db(self.db_path)

    @contextmanager
    def transaction(self):
        with db._lock:
            conn = db._connect(self.db_path)
            try:
                conn.execute('BEGIN IMMEDIATE')
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def reading(self):
        conn = db._connect(self.db_path)
        try:
            conn.execute('BEGIN')
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def _get(self, conn, table, entity_id, run_id=None):
        query, args = f'SELECT * FROM {table} WHERE id=?', [entity_id]
        if run_id is not None:
            query += ' AND run_id=?'
            args.append(run_id)
        return unpack(conn.execute(query, args).fetchone())

    def _session(self, conn, run_id):
        run = self._get(conn, 'team_sessions', run_id)
        if run is None:
            raise KeyError('团队运行不存在')
        return run

    def _event(self, conn, run_id, event_type, **payload):
        conn.execute('UPDATE team_sessions SET event_seq=event_seq+1 WHERE id=?', (run_id,))
        row = conn.execute('SELECT event_seq FROM team_sessions WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise TeamConflict('团队已删除')
        seq = row[0]
        conn.execute('INSERT INTO team_events VALUES (?,?,?,?,?)',
                     (run_id, seq, event_type, encode(payload), db._now()))
        return seq

    def _save_session(self, conn, run):
        fields = {k: v for k, v in run.items() if k not in {'id','status','epoch','event_seq','created_at'}}
        conn.execute('UPDATE team_sessions SET status=?,epoch=?,payload=? WHERE id=?',
                     (run['status'], run['epoch'], encode(fields), run['id']))
        summary = {'kind': 'team', 'orchestration_backend': 'langgraph',
                   'graph_version': self.graph_version, 'team_id': run['team_id'],
                   'team_version': run['team_version'], 'paused_reason': run.get('paused_reason'),
                   'goal': run['goal'], 'runner_epoch': run['epoch']}
        conn.execute('UPDATE conversations SET status=?,payload=?,state_rev=state_rev+1,updated_at=? WHERE id=?',
                     (run['status'], encode(summary), db._now(), run['id']))

    def _active_elapsed(self, run):
        return run.get('active_seconds',0) + (max(0,time.time()-run['segment_start']) if run.get('segment_start') else 0)

    def _touch_progress(self, conn, run):
        if run['status']=='running' and run.get('segment_start'):
            now=time.time()
            run['active_seconds']=run.get('active_seconds',0)+max(0,now-run['segment_start'])
            run['segment_start']=now
            run['progress_saved_at']=now
            self._save_session(conn,run)

    def _ancestors(self, conn, task):
        while task:
            yield task
            task=self._get(conn,'agent_tasks',task['parent_task_id']) if task['parent_task_id'] else None

    def _subtree_stat(self, conn, task_id, expression, condition='1', parameters=()):
        query=('WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL '
               'SELECT t.id FROM agent_tasks t JOIN tree ON t.parent_task_id=tree.id) '
               f'SELECT {expression} FROM agent_tasks WHERE id IN (SELECT id FROM tree) AND {condition}')
        return conn.execute(query,(task_id,*parameters)).fetchone()[0]

    def _save_task(self, conn, task):
        keys = {'id','run_id','instance_id','parent_task_id','status','created_at'}
        conn.execute('UPDATE agent_tasks SET status=?,payload=? WHERE id=?',
                     (task['status'], encode({k:v for k,v in task.items() if k not in keys}), task['id']))

    def _save_instance(self, conn, instance):
        keys = {'id','run_id','parent_id','status','epoch','current_task_id'}
        conn.execute('UPDATE agent_instances SET parent_id=?,status=?,epoch=?,current_task_id=?,payload=? WHERE id=?',
                     (instance.get('parent_id'), instance['status'], instance['epoch'], instance.get('current_task_id'),
                      encode({k:v for k,v in instance.items() if k not in keys}), instance['id']))

    def _receipt(self, conn, scope, request_id, arguments):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError('必须提供稳定的 request_id（最多200字符）')
        row = conn.execute('SELECT * FROM team_receipts WHERE scope=? AND request_id=?',
                           (scope, request_id)).fetchone()
        if row:
            if row['arguments'] != encode(arguments):
                raise TeamConflict('相同 request_id 的参数不一致')
            return json.loads(row['result'])
        return None

    def _record_receipt(self, conn, scope, request_id, arguments, result):
        conn.execute('INSERT INTO team_receipts VALUES (?,?,?,?,?)',
                     (scope, request_id, encode(arguments), encode(result), db._now()))

    def list_versions(self, kind, entity_id):
        table, key = ('role_versions','role_id') if kind == 'role' else ('team_definition_versions','team_id')
        with self.reading() as conn:
            return [self._definition_record(kind,dict(json.loads(r['payload']), id=entity_id, version=r['version'])) for r in
                    conn.execute(f'SELECT * FROM {table} WHERE {key}=? ORDER BY version', (entity_id,))]

    @staticmethod
    def _definition_record(kind, record):
        if kind=='role' and record is not None:
            from app.domain.teams import role_equivalence_key
            record['equivalence_key']=role_equivalence_key(record)
        return record

    def _read_definition(self, conn, kind, entity_id, version=None):
        table, versions, key = (('role_templates','role_versions','role_id') if kind == 'role'
                               else ('team_definitions','team_definition_versions','team_id'))
        head = conn.execute(f'SELECT * FROM {table} WHERE id=?', (entity_id,)).fetchone()
        if not head:
            return None
        row = conn.execute(f'SELECT * FROM {versions} WHERE {key}=? AND version=?',
                           (entity_id, head['version'] if version is None else int(version))).fetchone()
        if row is None:
            return None
        metadata = {k:v for k,v in dict(head).items() if k not in {'name','version'}}
        return self._definition_record(kind,{**json.loads(row['payload']), **metadata, 'version': row['version']})

    def get_definition(self, kind, entity_id, version=None):
        with self.reading() as conn:
            return self._read_definition(conn,kind,entity_id,version)

    def list_definitions(self, kind):
        table, versions, key = (('role_templates','role_versions','role_id') if kind == 'role'
                               else ('team_definitions','team_definition_versions','team_id'))
        with self.reading() as conn:
            return [self._definition_record(kind,{**json.loads(row['payload']), **{k:v for k,v in dict(row).items() if k!='payload'}})
                    for row in conn.execute(f'SELECT d.*,v.payload FROM {table} d JOIN {versions} v '
                                            f'ON d.id=v.{key} AND d.version=v.version ORDER BY d.updated_at DESC')]

    def _write_definition(self, conn, kind, payload, entity_id=None, base_version=None):
        table, versions, key = (('role_templates','role_versions','role_id') if kind == 'role'
                               else ('team_definitions','team_definition_versions','team_id'))
        now = db._now()
        if entity_id:
            head = conn.execute(f'SELECT * FROM {table} WHERE id=?', (entity_id,)).fetchone()
            if not head:
                raise KeyError('定义不存在')
            if base_version is None or int(base_version) != head['version']:
                raise TeamConflict('配置版本已变化，请重新加载后保存')
            version = head['version'] + 1
            conn.execute(f'UPDATE {table} SET version=?,name=?,updated_at=? WHERE id=?',
                         (version, payload['name'], now, entity_id))
        else:
            entity_id, version = uid(kind), 1
            cols = 'id,name,version,created_at,updated_at'
            conn.execute(f'INSERT INTO {table} ({cols}) VALUES (?,?,?,?,?)',
                         (entity_id, payload['name'], version, now, now))
        conn.execute(f'INSERT INTO {versions} VALUES (?,?,?,?)',
                     (entity_id, version, encode(payload), now))
        return self._read_definition(conn,kind,entity_id,version)

    def save_definition(self, kind, payload, entity_id=None, base_version=None):
        with self.transaction() as conn:
            return self._write_definition(conn,kind,payload,entity_id,base_version)

    def _reusable_roles(self, conn):
        from app.domain.teams import role_equivalence_key
        roles={}
        for row in conn.execute('SELECT d.id,v.payload FROM role_templates d JOIN role_versions v '
                                'ON d.id=v.role_id AND d.version=v.version WHERE d.archived=0 ORDER BY d.created_at,d.id'):
            roles.setdefault(role_equivalence_key(json.loads(row['payload'])),row['id'])
        return roles

    def get_or_create_preset_role(self, payload):
        from app.domain.teams import role_equivalence_key
        with self.transaction() as conn:
            role_id=self._reusable_roles(conn).get(role_equivalence_key(payload))
            return self._read_definition(conn,'role',role_id) if role_id else self._write_definition(conn,'role',payload)

    def save_preset_team(self, definition, roles, request_id=None):
        """Atomically reuse role heads and store an independent team definition."""
        from app.domain.teams import role_equivalence_key,validate_team_definition
        arguments={'definition':definition,'roles':roles}
        with self.transaction() as conn:
            if request_id is not None:
                old=self._receipt(conn,'create_preset_team',request_id,arguments)
                if old:
                    return old
            available=self._reusable_roles(conn)
            saved={}
            for key,role in roles.items():
                fingerprint=role_equivalence_key(role)
                role_id=available.get(fingerprint)
                saved[key]=self._read_definition(conn,'role',role_id) if role_id else self._write_definition(conn,'role',role)
                available[fingerprint]=saved[key]['id']
            payload=copy.deepcopy(definition)
            for node in payload['nodes']:
                role=saved[node['role_id']]
                node.update(role_id=role['id'],role_version=role['version'])
            payload=validate_team_definition(payload,{role['id']:role for role in saved.values()})
            result=self._write_definition(conn,'team',payload)
            if request_id is not None:
                self._record_receipt(conn,'create_preset_team',request_id,arguments,result)
            return result

    def preview_run(self, run_id):
        """Read the run's frozen topology and exact role versions without recovery."""
        with self.reading() as conn:
            run=self._get(conn,'team_sessions',run_id)
            if run is None:
                return None
            definition=run['definition']
            refs=list(dict.fromkeys((node['role_id'],node['role_version']) for node in definition['nodes']))
            roles=[self._read_definition(conn,'role',role_id,version) for role_id,version in refs]
            if any(role is None for role in roles):
                raise KeyError('冻结配置引用的角色版本不存在')
            head=conn.execute('SELECT version FROM team_definitions WHERE id=?',(run['team_id'],)).fetchone()
            return {'format':'kds-team-v1','schema_version':1,'definition':definition,'roles':roles,
                    'run_id':run_id,'team_id':run['team_id'],'team_version':run['team_version'],
                    'head_version':head['version'] if head else None}

    def archive_role(self, role_id):
        with self.transaction() as conn:
            conn.execute('UPDATE role_templates SET archived=1,updated_at=? WHERE id=?', (db._now(),role_id))
        return self.get_definition('role',role_id)

    def _new_instance(self, conn, run_id, role, name, node_id=None, parent_id=None, **extra):
        instance_id = uid('agent')
        payload = {'name': name, 'node_id': node_id, 'role': role, 'dynamic': node_id is None,
                   'creator_instance_id': parent_id, 'parent_instance_id': parent_id,
                   'harness_state': {}, **extra}
        conn.execute('INSERT INTO agent_instances VALUES (?,?,?,?,?,?,?)',
                     (instance_id,run_id,parent_id,'idle',1,None,encode(payload)))
        return instance_id

    def _new_task(self, conn, run, instance_id, goal, parent_task_id=None, **extra):
        limits = run['limits']
        count = conn.execute('SELECT count(*) FROM agent_tasks WHERE run_id=?', (run['id'],)).fetchone()[0]
        queued = conn.execute("SELECT count(*) FROM agent_tasks WHERE run_id=? AND status='queued'", (run['id'],)).fetchone()[0]
        if count >= limits['max_tasks'] or queued >= limits['max_queue_length']:
            raise TeamConflict('任务数或队列达到上限')
        ancestor=self._get(conn,'agent_tasks',parent_task_id,run['id']) if parent_task_id else None
        if parent_task_id and (not ancestor or ancestor['status'] in TERMINAL):
            raise TeamConflict('父任务不存在或已结束')
        for ancestor in self._ancestors(conn,ancestor):
            cap=ancestor.get('budget',{})
            if cap.get('max_tasks') is not None and self._subtree_stat(conn,ancestor['id'],'count(*)')>=cap['max_tasks']:
                raise TeamConflict('父任务子树任务数达到上限')
            if cap.get('max_queue_length') is not None and self._subtree_stat(conn,ancestor['id'],'count(*)',"status='queued'")>=cap['max_queue_length']:
                raise TeamConflict('父任务子树队列达到上限')
            if cap.get('max_instances') is not None:
                existing=conn.execute('WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT t.id FROM agent_tasks t JOIN tree ON t.parent_task_id=tree.id) '
                                      'SELECT DISTINCT instance_id FROM agent_tasks WHERE id IN (SELECT id FROM tree)',(ancestor['id'],)).fetchall()
                instance_ids={row[0] for row in existing}
                if len(instance_ids | {instance_id})>cap['max_instances']:
                    raise TeamConflict('父任务子树实例数达到上限')
        task_id = uid('task')
        instance = self._get(conn,'agent_instances',instance_id,run['id'])
        if not instance:
            raise ValueError('受托实例不存在')
        from app.domain.teams import normalize_budget
        budget = normalize_budget(extra.pop('budget',{}))
        defaults = normalize_budget(instance['role'].get('default_budget',{}))
        if defaults:
            # Validate stored/imported defaults, then cap each granted amount.
            for key,value in defaults.items():
                ceiling = budget.get(key)
                budget[key] = ceiling if value is None else (value if ceiling is None else min(ceiling,value))
        payload = {'goal': goal, 'input': extra.pop('input', {}), 'acceptance': extra.pop('acceptance', ''),
                   'activation_count': 0, 'depth': 0, 'budget': budget, 'kind': 'task',
                   'created_active_seconds':self._active_elapsed(run), **extra}
        conn.execute('INSERT INTO agent_tasks VALUES (?,?,?,?,?,?,?)',
                     (task_id,run['id'],instance_id,parent_task_id,'queued',encode(payload),db._now()))
        self._event(conn, run['id'], 'task_queued', task_id=task_id,instance_id=instance_id,parent_task_id=parent_task_id)
        return task_id

    def create_run(self, payload, definition, roles):
        request_id = payload.get('request_id')
        with self.transaction() as conn:
            existing = self._receipt(conn,'create_run',request_id,payload)
            if existing:
                return existing['id']
            run_id = uid('run')
            now = db._now()
            limits = payload['limits']
            run = {'id':run_id, 'status':'running', 'epoch':1, 'team_id':definition['id'],
                   'team_version':definition['version'], 'definition':copy.deepcopy(definition),
                   'goal':payload['goal'], 'shared_background':payload.get('shared_background',definition.get('shared_background','')),
                   'limits':limits, 'active_seconds':0, 'segment_start':time.time(),
                   'graph_version':self.graph_version, 'whiteboard_rev':0,
                   'summary':None, 'summary_error':None, 'paused_reason':None}
            conn.execute('INSERT INTO team_sessions(id,status,epoch,payload,created_at) VALUES (?,?,?,?,?)',
                         (run_id,'running',1,encode({k:v for k,v in run.items() if k not in {'id','status','epoch'}}),now))
            conn.execute('INSERT INTO conversations(id,config_id,name,payload,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                         (run_id,definition['id'],payload.get('name') or definition['name'],
                          encode({'kind':'team','orchestration_backend':'langgraph'}),'running',now,now))
            node_map = {}
            for node in definition['nodes']:
                role = copy.deepcopy(roles[(node['role_id'],node['role_version'])])
                role['system_prompt'] += '\n' + node.get('prompt_supplement','')
                node_map[node['id']] = self._new_instance(conn,run_id,role,node['name'],node_id=node['id'],
                                                          x=node.get('position',{}).get('x',0),y=node.get('position',{}).get('y',0))
            for edge in definition['edges']:
                if edge['type'] == 'task':
                    parent_id = node_map[edge['source']]
                    child = self._get(conn,'agent_instances',node_map[edge['target']])
                    child.update(parent_id=parent_id,parent_instance_id=parent_id)
                    self._save_instance(conn,child)
            for component in definition['rooms']:
                room_id = uid('room')
                members = [{'instance_id':node_map[n], 'joined_seq':0} for n in component['node_ids']]
                conn.execute('INSERT INTO team_rooms VALUES (?,?,?)',
                             (room_id,run_id,encode({'name':'群聊 '+str(len(members))+' 人','members':members})))
            for node_id in payload['entry_node_ids']:
                # The session ledger remains live when a human extends limits.
                # Only explicit role/request grants belong in a task's budget.
                self._new_task(conn,run,node_map[node_id],payload['goal'])
            conn.execute('INSERT INTO team_artifacts VALUES (?,?,?,?,?)',(run_id,0,'',None,now))
            self._save_session(conn,run)
            self._event(conn,run_id,'run_created',team_id=definition['id'],team_version=definition['version'])
            self._record_receipt(conn,'create_run',request_id,payload,{'id':run_id})
            return run_id

    def list_runs(self):
        with self.reading() as conn:
            return [unpack(r) for r in conn.execute('SELECT * FROM team_sessions ORDER BY created_at DESC')]

    def list_entities(self, run_id, table):
        allowed = {'agent_instances','agent_tasks','team_rooms','team_tool_logs'}
        if table not in allowed:
            raise ValueError('未知实体')
        with self.reading() as conn:
            self._session(conn,run_id)
            rows = [unpack(r) for r in conn.execute(f'SELECT * FROM {table} WHERE run_id=?', (run_id,))]
            return [row for row in rows if is_tool_log(row)] if table=='team_tool_logs' else rows

    def snapshot(self, run_id):
        with self.reading() as conn:
            row = self._get(conn,'team_sessions',run_id)
            if not row:
                return None
            row['agents'] = [unpack(r) for r in conn.execute('SELECT * FROM agent_instances WHERE run_id=?',(run_id,))]
            row['tasks'] = [unpack(r) for r in conn.execute('SELECT * FROM agent_tasks WHERE run_id=? ORDER BY rowid',(run_id,))]
            row['rooms'] = [unpack(r) for r in conn.execute('SELECT * FROM team_rooms WHERE run_id=?',(run_id,))]
            row['tool_logs'] = [log for r in conn.execute('SELECT * FROM team_tool_logs WHERE run_id=? ORDER BY rowid',(run_id,))
                                if is_tool_log(log:=unpack(r))]
            row['messages'] = [self._observe_message(conn,r) for r in reversed(conn.execute('SELECT * FROM team_messages WHERE run_id=? ORDER BY seq DESC LIMIT 100',(run_id,)).fetchall())]
            row['usage'] = self._usage(conn,run_id)
            row['usage']['reserved_tokens'] = self._reserved(conn,run_id)
            artifact = conn.execute('SELECT * FROM team_artifacts WHERE run_id=? ORDER BY rev DESC LIMIT 1',(run_id,)).fetchone()
            row['whiteboard'] = dict(artifact) if artifact else {'rev':0,'content':''}
            row['artifacts'] = [{'rev':r['rev'],'editor_id':r['editor_id'],'created_at':r['created_at']} for r in
                                conn.execute('SELECT * FROM team_artifacts WHERE run_id=? ORDER BY rev DESC LIMIT 50',(run_id,))]
            row['elapsed_seconds'] = row.get('active_seconds',0) + (max(0,time.time()-row['segment_start']) if row.get('segment_start') else 0)
            row['can_resume'] = row['status']=='paused' and not self._limit_reached(conn,row)
            return row

    def events(self, run_id, after=0, limit=200):
        with self.reading() as conn:
            run = self._session(conn,run_id)
            rows = conn.execute('SELECT * FROM team_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',
                                (run_id,int(after),min(500,max(1,int(limit))))).fetchall()
            events = [{**json.loads(r['payload']), **{k:v for k,v in dict(r).items() if k!='payload'}} for r in rows]
            return {'events':events,'event_seq':run['event_seq'],'next_after':events[-1]['seq'] if events else int(after)}

    def messages(self, run_id, instance_id=None, room_id=None, task_id=None, before=None, limit=100, after=None):
        with self.reading() as conn:
            self._session(conn,run_id)
            query, args = 'SELECT m.* FROM team_messages m WHERE m.run_id=?', [run_id]
            if instance_id:
                query += ' AND (m.instance_id=? OR EXISTS (SELECT 1 FROM mailbox_deliveries d WHERE d.message_id=m.id AND d.instance_id=?))'
                args.extend([instance_id,instance_id])
            if room_id:
                query += ' AND m.room_id=?'; args.append(room_id)
            if task_id:
                query += ' AND m.task_id=?'; args.append(task_id)
            if before is not None:
                query += ' AND m.seq<?'; args.append(int(before))
            if after is not None:
                query += ' AND m.seq>?'; args.append(int(after))
            query += ' ORDER BY m.seq DESC LIMIT ?'; args.append(min(500,max(1,int(limit))))
            return [self._observe_message(conn,r,instance_id) for r in reversed(conn.execute(query,args).fetchall())]

    def _observe_message(self, conn, row, instance_id=None):
        message=unpack(row)
        target=instance_id or message.get('target_instance_id')
        if target:
            delivery=conn.execute('SELECT d.consumed_by,o.updated_at FROM mailbox_deliveries d '
                                  'LEFT JOIN orchestration_operations o ON o.operation_id=d.consumed_by '
                                  'WHERE d.message_id=? AND d.instance_id=?',(message['id'],target)).fetchone()
            if delivery:
                consumed=bool(delivery['consumed_by'])
                message.update(delivery_status='consumed' if consumed else 'pending',queued=not consumed,
                               consumed_at=delivery['updated_at'] if consumed else None)
                if message.get('kind')=='human' and message.get('target_type')=='agent':
                    message['status']='consumed' if consumed else 'queued'
        return message

    def _usage(self, conn, run_id):
        r = conn.execute('SELECT coalesce(sum(prompt_tokens),0),coalesce(sum(completion_tokens),0) FROM usage_events WHERE conversation_id=?',(run_id,)).fetchone()
        return {'prompt_tokens':r[0],'completion_tokens':r[1],'total_tokens':r[0]+r[1]}

    def _reserved(self, conn, run_id):
        return conn.execute("SELECT coalesce(sum(tokens),0) FROM budget_reservations WHERE run_id=? AND status='reserved'",(run_id,)).fetchone()[0]

    def _limit_reached(self, conn, run):
        tokens = run['limits'].get('total_max_tokens')
        duration = run['limits'].get('total_duration_seconds')
        elapsed = run.get('active_seconds',0) + (max(0,time.time()-run['segment_start']) if run.get('segment_start') else 0)
        return ((tokens is not None and self._usage(conn,run['id'])['completion_tokens'] >= tokens)
                or (duration is not None and elapsed >= duration))

    def _message(self, conn, run, content, *, instance_id=None, task_id=None, room_id=None,
                 recipients=(), kind='agent', **extra):
        message_id = uid('message')
        seq = self._event(conn,run['id'],'message',message_id=message_id,instance_id=instance_id,
                          task_id=task_id,room_id=room_id,kind=kind)
        target_instance_id=extra.get('target_instance_id')
        payload = {'content':content,'kind':kind,**extra}
        if kind=='human':
            payload.update(target_type='room' if room_id else 'agent',target_id=room_id or target_instance_id,
                           status='queued')
        conn.execute('INSERT INTO team_messages VALUES (?,?,?,?,?,?,?,?)',
                     (message_id,run['id'],seq,room_id,instance_id,task_id,encode(payload),db._now()))
        for recipient in dict.fromkeys(recipients):
            conn.execute('INSERT OR IGNORE INTO mailbox_deliveries VALUES (?,?,?,?,?,NULL)',
                         (uid('delivery'),run['id'],recipient,extra.get('recipient_task_id'),message_id))
        return {'id':message_id,'seq':seq,'status':'queued','queued':True}

    def _room(self, conn, run_id, room_id, instance_id=None):
        room = self._get(conn,'team_rooms',room_id,run_id)
        if not room:
            raise ValueError('群聊不存在')
        if instance_id and instance_id not in [m['instance_id'] for m in room['members']]:
            raise ValueError('实例不是该群聊成员')
        return room

    def _discussion(self, conn, run, room, content, participants=None, max_rounds=None, root_trigger=None):
        participants = participants or [m['instance_id'] for m in room['members']]
        if not set(participants) <= {m['instance_id'] for m in room['members']}:
            raise ValueError('讨论参与者必须属于该群聊')
        rounds = min(int(max_rounds or run['limits']['max_discussion_turns']),run['limits']['max_discussion_turns'])
        if rounds < 1:
            raise ValueError('讨论次数必须为正数')
        # A causal trigger owns one quota, even if an Agent asks for a new ID.
        if root_trigger:
            for r in conn.execute('SELECT * FROM discussions WHERE run_id=? AND room_id=?',(run['id'],room['id'])):
                existing = unpack(r)
                if existing.get('root_trigger') == root_trigger:
                    return existing['id']
        discussion_id = uid('discussion')
        payload = {'goal':content,'participants':participants,'remaining':rounds,'cursor':0,
                   'root_trigger':root_trigger or discussion_id,'active_task_id':None,
                   'max_tokens':rounds*run['limits']['single_max_tokens'] if run['limits'].get('single_max_tokens') else None}
        conn.execute('INSERT INTO discussions VALUES (?,?,?,?,?)',
                     (discussion_id,run['id'],room['id'],'pending',encode(payload)))
        self._event(conn,run['id'],'discussion_created',discussion_id=discussion_id,room_id=room['id'])
        return discussion_id

    def human_message(self, run_id, payload):
        with self.transaction() as conn:
            run = self._session(conn,run_id)
            if run['status']=='completed':
                raise TeamConflict('运行已完成，只能查看')
            content = payload.get('content')
            if not isinstance(content,str) or not content.strip() or len(content)>50000:
                raise ValueError('补充信息不能为空且不能超过50000字符')
            key = payload.get('request_id') or uid('human')
            scope = run_id+':human'
            receipt = self._receipt(conn,scope,key,payload)
            if receipt:
                return receipt
            target_type, target = payload.get('target_type','agent'),payload.get('target_id')
            if target_type=='agent':
                instance = self._get(conn,'agent_instances',target,run_id)
                if not instance:
                    raise ValueError('收件实例不存在')
                result = self._message(conn,run,content,recipients=[target],kind='human',target_instance_id=target)
            elif target_type=='room':
                room = self._room(conn,run_id,target)
                result = self._message(conn,run,content,room_id=target,
                                       recipients=[m['instance_id'] for m in room['members']],kind='human')
                result['discussion_id'] = self._discussion(conn,run,room,content)
            else:
                raise ValueError('请选择 Agent 或群聊')
            self._record_receipt(conn,scope,key,payload,result)
            return result

    def _assert_activation(self, conn, activation_id, allow_result=False):
        activation = self._get(conn,'team_activations',activation_id)
        if not activation:
            raise TeamConflict('激活不存在或已删除')
        run = self._session(conn,activation['run_id'])
        instance = self._get(conn,'agent_instances',activation['instance_id'],run['id'])
        task = self._get(conn,'agent_tasks',activation['task_id'],run['id'])
        if (not instance or not task or run['epoch'] != activation['epoch']
                or instance['epoch'] != activation['instance_epoch'] or run['status']!='running'
                or task['status'] in TERMINAL or instance['current_task_id']!=task['id']
                or activation['status'] not in (ACTIVE if allow_result else {'prepared','running'})):
            raise TeamConflict('激活执行权已失效')
        elapsed=self._active_elapsed(run)
        if any(ancestor.get('budget',{}).get('total_duration_seconds') is not None and
               elapsed-ancestor.get('created_active_seconds',0)>=ancestor['budget']['total_duration_seconds']
               for ancestor in self._ancestors(conn,task)):
            raise TeamConflict('任务运行期限已到')
        return activation,run,instance,task

    def command(self, activation_id, tool, args, role_resolver=None):
        from app.domain.teams import narrow_capabilities,validate_role
        arguments = {'tool':tool,'args':args}
        request_id = args.get('request_id')
        with self.transaction() as conn:
            activation,run,instance,task = self._assert_activation(conn,activation_id)
            scope = run['id']+':task:'+task['id']
            old = self._receipt(conn,scope,request_id,arguments)
            if old:
                return old
            def owned(task_id):
                child = self._get(conn,'agent_tasks',task_id,run['id'])
                if not child or child['parent_task_id']!=task['id']:
                    raise ValueError('只能操作当前任务直接创建的子任务')
                return child
            if tool in {'kds_delegate_task','kds_spawn_subagent'}:
                if task.get('depth',0)>=min(run['limits']['max_depth'],task.get('budget',{}).get('max_depth',run['limits']['max_depth'])):
                    raise TeamConflict('任务树深度达到上限')
                if conn.execute('SELECT count(*) FROM agent_tasks WHERE parent_task_id=?',(task['id'],)).fetchone()[0]>=min(run['limits']['max_children'],task.get('budget',{}).get('max_children',run['limits']['max_children'])):
                    raise TeamConflict('子任务数量达到上限')
                task_input = args.get('task',args)
                goal = task_input.get('goal')
                if not isinstance(goal,str) or not goal.strip():
                    raise ValueError('子任务目标不能为空')
                budget=self._delegated_budget(run,task,args.get('budget',{}))
                if tool=='kds_delegate_task':
                    child = self._get(conn,'agent_instances',args.get('child_instance_id'),run['id'])
                    if not child or child['parent_id']!=instance['id']:
                        raise ValueError('只能向直接子实例委派')
                    # Acceptance is independent of execution. A busy child keeps
                    # its current task; _new_task applies the finite queue caps,
                    # and dispatch admits its queued work in creation order only
                    # after that task finishes (including child-result waits).
                    child_id = child['id']
                else:
                    count = conn.execute('SELECT count(*) FROM agent_instances WHERE run_id=?',(run['id'],)).fetchone()[0]
                    if count>=run['limits']['max_instances']:
                        raise TeamConflict('实例数量达到上限')
                    if args.get('role_id'):
                        # Resolve in this transaction: no nested repository lock.
                        role_row = conn.execute('SELECT v.payload,v.version FROM role_versions v JOIN role_templates r ON r.id=v.role_id '
                                                'WHERE v.role_id=? AND v.version=coalesce(?,r.version)',
                                                (args['role_id'],args.get('role_version'))).fetchone()
                        if not role_row:
                            raise ValueError('角色版本不存在')
                        role = {**json.loads(role_row['payload']),'id':args['role_id'],'version':role_row['version']}
                    else:
                        role_input = dict(args.get('role') or {})
                        role_input.setdefault('tools',instance['role'].get('tools',[]))
                        role_input.setdefault('model_config_id',instance['role'].get('model_config_id','default'))
                        role = validate_role(role_input)
                        role.update(id=uid('temporary_role'),version=1,temporary=True,reason=args.get('reason',''))
                    parent_cap = {'tools':instance['role'].get('tools',[]),
                                  'model_config_id':instance['role'].get('model_config_id','default'),
                                  'budget':task.get('budget',{})}
                    caps = narrow_capabilities(parent_cap,{'tools':role.get('tools',parent_cap['tools']),
                                                           'model_config_id':role.get('model_config_id',parent_cap['model_config_id']),
                                                           'budget':budget})
                    role.update(tools=caps['tools'],model_config_id=caps['model_config_id'])
                    child_id = self._new_instance(conn,run['id'],role,args.get('name') or role['name'],parent_id=instance['id'])
                    self._event(conn,run['id'],'agent_spawned',instance_id=child_id,creator_instance_id=instance['id'],role_id=role['id'])
                    if args.get('join_room'):
                        room_id = args['join_room'] if isinstance(args['join_room'],str) else next(
                            (unpack(r)['id'] for r in conn.execute('SELECT * FROM team_rooms WHERE run_id=?',(run['id'],))
                             if instance['id'] in [m['instance_id'] for m in unpack(r)['members']]),None)
                        room = self._room(conn,run['id'],room_id,instance['id'])
                        room['members'].append({'instance_id':child_id,'joined_seq':run['event_seq']+1})
                        conn.execute('UPDATE team_rooms SET payload=? WHERE id=?',
                                     (encode({k:v for k,v in room.items() if k not in {'id','run_id'}}),room['id']))
                task_id = self._new_task(conn,run,child_id,goal,task['id'],input=task_input.get('input',{}),
                                         acceptance=task_input.get('acceptance',''),budget=budget,depth=task.get('depth',0)+1)
                result = {'task_id':task_id,'instance_id':child_id,'status':'queued'}
            elif tool=='kds_get_task_results':
                requested = args.get('task_ids',[])
                if not isinstance(requested,list) or any(not isinstance(t,str) or not t.strip() for t in requested):
                    raise ValueError('task_ids 必须是非空字符串组成的任务 ID 数组')
                task_ids = list(dict.fromkeys(requested))
                children = [owned(task_id) for task_id in task_ids]
                result = {'tasks':children}
                if any(child['status'] not in TERMINAL for child in children):
                    # This scheduling signal shares the same durable receipt as
                    # the owned query snapshot. The bridge yields the parent;
                    # graph commit records the wait before releasing its slot.
                    result['kds_control'] = {'action':'wait_children','task_ids':task_ids,'mode':'all'}
            elif tool in {'kds_cancel_task','kds_retry_task'}:
                child = owned(args.get('task_id'))
                if tool=='kds_cancel_task':
                    self._cancel_subtree(conn,run,child['id'],args.get('reason','父任务取消'))
                    result = {'task_id':child['id'],'status':'cancelled'}
                else:
                    if child['status'] not in TERMINAL:
                        raise ValueError('只能重试终态子任务')
                    result = {'task_id':self._new_task(conn,run,child['instance_id'],child['goal'],task['id'],
                                input=child.get('input',{}),acceptance=child.get('acceptance',''),
                                depth=child.get('depth',0),budget=child.get('budget',{}),retry_of_task_id=child['id'])}
            elif tool in {'kds_send_group_message','kds_request_discussion'}:
                room = self._room(conn,run['id'],args.get('room_id'),instance['id'])
                if tool=='kds_request_discussion':
                    cause = task.get('discussion_id') or task['id']
                    if task.get('discussion_id'):
                        discussion = self._get(conn,'discussions',task['discussion_id'],run['id'])
                        cause = discussion['root_trigger']
                    result = {'discussion_id':self._discussion(conn,run,room,args.get('goal','讨论'),
                                args.get('participants'),args.get('max_rounds'),root_trigger=cause)}
                else:
                    content = args.get('content')
                    if not isinstance(content,str) or not content.strip() or len(content)>50000:
                        raise ValueError('群聊消息不能为空且不能超过50000字符')
                    members = [m['instance_id'] for m in room['members']]
                    mentions = args.get('mentions') or []
                    if not set(mentions)<=set(members):
                        raise ValueError('提及对象必须属于群聊')
                    discussion_id = args.get('discussion_id') or task.get('discussion_id')
                    if discussion_id:
                        discussion = self._get(conn,'discussions',discussion_id,run['id'])
                        if not discussion or discussion['room_id']!=room['id']:
                            raise ValueError('讨论范围不匹配')
                    result = self._message(conn,run,content,instance_id=instance['id'],task_id=task['id'],room_id=room['id'],
                                            recipients=[m for m in members if m!=instance['id']],discussion_id=discussion_id)
                    if mentions and not discussion_id:
                        result['discussion_id'] = self._discussion(conn,run,room,content,mentions,root_trigger=task['id'])
            else:
                raise ValueError('未知编排工具')
            self._record_receipt(conn,scope,request_id,arguments,result)
            self._event(conn,run['id'],'command_accepted',tool=tool,request_id=request_id,
                          task_id=task['id'],instance_id=instance['id'],activation_id=activation_id,result=result)
            return result

    def _delegated_budget(self, run, task, requested):
        """Validate against live session grants without freezing implicit caps."""
        from app.domain.teams import narrow_capabilities, normalize_budget
        explicit=normalize_budget(task.get('budget',{}))
        effective=copy.deepcopy(run['limits'])
        for key,value in explicit.items():
            session=effective.get(key)
            effective[key]=session if value is None else (value if session is None else min(session,value))
        # A model cannot claim a greater effective quota than its current grant.
        narrow_capabilities({'budget':effective},{'budget':requested})
        # Persist only actual task grants; inherited session limits stay live.
        return narrow_capabilities({'budget':explicit},{'budget':requested})['budget']

    def _cancel_subtree(self, conn, run, task_id, reason):
        rows = conn.execute('WITH RECURSIVE tree(id) AS (SELECT id FROM agent_tasks WHERE id=? AND run_id=? '
                            'UNION ALL SELECT t.id FROM agent_tasks t JOIN tree ON t.parent_task_id=tree.id) '
                            'SELECT t.* FROM agent_tasks t JOIN tree ON t.id=tree.id',(task_id,run['id'])).fetchall()
        for row in rows:
            task = unpack(row)
            if task['status'] in TERMINAL:
                continue
            task.update(status='cancelled',error={'message':reason,'type':'cancelled'})
            self._finish_task(conn,run,task)
            instance = self._get(conn,'agent_instances',task['instance_id'],run['id'])
            if instance and instance['current_task_id']==task['id']:
                instance.update(status='idle',current_task_id=None,epoch=instance['epoch']+1)
                self._save_instance(conn,instance)
            conn.execute("UPDATE team_activations SET status='cancelled' WHERE task_id=? AND status IN ('prepared','running','result_ready','receipt_ready','uncertain')",(task['id'],))
            conn.execute("UPDATE orchestration_operations SET status='cancelled' WHERE task_id=? AND status NOT IN ('committed','failed','cancelled')",(task['id'],))
            conn.execute("UPDATE budget_reservations SET status='released' WHERE task_id=? AND status='reserved'",(task['id'],))

    def _finish_task(self, conn, run, task):
        self._save_task(conn,task)
        if task['parent_task_id']:
            parent = self._get(conn,'agent_tasks',task['parent_task_id'],run['id'])
            if parent:
                self._message(conn,run,task.get('result') or task.get('error') or '',
                               instance_id=task['instance_id'],task_id=task['id'],recipients=[parent['instance_id']],
                               kind='child_result',recipient_task_id=parent['id'],status=task['status'])
        self._event(conn,run['id'],'task_finished',task_id=task['id'],instance_id=task['instance_id'],status=task['status'])

    def _pause(self, conn, run, reason):
        if run.get('segment_start'):
            run['active_seconds'] = run.get('active_seconds',0)+max(0,time.time()-run['segment_start'])
        run.update(status='paused',paused_reason=reason,segment_start=None,epoch=run['epoch']+1)
        self._save_session(conn,run)
        # Results already saved remain reusable. Running external calls are uncertain.
        for row in conn.execute("SELECT * FROM team_activations WHERE run_id=? AND status IN ('prepared','running','result_ready')",(run['id'],)).fetchall():
            activation = unpack(row)
            new_status = 'receipt_ready' if activation['status']=='result_ready' else ('uncertain' if activation['status']=='running' else 'abandoned')
            conn.execute('UPDATE team_activations SET status=? WHERE id=?',(new_status,activation['id']))
            if new_status=='uncertain':
                conn.execute("UPDATE orchestration_attempts SET status='uncertain' WHERE operation_id=? AND status='running'",(activation['id'],))
            task = self._get(conn,'agent_tasks',activation['task_id'],run['id'])
            if task and task['status'] not in TERMINAL:
                task.update(status='paused',paused_from='queued',uncertain=new_status=='uncertain',
                            pending_activation_id=activation['id'] if new_status=='receipt_ready' else None)
                self._save_task(conn,task)
            instance = self._get(conn,'agent_instances',activation['instance_id'],run['id'])
            if instance:
                instance.update(status='paused',epoch=instance['epoch']+1)
                self._save_instance(conn,instance)
        conn.execute("UPDATE budget_reservations SET status='released' WHERE run_id=? AND status='reserved'",(run['id'],))
        self._event(conn,run['id'],'run_paused',reason=reason)

    def pause(self, run_id, reason='manual'):
        with self.transaction() as conn:
            run = self._session(conn,run_id)
            if run['status']=='running':
                self._pause(conn,run,reason)
        return self.snapshot(run_id)

    def resume(self, run_id, retry_uncertain=False):
        with self.transaction() as conn:
            run = self._session(conn,run_id)
            if run['status']!='paused':
                raise TeamConflict('只能继续已暂停的运行')
            if run.get('graph_version')!=self.graph_version:
                raise TeamConflict('执行图版本不兼容，请保留数据并迁移')
            if self._limit_reached(conn,run):
                raise TeamConflict('运行上限已到，请先延长限制')
            tasks = [unpack(r) for r in conn.execute('SELECT * FROM agent_tasks WHERE run_id=?',(run_id,))]
            if any(t.get('uncertain') and t['status'] not in TERMINAL for t in tasks) and not retry_uncertain:
                raise TeamConflict('存在结果不确定的外部调用；核对工具副作用后，显式勾选重试不确定任务')
            run.update(status='running',paused_reason=None,segment_start=time.time(),epoch=run['epoch']+1)
            self._save_session(conn,run)
            for task in tasks:
                if task['status']=='paused':
                    task.update(status=task.pop('paused_from','queued'),uncertain=False)
                    self._save_task(conn,task)
                    instance = self._get(conn,'agent_instances',task['instance_id'],run_id)
                    instance.update(status='queued',epoch=instance['epoch']+1)
                    self._save_instance(conn,instance)
            self._event(conn,run_id,'run_resumed',retry_uncertain=retry_uncertain)
        return self.snapshot(run_id)

    def update_limits(self, run_id, limits):
        with self.transaction() as conn:
            run = self._session(conn,run_id)
            if run['status']=='completed':
                raise TeamConflict('已完成的运行不能修改限制')
            run['limits'] = limits
            self._save_session(conn,run)
            self._event(conn,run_id,'limits_updated',limits=limits)
        return self.snapshot(run_id)

    def _reconcile(self, conn, run):
        from app.domain.teams import wait_satisfied
        tasks = [unpack(r) for r in conn.execute('SELECT * FROM agent_tasks WHERE run_id=?',(run['id'],))]
        indexed = {t['id']:t for t in tasks}
        for task in tasks:
            task=self._get(conn,'agent_tasks',task['id'],run['id'])
            duration=task.get('budget',{}).get('total_duration_seconds')
            if duration is not None and task['status'] not in TERMINAL and self._active_elapsed(run)-task.get('created_active_seconds',0)>=duration:
                task.update(status='failed',error={'type':'duration_limit','message':'任务运行时长达到上限'})
                self._finish_task(conn,run,task)
                self._cancel_descendants(conn,run,task)
                conn.execute("UPDATE team_activations SET status='cancelled' WHERE task_id=? AND status IN ('prepared','running','result_ready','receipt_ready','uncertain')",(task['id'],))
                conn.execute("UPDATE orchestration_operations SET status='failed',error=? WHERE task_id=? AND status NOT IN ('committed','failed','cancelled')",(encode(task['error']),task['id']))
                conn.execute("UPDATE budget_reservations SET status='released' WHERE task_id=? AND status='reserved'",(task['id'],))
                instance=self._get(conn,'agent_instances',task['instance_id'],run['id'])
                if instance and instance['current_task_id']==task['id']:
                    instance.update(status='idle',current_task_id=None,epoch=instance['epoch']+1)
                    self._save_instance(conn,instance)
                continue
            if task['status']=='waiting_children':
                wait = task['wait']
                timed_out = wait.get('deadline') is not None and run.get('active_seconds',0)+(time.time()-run['segment_start'] if run.get('segment_start') else 0)>=wait['deadline']
                if wait_satisfied(wait,indexed) or timed_out:
                    task.update(status='queued',wait_timeout=timed_out)
                    self._save_task(conn,task)
                    instance = self._get(conn,'agent_instances',task['instance_id'])
                    instance['status'] = 'queued'
                    self._save_instance(conn,instance)
                    self._event(conn,run['id'],'parent_ready',task_id=task['id'],instance_id=instance['id'],timeout=timed_out)
        # A room has one ordinary reply at a time; busy/waiting members just retain mail.
        for row in conn.execute("SELECT * FROM discussions WHERE run_id=? AND status='pending'",(run['id'],)).fetchall():
            discussion = unpack(row)
            current = indexed.get(discussion.get('active_task_id'))
            if current and current['status'] not in TERMINAL:
                continue
            members = discussion['participants']
            selected = None
            while discussion['remaining']>0 and discussion['cursor']<len(members):
                member = members[discussion['cursor']]
                discussion['cursor']+=1
                instance = self._get(conn,'agent_instances',member,run['id'])
                busy = conn.execute("SELECT 1 FROM agent_tasks WHERE run_id=? AND instance_id=? AND status NOT IN ('succeeded','failed','cancelled')",
                                    (run['id'],member)).fetchone()
                if instance and not busy and not instance['current_task_id']:
                    selected=member
                    break
            if selected:
                discussion['active_task_id'] = self._new_task(conn,run,selected,discussion['goal'],kind='room_reply',
                            discussion_id=discussion['id'],room_id=discussion['room_id'],
                            budget={})
                discussion['remaining']-=1
            status = 'pending' if selected else 'finished'
            conn.execute('UPDATE discussions SET status=?,payload=? WHERE id=?',
                         (status,encode({k:v for k,v in discussion.items() if k not in {'id','run_id','room_id','status'}}),discussion['id']))
        live = conn.execute("SELECT 1 FROM agent_tasks WHERE run_id=? AND status NOT IN ('succeeded','failed','cancelled') LIMIT 1",(run['id'],)).fetchone()
        pending = conn.execute("SELECT 1 FROM discussions WHERE run_id=? AND status='pending' LIMIT 1",(run['id'],)).fetchone()
        auxiliary=conn.execute("SELECT 1 FROM team_activations WHERE run_id=? AND status='auxiliary' LIMIT 1",(run['id'],)).fetchone()
        if not live and not pending and not auxiliary:
            self._pause(conn,run,'tasks_finished')

    def _task_available(self, conn, task):
        """Every ancestor's budget covers its whole subtree; reservations count once."""
        available = None
        cursor = task
        while cursor:
            limit = cursor.get('budget',{}).get('total_max_tokens')
            if limit is not None:
                subtree = 'WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT t.id FROM agent_tasks t JOIN tree ON t.parent_task_id=tree.id) '
                used = conn.execute(subtree+'SELECT coalesce(sum(completion_tokens),0) FROM usage_events WHERE task_id IN (SELECT id FROM tree)',(cursor['id'],)).fetchone()[0]
                reserved = conn.execute(subtree+"SELECT coalesce(sum(tokens),0) FROM budget_reservations WHERE status='reserved' AND task_id IN (SELECT id FROM tree)",(cursor['id'],)).fetchone()[0]
                remaining = max(0,limit-used-reserved)
                available = remaining if available is None else min(available,remaining)
            cursor = self._get(conn,'agent_tasks',cursor['parent_task_id']) if cursor['parent_task_id'] else None
        return available

    def _output_allowance(self, conn, run, task=None):
        """Reserve the actual finite remaining grant, without an implicit turn cap."""
        limits = []
        single = run['limits'].get('single_max_tokens')
        if single is not None and single != 0:
            limits.append(single)
        if task is not None:
            for ancestor in self._ancestors(conn,task):
                single = ancestor.get('budget',{}).get('single_max_tokens')
                if single is not None and single != 0:
                    limits.append(single)
            available = self._task_available(conn,task)
            if available is not None:
                limits.append(available)
        total = run['limits'].get('total_max_tokens')
        if total is not None:
            limits.append(total-self._usage(conn,run['id'])['completion_tokens']-
                          self._reserved(conn,run['id'])-run['limits']['summary_max_tokens'])
        return min(limits) if limits else None

    def dispatch(self, run_id, slots=1, *, blocked_instance_ids=()):
        blocked_instance_ids=frozenset(blocked_instance_ids)
        with self.transaction() as conn:
            run = self._session(conn,run_id)
            if run['status']!='running':
                return []
            self._touch_progress(conn,run)
            if self._limit_reached(conn,run):
                self._pause(conn,run,'limit')
                return []
            self._reconcile(conn,run)
            if run['status']!='running':
                return []
            count = conn.execute("SELECT count(*) FROM team_activations WHERE run_id=? AND status IN ('prepared','running','result_ready','auxiliary')",(run_id,)).fetchone()[0]
            slots = min(slots,run['limits']['max_concurrency']-count,run['limits']['max_processes']-count)
            selected = []
            # SQLite insertion order is acceptance order, even when timestamps
            # tie or the wall clock moves backwards. Continuations retain the
            # same row and current_task_id, ahead of later work for that role.
            for row in conn.execute("SELECT * FROM agent_tasks WHERE run_id=? AND status='queued' ORDER BY rowid",(run_id,)).fetchall():
                if len(selected)>=slots:
                    break
                task = unpack(row)
                if task['instance_id'] in blocked_instance_ids:
                    continue
                if any(t.get('budget',{}).get(key) is not None and self._subtree_stat(conn,t['id'],'count(*)',"status='running'")>=t['budget'][key]
                       for t in self._ancestors(conn,task) for key in ('max_concurrency','max_processes')):
                    continue
                instance = self._get(conn,'agent_instances',task['instance_id'],run_id)
                if instance['current_task_id'] not in (None,task['id']):
                    continue
                if conn.execute("SELECT 1 FROM team_activations WHERE instance_id=? AND status IN ('prepared','running','result_ready')",(instance['id'],)).fetchone():
                    continue
                pending_id = task.get('pending_activation_id')
                saved = self._get(conn,'team_activations',pending_id) if pending_id else None
                saved = saved if saved and saved['status']=='receipt_ready' else None
                activation_limit = min(run['limits']['max_activations_per_task'],task.get('budget',{}).get('max_activations_per_task',run['limits']['max_activations_per_task']))
                if not saved and task['activation_count']>=activation_limit:
                    task.update(status='failed',error={'type':'activation_limit','message':'任务激活次数达到上限'})
                    self._finish_task(conn,run,task)
                    self._cancel_descendants(conn,run,task)
                    instance.update(status='idle',current_task_id=None)
                    self._save_instance(conn,instance)
                    continue
                if saved:
                    # Reuse the already paid-for output; no new model reservation.
                    allowance = saved['output_budget']
                else:
                    allowance = self._output_allowance(conn,run,task)
                if allowance is not None and allowance<=0:
                    if not selected and count==0:
                        self._pause(conn,run,'budget')
                    break
                task.pop('pending_activation_id',None)
                activation_id = saved['id'] if saved else uid('activation')
                mail = [r['id'] for r in conn.execute('SELECT id FROM mailbox_deliveries WHERE run_id=? AND instance_id=? AND consumed_by IS NULL AND (task_id IS NULL OR task_id=?)',(run_id,instance['id'],task['id']))]
                input_ref = {'mailbox_ids':mail, 'child_task_ids':[r[0] for r in conn.execute('SELECT id FROM agent_tasks WHERE parent_task_id=?',(task['id'],))],
                             'child_result_ids':[r[0] for r in conn.execute("SELECT id FROM agent_tasks WHERE parent_task_id=? AND status IN ('succeeded','failed','cancelled')",(task['id'],))],
                             'whiteboard_rev':run['whiteboard_rev'],'output_budget':allowance,
                             'task_goal':task['goal'],'task_input':task.get('input',{}),'acceptance':task.get('acceptance','')}
                if saved:
                    conn.execute("UPDATE team_activations SET status='result_ready',epoch=?,instance_epoch=? WHERE id=?",(run['epoch'],instance['epoch'],activation_id))
                    conn.execute('UPDATE orchestration_operations SET runner_epoch=?,instance_epoch=? WHERE operation_id=?',(run['epoch'],instance['epoch'],activation_id))
                else:
                    conn.execute('INSERT INTO team_activations VALUES (?,?,?,?,?,?,?,?,?)',
                                 (activation_id,run_id,instance['id'],task['id'],run['epoch'],instance['epoch'],'prepared',encode(input_ref),db._now()))
                    conn.execute('INSERT INTO orchestration_operations(operation_id,conversation_id,kind,status,runner_epoch,input,instance_id,task_id,activation_id,instance_epoch,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (activation_id,run_id,'team_activation','prepared',run['epoch'],encode(input_ref),instance['id'],task['id'],activation_id,instance['epoch'],db._now(),db._now()))
                if not saved:
                    conn.execute('INSERT OR REPLACE INTO budget_reservations VALUES (?,?,?,?,?,?)',
                                 (activation_id,run_id,task['id'],allowance if allowance is not None else 0,'reserved',db._now()))
                task.update(status='running',activation_count=task['activation_count']+(0 if saved else 1),uncertain=False)
                instance.update(status='running',current_task_id=task['id'])
                self._save_task(conn,task); self._save_instance(conn,instance)
                self._event(conn,run_id,'activation_claimed',activation_id=activation_id,instance_id=instance['id'],task_id=task['id'],output_budget=allowance)
                selected.append(activation_id)
            return selected

    def heartbeat(self, run_id):
        """Persist active time and settle deadlines even while all slots are busy."""
        with self.transaction() as conn:
            run=self._session(conn,run_id)
            if run['status']=='running':
                self._touch_progress(conn,run)
                if self._limit_reached(conn,run):
                    self._pause(conn,run,'limit')
                else:
                    self._reconcile(conn,run)
        return self.snapshot(run_id)

    def _cancel_descendants(self, conn, run, task):
        for child in conn.execute('SELECT id FROM agent_tasks WHERE parent_task_id=?',(task['id'],)).fetchall():
            self._cancel_subtree(conn,run,child['id'],'父任务失败或取消')

    def context(self, activation_id):
        with self.reading() as conn:
            activation,run,instance,task = self._assert_activation(conn,activation_id,True)
            input_ref = activation
            messages = []
            for mailbox_id in input_ref['mailbox_ids']:
                row = conn.execute('SELECT m.* FROM team_messages m JOIN mailbox_deliveries d ON d.message_id=m.id WHERE d.id=? AND d.instance_id=?',
                                   (mailbox_id,instance['id'])).fetchone()
                if row:
                    messages.append(unpack(row))
            children = [unpack(r) for r in conn.execute('SELECT * FROM agent_tasks WHERE parent_task_id=?',(task['id'],))]
            receipts = [{'request_id':r['request_id'],'arguments':json.loads(r['arguments']),'result':json.loads(r['result'])} for r in
                        conn.execute('SELECT * FROM team_receipts WHERE scope=? ORDER BY created_at',(run['id']+':task:'+task['id'],))]
            rooms = [unpack(r) for r in conn.execute('SELECT * FROM team_rooms WHERE run_id=?',(run['id'],))
                     if instance['id'] in [m['instance_id'] for m in unpack(r)['members']]]
            visible_roles=[]
            for row in conn.execute('SELECT * FROM agent_instances WHERE run_id=? AND id!=?',(run['id'],instance['id'])):
                other=unpack(row)
                visibility=other['role'].get('visibility',[])
                if visibility=='all' or (isinstance(visibility,list) and
                        ('all' in visibility or instance['id'] in visibility or instance.get('node_id') in visibility)):
                    visible_roles.append({'instance_id':other['id'],'name':other['name'],
                                          'system_prompt':other['role']['system_prompt']})
            artifact = conn.execute('SELECT * FROM team_artifacts WHERE run_id=? AND rev=?',(run['id'],activation['whiteboard_rev'])).fetchone()
            direct_children = [{'id':r['id'],'name':unpack(r)['name'],'status':r['status'],
                                'node_id':unpack(r).get('node_id'),'role_id':unpack(r)['role'].get('id')} for r in
                               conn.execute('SELECT * FROM agent_instances WHERE run_id=? AND parent_id=?',(run['id'],instance['id']))]
            # Tools/private logs and siblings' task inputs never enter this projection.
            history = encode({'task':{'id':task['id'],'goal':activation['task_goal'],'input':activation['task_input'],
                                     'acceptance':activation['acceptance'],'kind':task.get('kind'),
                                     'wait_timeout':task.get('wait_timeout',False),'conflict':task.get('conflict')},
                              'messages':messages,'children':direct_children,
                              'child_results':[t for t in children if t['id'] in activation.get('child_result_ids',[])],
                              'receipts':receipts,'rooms':rooms,'visible_roles':visible_roles,'whiteboard':dict(artifact) if artifact else {},
                              'shared_background':run.get('shared_background','')})
            return {'conversation_id':run['id'],'instance_id':instance['id'],'task_id':task['id'],
                    'activation_id':activation_id,'system':instance['role']['system_prompt'],
                    'history':history,'harness_state':instance.get('harness_state',{}),
                    'tools':instance['role'].get('tools',[]),'output_budget':activation['output_budget'],
                    'model_config':instance['role'].get('model_config_id','default'),
                    'task':task,'instance':instance,'run':run}

    def operation(self, activation_id):
        with self.reading() as conn:
            row = conn.execute('SELECT * FROM orchestration_operations WHERE operation_id=?',(activation_id,)).fetchone()
            if not row:
                raise KeyError('激活操作不存在')
            op = dict(row)
            for key in ('input','result','output'):
                op[key] = json.loads(op[key]) if op.get(key) else None
            return op

    def start_attempt(self, activation_id):
        with self.transaction() as conn:
            activation,run,instance,task = self._assert_activation(conn,activation_id,True)
            op = conn.execute('SELECT * FROM orchestration_operations WHERE operation_id=?',(activation_id,)).fetchone()
            # Definite external receipts win over a checkpoint interrupted before promotion.
            saved = conn.execute("SELECT * FROM orchestration_attempts WHERE operation_id=? AND status='result_ready' AND result IS NOT NULL ORDER BY created_at DESC LIMIT 1",(activation_id,)).fetchone()
            if op['result'] or saved:
                result = op['result'] or saved['result']
                conn.execute("UPDATE orchestration_operations SET status='result_ready',result=? WHERE operation_id=?",(result,activation_id))
                conn.execute("UPDATE team_activations SET status='result_ready' WHERE id=?",(activation_id,))
                return {'result':json.loads(result),'attempt_id':saved['attempt_id'] if saved else None}
            attempt_id = uid('attempt')
            now = db._now()
            conn.execute('INSERT INTO orchestration_attempts(attempt_id,operation_id,runner_epoch,status,instance_id,task_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                         (attempt_id,activation_id,run['epoch'],'running',instance['id'],task['id'],now,now))
            conn.execute("UPDATE orchestration_operations SET status='running' WHERE operation_id=?",(activation_id,))
            conn.execute("UPDATE team_activations SET status='running' WHERE id=?",(activation_id,))
            return {'attempt_id':attempt_id,'result':None}

    def save_result(self, activation_id, attempt_id, result):
        with self.transaction() as conn:
            activation = self._get(conn,'team_activations',activation_id)
            if not activation:
                return False
            run = self._session(conn,activation['run_id'])
            attempt=conn.execute('SELECT operation_id FROM orchestration_attempts WHERE attempt_id=?',(attempt_id,)).fetchone()
            if not attempt or attempt['operation_id']!=activation_id:
                raise TeamConflict('外部回执与激活身份不匹配')
            self._touch_progress(conn,run)
            conn.execute("UPDATE orchestration_attempts SET status='result_ready',result=?,updated_at=? WHERE attempt_id=?",(encode(result),db._now(),attempt_id))
            conn.execute("UPDATE orchestration_operations SET status='result_ready',result=?,updated_at=? WHERE operation_id=? AND status NOT IN ('committed','cancelled','failed','abandoned')",(encode(result),db._now(),activation_id))
            if activation['status'] in ACTIVE:
                conn.execute("UPDATE team_activations SET status='result_ready' WHERE id=?",(activation_id,))
                return True
            if activation['status']=='uncertain' and run['status']=='paused':
                task = self._get(conn,'agent_tasks',activation['task_id'])
                if task['status']=='paused':
                    task.update(uncertain=False,pending_activation_id=activation_id)
                    self._save_task(conn,task)
                    conn.execute("UPDATE team_activations SET status='receipt_ready' WHERE id=?",(activation_id,))
            return False

    def record_usage(self, activation_id, attempt_id, event):
        with self.transaction() as conn:
            activation = self._get(conn,'team_activations',activation_id)
            if not activation:
                return False
            run=self._session(conn,activation['run_id'])
            attempt=conn.execute('SELECT operation_id FROM orchestration_attempts WHERE attempt_id=?',(attempt_id,)).fetchone()
            if not attempt or attempt['operation_id']!=activation_id:
                raise TeamConflict('用量回执与激活身份不匹配')
            self._touch_progress(conn,run)
            prompt = max(0,int(event.get('prompt_tokens',event.get('input_tokens',0)) or 0))
            output = max(0,int(event.get('completion_tokens',event.get('output_tokens',0)) or 0))
            event_key = event.get('_event_id') or event.get('event_id') or event.get('id') or encode(event)
            key = attempt_id+':'+str(event_key)
            row = conn.execute('SELECT * FROM usage_events WHERE event_id=?',(key,)).fetchone()
            if row:
                if (row['prompt_tokens']!=prompt or row['completion_tokens']!=output or
                        row['operation_id']!=activation_id or row['conversation_id']!=activation['run_id'] or
                        row['instance_id']!=activation['instance_id'] or row['task_id']!=activation['task_id']):
                    raise TeamConflict('重复用量事件数据不一致')
                return False
            conn.execute('INSERT INTO usage_events(event_id,conversation_id,operation_id,attempt_id,source,prompt_tokens,completion_tokens,created_at,instance_id,task_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (key,activation['run_id'],activation_id,attempt_id,'team_dsh',prompt,output,db._now(),activation['instance_id'],activation['task_id']))
            self._event(conn,activation['run_id'],'usage',instance_id=activation['instance_id'],task_id=activation['task_id'],
                          activation_id=activation_id,attempt_id=attempt_id,prompt_tokens=prompt,completion_tokens=output)
            return True

    def record_activity(self, activation_id, event):
        with self.transaction() as conn:
            activation = self._get(conn,'team_activations',activation_id)
            if not activation:
                return
            # Only public tool events are stored; reasoning/thinking events are dropped.
            kind = str(event.get('type',event.get('kind','')))
            if 'thinking' in kind or 'reasoning' in kind:
                return
            instance=self._get(conn,'agent_instances',activation['instance_id'])
            phase=event.get('stage') or kind
            phase_changed=bool(instance and phase and instance.get('dsh_phase')!=phase)
            if phase_changed:
                instance['dsh_phase']=phase
                self._save_instance(conn,instance)
            log=event.get('tool_log')
            if not is_tool_log(log):
                # Phase is current instance state, not a separate tool call.
                if phase_changed:
                    self._event(conn,activation['run_id'],'activity',instance_id=activation['instance_id'],task_id=activation['task_id'],activation_id=activation_id)
                return
            public=dict(log)
            identity=public.get('id') or encode([public.get('session_id'),public.get('step'),public.get('call_id')])
            key = activation_id+':'+str(identity)
            existing=conn.execute('SELECT payload FROM team_tool_logs WHERE id=?',(key,)).fetchone()
            if existing:
                old=json.loads(existing['payload'])
                public=merge_tool_log(old,public)
                if public==old and not phase_changed:
                    return
            if activation['instance_id'].startswith('system:'):
                public['scope']='system'
            conn.execute('INSERT INTO team_tool_logs VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                         (key,activation['run_id'],activation['instance_id'],activation['task_id'],activation_id,encode(public)))
            self._event(conn,activation['run_id'],'activity',instance_id=activation['instance_id'],task_id=activation['task_id'],activation_id=activation_id)
            conn.execute('DELETE FROM team_tool_logs WHERE run_id=? AND id NOT IN (SELECT id FROM team_tool_logs WHERE run_id=? ORDER BY rowid DESC LIMIT 200)',(activation['run_id'],activation['run_id']))

    def validate_result(self, activation_id, result):
        from app.domain.teams import validate_delivery
        with self.reading() as conn:
            activation,run,instance,task = self._assert_activation(conn,activation_id,True)
            children = [unpack(r) for r in conn.execute('SELECT * FROM agent_tasks WHERE parent_task_id=?',(task['id'],))]
            clean = dict(result)
            clean.setdefault('speech',clean.get('content',''))
            clean.setdefault('result',clean.get('content',clean.get('speech','')))
            if clean.get('whiteboard_ops') and 'whiteboard' not in clean:
                clean['whiteboard'] = {'base_rev':activation['whiteboard_rev'],'ops':clean['whiteboard_ops']}
            return validate_delivery(clean,children,task['id'])

    def _pending_query_yield(self, conn, run, task, raw, delivery):
        """Accept scheduling-only interruption only against its durable query."""
        control = raw.get('scheduling_control')
        if not isinstance(control,dict) or control.get('kind')!='pending_children_yield':
            return False
        ids, request_id = control.get('task_ids'),control.get('request_id')
        if (not isinstance(request_id,str) or not request_id or not isinstance(ids,list) or not ids
                or any(not isinstance(identifier,str) or not identifier for identifier in ids)
                or len(set(ids))!=len(ids) or delivery.get('action')!='wait_children'
                or delivery.get('wait')!={'task_ids':ids,'mode':'all'}
                or delivery.get('result') is not None or delivery.get('whiteboard')):
            raise TeamConflict('自动等待缺少有效的调度控制回执')
        row = conn.execute('SELECT arguments,result FROM team_receipts WHERE scope=? AND request_id=?',
                           (run['id']+':task:'+task['id'],request_id)).fetchone()
        arguments,receipt = (json.loads(row['arguments']),json.loads(row['result'])) if row else ({},{})
        requested = arguments.get('args',{}).get('task_ids',[])
        children = receipt.get('tasks',[])
        expected = {'action':'wait_children','task_ids':ids,'mode':'all'}
        if (arguments.get('tool')!='kds_get_task_results' or not isinstance(requested,list)
                or any(not isinstance(identifier,str) for identifier in requested)
                or list(dict.fromkeys(requested))!=ids or receipt.get('kds_control')!=expected
                or not isinstance(children,list) or len(children)!=len(ids)
                or any(not isinstance(child,dict) or child.get('id')!=identifier
                       or child.get('parent_task_id')!=task['id'] for child,identifier in zip(children,ids))
                or not any(child.get('status') not in TERMINAL for child in children)):
            raise TeamConflict('自动等待与已保存的直属子任务查询不一致')
        for identifier in ids:
            child = self._get(conn,'agent_tasks',identifier,run['id'])
            if not child or child['parent_task_id']!=task['id']:
                raise TeamConflict('自动等待的直属子任务执行权已失效')
        return True

    def commit(self, activation_id, delivery):
        from app.domain.teams import wait_satisfied
        from app.domain.whiteboard import apply_ops
        with self.transaction() as conn:
            operation = conn.execute('SELECT * FROM orchestration_operations WHERE operation_id=?',(activation_id,)).fetchone()
            if operation and operation['status']=='committed':
                return 'committed'
            activation,run,instance,task = self._assert_activation(conn,activation_id,True)
            self._touch_progress(conn,run)
            raw = json.loads(operation['result']) if operation and operation['result'] else {}
            scheduling_yield = self._pending_query_yield(conn,run,task,raw,delivery)
            wb = delivery.get('whiteboard')
            if wb and wb.get('ops'):
                definition = run['definition']
                editors = definition.get('whiteboard_editors',[])
                if not definition.get('whiteboard_enabled') or (instance['id'] not in editors and instance.get('node_id') not in editors and instance['role'].get('id') not in editors):
                    raise ValueError('该实例无产出白板编辑权限')
                if wb['base_rev']!=run['whiteboard_rev']:
                    task.update(status='queued',conflict={'type':'whiteboard','base_rev':wb['base_rev'],'current_rev':run['whiteboard_rev']})
                    self._save_task(conn,task)
                    instance['status']='queued'; self._save_instance(conn,instance)
                    conn.execute("UPDATE team_activations SET status='abandoned' WHERE id=?",(activation_id,))
                    conn.execute("UPDATE orchestration_operations SET status='abandoned',error=? WHERE operation_id=?",(encode(task['conflict']),activation_id))
                    conn.execute("UPDATE budget_reservations SET status='released' WHERE id=?",(activation_id,))
                    self._event(conn,run['id'],'whiteboard_conflict',activation_id=activation_id,task_id=task['id'])
                    return 'conflict'
                previous = conn.execute('SELECT content FROM team_artifacts WHERE run_id=? AND rev=?',(run['id'],run['whiteboard_rev'])).fetchone()[0]
                run['whiteboard_rev']+=1
                conn.execute('INSERT INTO team_artifacts VALUES (?,?,?,?,?)',
                             (run['id'],run['whiteboard_rev'],apply_ops(previous,wb['ops']),instance['id'],db._now()))
                self._save_session(conn,run)
            # Validate completion against the latest owned tasks in the commit transaction.
            children = [unpack(r) for r in conn.execute('SELECT * FROM agent_tasks WHERE parent_task_id=?',(task['id'],))]
            if delivery['action']=='complete_task' and any(t['status'] not in TERMINAL for t in children):
                raise ValueError('当前任务仍有存活子任务')
            speech = delivery.get('speech','')
            if speech:
                if task.get('kind')=='room_reply':
                    room = self._room(conn,run['id'],task['room_id'],instance['id'])
                    self._message(conn,run,speech,instance_id=instance['id'],task_id=task['id'],room_id=room['id'],
                                   recipients=[m['instance_id'] for m in room['members'] if m['instance_id']!=instance['id']],discussion_id=task['discussion_id'])
                else:
                    self._message(conn,run,speech,instance_id=instance['id'],task_id=task['id'])
            if delivery['action']=='complete_task':
                task.update(status='succeeded',result=delivery.get('result',speech),completed_at=db._now())
                self._finish_task(conn,run,task)
                instance.update(status='idle',current_task_id=None)
            elif delivery['action']=='continue':
                task.update(status='queued',last_delivery=delivery,conflict=None)
                instance['status']='queued'
                self._save_task(conn,task)
            else:
                wait = dict(delivery['wait'])
                if wait.get('timeout_seconds') is not None:
                    wait['deadline'] = run.get('active_seconds',0)+max(0,time.time()-run['segment_start'])+wait['timeout_seconds']
                task.update(wait=wait,status='queued' if wait_satisfied(wait,children) else 'waiting_children',last_delivery=delivery)
                instance['status']=task['status']
                self._save_task(conn,task)
            instance['harness_state']=raw.get('state',raw.get('harness_state',instance.get('harness_state',{})))
            self._save_instance(conn,instance)
            for mailbox_id in activation['mailbox_ids']:
                if scheduling_yield:
                    message = conn.execute('SELECT m.payload FROM team_messages m JOIN mailbox_deliveries d '
                                           'ON d.message_id=m.id WHERE d.id=? AND d.instance_id=?',
                                           (mailbox_id,instance['id'])).fetchone()
                    # The interrupted turn has no completed delivery. Retain
                    # supplements when rebuilding its fresh DSH session; formal
                    # child results already have their own authorized history.
                    if message and json.loads(message['payload']).get('kind')!='child_result':
                        continue
                conn.execute('UPDATE mailbox_deliveries SET consumed_by=? WHERE id=? AND consumed_by IS NULL',(activation_id,mailbox_id))
            conn.execute("UPDATE orchestration_operations SET status='committed',output=?,updated_at=? WHERE operation_id=?",(encode(delivery),db._now(),activation_id))
            conn.execute("UPDATE team_activations SET status='committed' WHERE id=?",(activation_id,))
            conn.execute("UPDATE budget_reservations SET status='settled' WHERE id=?",(activation_id,))
            self._event(conn,run['id'],'activation_committed',activation_id=activation_id,instance_id=instance['id'],task_id=task['id'],action=delivery['action'])
            return 'committed'

    def fail_activation(self, activation_id, error, uncertain=False):
        with self.transaction() as conn:
            activation = self._get(conn,'team_activations',activation_id)
            if not activation or activation['status'] not in ACTIVE:
                return
            run = self._session(conn,activation['run_id'])
            task = self._get(conn,'agent_tasks',activation['task_id'])
            instance = self._get(conn,'agent_instances',activation['instance_id'])
            if uncertain:
                task['error']={'type':'uncertain','message':str(error)}
                self._save_task(conn,task)
                self._pause(conn,run,'uncertain')
            else:
                task.update(status='failed',error={'type':type(error).__name__,'message':str(error)})
                self._finish_task(conn,run,task)
                self._cancel_descendants(conn,run,task)
                instance.update(status='idle',current_task_id=None)
                self._save_instance(conn,instance)
                conn.execute("UPDATE team_activations SET status='failed' WHERE id=?",(activation_id,))
                conn.execute("UPDATE orchestration_operations SET status='failed',error=? WHERE operation_id=?",(encode(task['error']),activation_id))
                conn.execute("UPDATE orchestration_attempts SET status='failed',error=? WHERE operation_id=? AND status='running'",(encode(task['error']),activation_id))
                conn.execute("UPDATE budget_reservations SET status='released' WHERE id=?",(activation_id,))

    def recover(self):
        """Startup never invokes a model. Promote definite attempt receipts first."""
        with self.transaction() as conn:
            for row in conn.execute("SELECT * FROM team_sessions WHERE status!='completed'").fetchall():
                run = unpack(row)
                for arow in conn.execute("SELECT * FROM team_activations WHERE run_id=? AND status IN ('prepared','running','result_ready')",(run['id'],)).fetchall():
                    activation = unpack(arow)
                    receipt = conn.execute("SELECT result FROM orchestration_attempts WHERE operation_id=? AND result IS NOT NULL AND status='result_ready' ORDER BY created_at DESC LIMIT 1",(activation['id'],)).fetchone()
                    if receipt:
                        conn.execute("UPDATE team_activations SET status='result_ready' WHERE id=?",(activation['id'],))
                        conn.execute("UPDATE orchestration_operations SET status='result_ready',result=? WHERE operation_id=?",(receipt['result'],activation['id']))
                if run['status']=='running':
                    # Persisted elapsed time excludes service downtime.
                    run['segment_start']=None
                    self._pause(conn,run,'restart')
                else:
                    conn.execute("UPDATE budget_reservations SET status='released' WHERE run_id=? AND status='reserved'",(run['id'],))

    def delete(self, run_id):
        with self.transaction() as conn:
            if not self._get(conn,'team_sessions',run_id):
                return False
            conn.execute('DELETE FROM orchestration_attempts WHERE operation_id IN (SELECT operation_id FROM orchestration_operations WHERE conversation_id=?)',(run_id,))
            for table in ('usage_events','orchestration_operations','orchestration_commands'):
                conn.execute(f'DELETE FROM {table} WHERE conversation_id=?',(run_id,))
            for table in ('agent_instances','agent_tasks','team_rooms','discussions','team_messages','mailbox_deliveries',
                          'team_activations','budget_reservations','team_events','team_tool_logs','team_artifacts'):
                conn.execute(f'DELETE FROM {table} WHERE run_id=?',(run_id,))
            conn.execute('DELETE FROM team_receipts WHERE scope LIKE ?',(run_id+':%',))
            conn.execute('DELETE FROM team_sessions WHERE id=?',(run_id,))
            conn.execute('DELETE FROM conversations WHERE id=?',(run_id,))
            return True

    def prepare_finalize(self, run_id, summarize=True):
        """Human completion is durable before the optional summary request."""
        with self.transaction() as conn:
            run=self._session(conn,run_id)
            if run['status']=='completed':
                return run.get('summary_operation_id')
            tokens=run['limits']['summary_max_tokens']
            ceiling=run['limits'].get('total_max_tokens')
            if summarize and ceiling is not None and self._usage(conn,run_id)['completion_tokens']+tokens>ceiling:
                raise TeamConflict('总结预算不足，请延长上限或只保存现有结果完成')
            if run['status']=='running':
                self._pause(conn,run,'finalizing')
            for row in conn.execute('SELECT id FROM agent_tasks WHERE run_id=? AND parent_task_id IS NULL',(run_id,)).fetchall():
                self._cancel_subtree(conn,run,row['id'],'人类完成整场运行')
            run.update(status='completed',completed_at=db._now(),paused_reason=None,segment_start=None,epoch=run['epoch']+1)
            op_id=None
            if summarize:
                op_id=self._prepare_auxiliary(conn,run,'summary',{},tokens)
                run['summary_operation_id']=op_id
            self._save_session(conn,run)
            self._event(conn,run_id,'run_completed',summary_operation_id=op_id)
            return op_id

    def _prepare_auxiliary(self, conn, run, kind, arguments, tokens):
        op_id=uid('auxiliary')
        now=db._now()
        references={'kind':kind,'arguments':arguments,'message_ids':[r[0] for r in conn.execute(
                    'SELECT id FROM team_messages WHERE run_id=? AND room_id IS NOT NULL ORDER BY seq',(run['id'],))],
                    'task_ids':[r[0] for r in conn.execute("SELECT id FROM agent_tasks WHERE run_id=? AND status IN ('succeeded','failed','cancelled')",(run['id'],))],
                    'whiteboard_rev':run['whiteboard_rev'],'output_budget':tokens}
        conn.execute('INSERT INTO orchestration_operations(operation_id,conversation_id,kind,status,runner_epoch,input,instance_id,task_id,activation_id,instance_epoch,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (op_id,run['id'],'team_'+kind,'prepared',run['epoch'],encode(references),
                      'system:'+kind,'system:'+op_id,op_id,1,now,now))
        conn.execute('INSERT INTO team_activations VALUES (?,?,?,?,?,?,?,?,?)',
                     (op_id,run['id'],'system:'+kind,'system:'+op_id,run['epoch'],1,'auxiliary',encode(references),now))
        conn.execute('INSERT INTO budget_reservations VALUES (?,?,?,?,?,?)',(op_id,run['id'],None,tokens if tokens is not None else 0,'reserved',now))
        self._event(conn,run['id'],'auxiliary_prepared',operation_id=op_id,kind=kind,scope='system')
        return op_id

    def prepare_auxiliary(self, run_id, kind, arguments, request_id):
        if kind not in {'assist','score','vote'}:
            raise ValueError('辅助类型只能为 assist、score 或 vote')
        with self.transaction() as conn:
            run=self._session(conn,run_id)
            if run['status']=='completed':
                raise TeamConflict('运行已完成，只能查看')
            scope=run_id+':auxiliary'
            old=self._receipt(conn,scope,request_id,{'kind':kind,'arguments':arguments})
            if old:
                return old['operation_id']
            count=conn.execute("SELECT count(*) FROM budget_reservations WHERE run_id=? AND status='reserved'",(run_id,)).fetchone()[0]
            if count>=min(run['limits']['max_concurrency'],run['limits']['max_processes']):
                raise TeamConflict('当前并发名额已满，请稍后重试')
            tokens=self._output_allowance(conn,run)
            if (tokens is not None and tokens<=0) or self._limit_reached(conn,run):
                raise TeamConflict('没有可用辅助预算，请先延长限制')
            op_id=self._prepare_auxiliary(conn,run,kind,arguments,tokens)
            self._record_receipt(conn,scope,request_id,{'kind':kind,'arguments':arguments},{'operation_id':op_id})
            return op_id

    def auxiliary_context(self, op_id):
        with self.reading() as conn:
            op=conn.execute('SELECT * FROM orchestration_operations WHERE operation_id=?',(op_id,)).fetchone()
            if not op:
                raise KeyError('系统辅助不存在')
            refs=json.loads(op['input'])
            run=self._session(conn,op['conversation_id'])
            messages=[self._get(conn,'team_messages',mid) for mid in refs['message_ids']]
            results=[]
            for task_id in refs['task_ids']:
                task=self._get(conn,'agent_tasks',task_id)
                if task:
                    results.append({k:task.get(k) for k in ('id','instance_id','parent_task_id','goal','status','result','error')})
            artifact=conn.execute('SELECT * FROM team_artifacts WHERE run_id=? AND rev=?',(run['id'],refs['whiteboard_rev'])).fetchone()
            history=encode({'goal':run['goal'],'public_messages':messages,'task_results':results,
                            'whiteboard':dict(artifact) if artifact else {},'arguments':refs['arguments']})
            return {'conversation_id':run['id'],'instance_id':'system:'+refs['kind'],'task_id':'system:'+op_id,
                    'activation_id':op_id,'operation_id':op_id,'output_budget':refs['output_budget'],
                    'system':'你是 KDS 系统辅助。仅根据授权的公开记录与正式结果工作，不推测私有工具过程。',
                    'history':history,'model_config':{},'tools':[],'purpose':refs['kind']}

    def start_auxiliary(self, op_id):
        with self.transaction() as conn:
            row=conn.execute('SELECT * FROM orchestration_operations WHERE operation_id=?',(op_id,)).fetchone()
            if not row:
                raise KeyError('系统辅助不存在')
            run=self._session(conn,row['conversation_id'])
            reservation=conn.execute('SELECT status FROM budget_reservations WHERE id=?',(op_id,)).fetchone()
            if row['status']!='committed' and (run['epoch']!=row['runner_epoch'] or not reservation or reservation['status']!='reserved'):
                raise TeamConflict('辅助执行权已失效')
            if row['result']:
                return {'result':json.loads(row['result']),'attempt_id':None}
            known=conn.execute("SELECT result,attempt_id FROM orchestration_attempts WHERE operation_id=? AND result IS NOT NULL AND status='result_ready'",(op_id,)).fetchone()
            if known:
                conn.execute("UPDATE orchestration_operations SET status='result_ready',result=? WHERE operation_id=?",(known['result'],op_id))
                return {'result':json.loads(known['result']),'attempt_id':known['attempt_id']}
            if row['status']!='prepared':
                raise TeamConflict('辅助操作已有执行者或结果不确定')
            attempt_id=uid('attempt')
            now=db._now()
            conn.execute('INSERT INTO orchestration_attempts(attempt_id,operation_id,runner_epoch,status,instance_id,task_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                         (attempt_id,op_id,row['runner_epoch'],'running',row['instance_id'],row['task_id'],now,now))
            conn.execute("UPDATE orchestration_operations SET status='running' WHERE operation_id=?",(op_id,))
            return {'attempt_id':attempt_id,'result':None}

    def commit_auxiliary(self, op_id, error=None):
        with self.transaction() as conn:
            row=conn.execute('SELECT * FROM orchestration_operations WHERE operation_id=?',(op_id,)).fetchone()
            if not row:
                return None
            run=self._session(conn,row['conversation_id'])
            if row['status']=='committed':
                return json.loads(row['result']) if row['result'] else None
            result=json.loads(row['result']) if row['result'] else None
            if row['kind']=='team_summary':
                run['summary']=(result.get('summary',result.get('text',result.get('reply'))) if isinstance(result,dict) else result)
                run['summary_error']=str(error) if error else (None if result else '总结中断，未保存确定结果')
                self._save_session(conn,run)
            conn.execute('UPDATE orchestration_operations SET status=?,error=?,updated_at=? WHERE operation_id=?',
                         ('failed' if error else 'committed',encode(str(error)) if error else None,db._now(),op_id))
            conn.execute('UPDATE team_activations SET status=? WHERE id=?',('failed' if error else 'committed',op_id))
            conn.execute("UPDATE budget_reservations SET status='settled' WHERE id=?",(op_id,))
            self._event(conn,run['id'],'auxiliary_failed' if error else 'auxiliary_committed',operation_id=op_id,
                          scope='system',kind=row['kind'],error=str(error) if error else None,result=result)
            return result

    def recover_auxiliary(self):
        with self.reading() as conn:
            rows=[dict(r) for r in conn.execute("SELECT * FROM orchestration_operations WHERE kind IN ('team_summary','team_assist','team_score','team_vote') AND status NOT IN ('committed','failed','abandoned')")]
        for op in rows:
            with self.transaction() as conn:
                receipt=conn.execute("SELECT result FROM orchestration_attempts WHERE operation_id=? AND result IS NOT NULL AND status='result_ready' LIMIT 1",(op['operation_id'],)).fetchone()
                if not op['result'] and receipt:
                    conn.execute("UPDATE orchestration_operations SET status='result_ready',result=? WHERE operation_id=?",(receipt['result'],op['operation_id']))
            self.commit_auxiliary(op['operation_id'],None if op['result'] or receipt else '服务重启，辅助调用未保存确定结果')
