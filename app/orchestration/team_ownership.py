"""One application process may own a team's SQLite scheduler database."""
import atexit
import os
import threading
from pathlib import Path

from app.repositories.teams import TeamConflict

_registry={}
_lock=threading.Lock()


class TeamOwnership:
    def __init__(self,key,entry):
        self.key,self.entry=key,entry
        self.closed=False

    def close(self):
        with _lock:
            if self.closed:
                return
            self.closed=True
            self.entry['references']-=1
            if self.entry['references']==0:
                file=self.entry['file']
                file.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(file.fileno(),fcntl.LOCK_UN)
                file.close()
                _registry.pop(self.key,None)


def acquire_team_owner(db_path):
    key=str(Path(db_path).resolve())
    with _lock:
        if key not in _registry:
            path=Path(key).with_suffix('.team-owner.lock')
            path.parent.mkdir(parents=True,exist_ok=True)
            file=open(path,'a+b')
            try:
                if file.tell()==0:
                    file.write(b'0');file.flush()
                file.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as error:
                file.close()
                raise TeamConflict('该数据库已有应用进程调度团队，请使用单个应用进程') from error
            _registry[key]={'file':file,'references':0}
        entry=_registry[key]
        entry['references']+=1
        owner=TeamOwnership(key,entry)
        atexit.register(owner.close)
        return owner
