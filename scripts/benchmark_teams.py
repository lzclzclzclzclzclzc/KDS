"""Reproducible offline team capacity sample on temporary SQLite databases."""
import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.orchestration.checkpointer import get_saver,close_savers
from app.repositories.teams import TeamRepository
from app.services.team_sessions import TeamSessionService,MockTeamExecutor


class DelayedExecutor(MockTeamExecutor):
    def __init__(self,delay):
        self.delay=delay
        self.lock=threading.Lock()
        self.active=self.peak=0

    def execute(self,*args,**kwargs):
        with self.lock:
            self.active+=1
            self.peak=max(self.peak,self.active)
        try:
            time.sleep(self.delay)
            return super().execute(*args,**kwargs)
        finally:
            with self.lock:
                self.active-=1


def measure(count,concurrency,delay):
    with tempfile.TemporaryDirectory(prefix='kds-team-bench-') as directory:
        business=Path(directory)/'business.db'
        checkpoints=Path(directory)/'checkpoints.db'
        executor=DelayedExecutor(delay)
        service=TeamSessionService(TeamRepository(business),executor,autostart=False,checkpointer=get_saver(checkpoints))
        try:
            role=service.create_role({'name':'离线容量角色','system_prompt':'完成目标'})
            nodes=[{'id':str(index),'name':str(index),'role_id':role['id'],'role_version':1} for index in range(count)]
            team=service.create_team({'name':'容量样本','nodes':nodes,'edges':[]})
            run=service.create_run({'team_id':team['id'],'team_version':1,'goal':'固定延迟的独立工作',
                'entry_node_ids':[n['id'] for n in nodes],'request_id':'bench',
                'limits':{'max_concurrency':concurrency,'max_processes':concurrency,'max_instances':max(32,count),'max_tasks':max(128,count)}})
            service.autostart=True
            started=time.perf_counter()
            service.start(run['id'])
            while True:
                snapshot=service.get_snapshot(run['id'])
                if snapshot['status']=='paused':
                    break
                if time.perf_counter()-started>60:
                    raise RuntimeError('离线容量样本超时')
                time.sleep(.01)
            duration=time.perf_counter()-started
            assert all(task['status']=='succeeded' for task in snapshot['tasks'])
            assert snapshot['usage']['completion_tokens']==count*24
            assert snapshot['usage']['reserved_tokens']==0
            assert executor.peak<=concurrency
            service.close()
            close_savers(checkpoints)
            with service.repository.reading() as conn:
                operations=conn.execute('SELECT count(*) FROM orchestration_operations').fetchone()[0]
                events=conn.execute('SELECT count(*) FROM team_events').fetchone()[0]
            return {'instances':count,'concurrency':concurrency,'delay_ms':round(delay*1000),
                    'seconds':round(duration,3),'peak_calls':executor.peak,'operations':operations,
                    'events':events,'output_tokens':snapshot['usage']['completion_tokens'],
                    'business_bytes':business.stat().st_size,'checkpoint_bytes':checkpoints.stat().st_size}
        finally:
            service.close()
            close_savers(checkpoints)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--instances',type=int,default=8)
    parser.add_argument('--concurrency',type=int,nargs='+',default=[1,3])
    parser.add_argument('--delay',type=float,default=.1)
    args=parser.parse_args()
    for concurrency in args.concurrency:
        print(json.dumps(measure(args.instances,concurrency,args.delay),ensure_ascii=False))
