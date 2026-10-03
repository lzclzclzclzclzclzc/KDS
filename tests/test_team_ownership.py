"""An actual second process cannot schedule the same team database."""
import subprocess
import sys

from app.orchestration.team_ownership import acquire_team_owner


def probe(path):
    program="""
import sys
from app.orchestration.team_ownership import acquire_team_owner
from app.repositories.teams import TeamConflict
try:
    owner=acquire_team_owner(sys.argv[1])
except TeamConflict:
    print('blocked')
else:
    print('owned')
    owner.close()
"""
    result=subprocess.run([sys.executable,"-c",program,str(path)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    return result.stdout.strip()


def test_database_owner_blocks_other_process_until_all_local_references_close(tmp_path):
    path=tmp_path/"owner.db"
    first=acquire_team_owner(path)
    second=acquire_team_owner(path)
    try:
        assert probe(path)=="blocked"
        first.close()
        assert probe(path)=="blocked"
    finally:
        first.close()
        second.close()
    assert probe(path)=="owned"
