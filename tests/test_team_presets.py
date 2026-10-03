"""Opt-in paper presets preserve scope, provenance and editable versioning."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3

import pytest
from flask import Flask
from langgraph.checkpoint.memory import InMemorySaver

from app.domain.team_presets import ROLE_PRESETS, TEAM_PRESETS, get_role_preset
from app.repositories.teams import TeamRepository
from app.routes.team_api import team_api_bp
from app.services.team_sessions import MockTeamExecutor, TeamSessionService


@pytest.fixture
def preset_service(tmp_path):
    service=TeamSessionService(TeamRepository(tmp_path/'presets.db'),MockTeamExecutor(),
                               autostart=False,checkpointer=InMemorySaver())
    yield service
    service.close()


def client(service):
    app=Flask(__name__)
    app.register_blueprint(team_api_bp)
    app.extensions['team_service']=service
    return app.test_client()


def test_catalog_is_read_only_and_sources_are_visible(preset_service):
    response=client(preset_service).get('/api/team-presets')
    assert response.status_code==200
    catalog=response.get_json()
    assert len(catalog['roles'])==9 and len(catalog['teams'])==4
    assert {'read','write'}.issubset({t['name'] for t in catalog['tools']})
    assert preset_service.list_roles()==[] and preset_service.list_teams()==[]
    for preset in catalog['roles']+catalog['teams']:
        assert preset['sources'] and all(s['url'].startswith('https://') for s in preset['sources'])


@pytest.mark.parametrize('key',[p['key'] for p in ROLE_PRESETS])
def test_role_presets_create_frozen_editable_versions(preset_service,key):
    response=client(preset_service).post('/api/team-presets/roles/'+key)
    assert response.status_code==201,response.get_json()
    role=response.get_json()
    assert role['preset_key']==key and role['sources']
    assert role['default_budget']['single_max_tokens'] is None
    assert role['model_config_id']=='default'
    updated=preset_service.update_role(role['id'],{**role,'base_version':1,'name':'自定义职责'})
    assert updated['version']==2
    assert preset_service.get_role(role['id'],1)['name']==role['name']


@pytest.mark.parametrize('key',[p['key'] for p in TEAM_PRESETS])
def test_team_presets_are_launchable_forests_and_copies(preset_service,key):
    response=client(preset_service).post('/api/team-presets/teams/'+key)
    assert response.status_code==201,response.get_json()
    team=response.get_json()
    definition=team
    assert definition['preset_key']==key and definition['sources']
    assert definition['limits']['single_max_tokens'] is None
    assert len(definition['roots'])==1
    pairs=[frozenset([edge['source'],edge['target']]) for edge in definition['edges']]
    assert len(pairs)==len(set(pairs))
    for node in definition['nodes']:
        assert preset_service.get_role(node['role_id'],node['role_version'])['sources']
    run=preset_service.create_run({'team_id':team['id'],'team_version':1,'goal':'验证预设可启动',
                                   'entry_node_ids':definition['roots'],'request_id':key})
    assert len(run['agents'])==len(definition['nodes'])
    exported=preset_service.export_team(team['id'])
    assert exported['definition']['preset_key']==key
    assert preset_service.import_team(exported)['id']!=team['id']
    second=preset_service.create_preset_team(key)
    assert second['id']!=team['id']
    assert {(n['role_id'],n['role_version']) for n in second['nodes']} == {(n['role_id'],n['role_version']) for n in definition['nodes']}


def test_unknown_preset_and_tool_filter_leave_no_partial_definitions(preset_service):
    browser=client(preset_service)
    assert browser.post('/api/team-presets/roles/missing').status_code==404
    assert browser.post('/api/team-presets/teams/missing').status_code==404
    assert preset_service.list_roles()==[] and preset_service.list_teams()==[]
    restricted=get_role_preset('builder',['read'])
    assert restricted['role']['tools']==['read']
    assert get_role_preset('builder',[])['role']['tools']==[]


def test_overlapping_preset_teams_reuse_roles_without_editing_existing_refs(preset_service):
    camel=preset_service.create_preset_team('camel_pair')
    before=preset_service.export_team(camel['id'])
    autogen=preset_service.create_preset_team('autogen_review')
    builder=preset_service.create_preset_role('builder')
    assert next(n['role_id'] for n in camel['nodes'] if n['id']=='builder') == builder['id']
    assert next(n['role_id'] for n in autogen['nodes'] if n['id']=='builder') == builder['id']
    assert len(preset_service.list_roles())==4
    assert preset_service.export_team(camel['id'])==before


@pytest.mark.parametrize('field,value',[
    ('system_prompt','自定义工作规则'),('tools',['read']),('default_budget',{'single_max_tokens':50}),
    ('visibility',[]),('model_config_id','alternate'),('name','相同执行配置不同角色名称'),
])
def test_preset_reuse_does_not_merge_customized_current_versions(preset_service,monkeypatch,field,value):
    from app import config
    monkeypatch.setitem(config.TEAM_MODEL_CONFIGS,'alternate',{})
    original=preset_service.create_preset_role('builder')
    customized=preset_service.update_role(original['id'],{**original,'base_version':1,field:value})
    restored=preset_service.create_preset_role('builder')
    assert restored['id']!=customized['id'] and restored['version']==1
    assert restored['equivalence_key']!=customized['equivalence_key']
    assert preset_service.get_role(original['id'],1)['equivalence_key']==restored['equivalence_key']
    assert preset_service.get_role(original['id'])==customized


def test_preset_reuse_normalizes_unlimited_budget_and_tool_order_but_freezes_versions(preset_service):
    original=preset_service.create_preset_role('builder')
    team=preset_service.create_preset_team('camel_pair')
    old_export=preset_service.export_team(team['id'])
    equivalent=preset_service.update_role(original['id'],{
        **original,'base_version':1,'tools':list(reversed(original['tools'])),'default_budget':{}})
    assert equivalent['equivalence_key']==original['equivalence_key']
    assert preset_service.create_preset_role('builder')['id']==original['id']
    new_team=preset_service.create_preset_team('camel_pair')
    assert next(n['role_version'] for n in new_team['nodes'] if n['id']=='builder')==2
    after=preset_service.export_team(team['id'])
    assert after['definition']==old_export['definition']
    # Template head timestamps may advance; selected version payloads cannot.
    assert [{k:v for k,v in r.items() if k!='updated_at'} for r in after['roles']] == [
        {k:v for k,v in r.items() if k!='updated_at'} for r in old_export['roles']]
    zero=preset_service.update_role(original['id'],{**equivalent,'base_version':2,'default_budget':{'single_max_tokens':0}})
    assert zero['equivalence_key']==original['equivalence_key']


def test_archived_role_is_not_reused_and_existing_preview_remains_readable(preset_service):
    team=preset_service.create_preset_team('camel_pair')
    role_id=next(n['role_id'] for n in team['nodes'] if n['id']=='builder')
    preset_service.archive_role(role_id)
    replacement=preset_service.create_preset_role('builder')
    assert replacement['id']!=role_id
    assert any(r['id']==role_id and r['version']==1 for r in preset_service.preview_team(team['id'])['roles'])


def test_concurrent_preset_creation_reuses_roles_and_same_request_team(preset_service):
    def create(index):
        return preset_service.create_preset_team('camel_pair',request_id='same-team')
    with ThreadPoolExecutor(max_workers=4) as pool:
        teams=list(pool.map(create,range(4)))
    assert len({t['id'] for t in teams})==1
    assert len(preset_service.list_roles())==2 and len(preset_service.list_teams())==1
    with ThreadPoolExecutor(max_workers=4) as pool:
        copies=list(pool.map(lambda _:preset_service.create_preset_team('camel_pair'),range(4)))
    assert len({t['id'] for t in copies})==4
    assert len(preset_service.list_roles())==2
    response=client(preset_service).post('/api/team-presets/teams/autogen_review',json={'request_id':'same-team'})
    assert response.status_code==409


def test_preset_team_write_failure_rolls_back_new_roles_and_receipt(preset_service,monkeypatch):
    repo=preset_service.repository
    original=repo._write_definition
    def fail_team(conn,kind,*args,**kwargs):
        if kind=='team':
            raise RuntimeError('保存失败')
        return original(conn,kind,*args,**kwargs)
    monkeypatch.setattr(repo,'_write_definition',fail_team)
    with pytest.raises(RuntimeError,match='保存失败'):
        preset_service.create_preset_team('camel_pair',request_id='failed-create')
    assert preset_service.list_roles()==[] and preset_service.list_teams()==[]
    with repo.reading() as conn:
        assert conn.execute('SELECT count(*) FROM team_receipts').fetchone()[0]==0


def database_contents(service):
    with sqlite3.connect(service.repository.db_path) as conn:
        return list(conn.iterdump())


@pytest.mark.parametrize('key',[p['key'] for p in TEAM_PRESETS])
def test_preset_preview_shows_topology_and_roles_without_writing(preset_service,key,monkeypatch):
    before=database_contents(preset_service)
    def unexpected(*args,**kwargs):
        raise AssertionError('预览不能创建或执行')
    monkeypatch.setattr(preset_service,'create_role',unexpected)
    monkeypatch.setattr(preset_service,'create_team',unexpected)
    monkeypatch.setattr(preset_service,'start',unexpected)
    response=client(preset_service).get('/api/team-presets/teams/'+key+'/preview')
    assert response.status_code==200,response.get_json()
    preview=response.get_json()
    refs={(n['role_id'],n['role_version']) for n in preview['definition']['nodes']}
    assert refs=={(r['id'],r['version']) for r in preview['roles']}
    assert preview['definition']['sources'] and preview['definition']['edges']
    assert all(r['sources'] and r['equivalence_key'] for r in preview['roles'])
    assert database_contents(preset_service)==before


def test_saved_and_run_previews_read_frozen_versions_without_recovery(preset_service,monkeypatch):
    team=preset_service.create_preset_team('camel_pair')
    original_roles=preset_service.export_team(team['id'])['roles']
    run=preset_service.create_run({'team_id':team['id'],'team_version':1,'goal':'Frozen goal',
                                   'entry_node_ids':team['roots'],'request_id':'preview-run'})
    preset_service.update_role(original_roles[0]['id'],{
        **original_roles[0],'base_version':1,'system_prompt':'不同的新提示','name':'新版角色'})
    changed=deepcopy(team)
    changed.update(name='新团队名称',shared_background='新版背景',base_version=1)
    preset_service.update_team(team['id'],changed)
    before=database_contents(preset_service)
    def unexpected(*args,**kwargs):
        raise AssertionError('预览不得恢复、启动或创建')
    monkeypatch.setattr(preset_service.repository,'recover',unexpected)
    monkeypatch.setattr(preset_service,'start',unexpected)
    monkeypatch.setattr(preset_service,'get_snapshot',unexpected)
    browser=client(preset_service)
    history=browser.get('/api/teams/'+team['id']+'/preview?version=1').get_json()
    frozen_response=browser.get('/api/team-runs/'+run['id']+'/preview')
    assert frozen_response.status_code==200,frozen_response.get_json()
    frozen=frozen_response.get_json()
    for preview in (history,frozen):
        assert preview['definition']['name']==team['name']
        assert preview['definition']['shared_background']==team['shared_background']
        assert preview['definition']['version']==1 and preview['head_version']==2
        assert [(r['id'],r['version'],r['system_prompt']) for r in preview['roles']] == [(r['id'],r['version'],r['system_prompt']) for r in original_roles]
    assert frozen['run_id']==run['id'] and frozen['team_version']==1
    current=browser.get('/api/teams/'+team['id']+'/preview').get_json()
    assert current['definition']['name']=='新团队名称' and current['definition']['version']==2
    assert database_contents(preset_service)==before
    assert preset_service.runners=={}


def test_preview_invalid_versions_and_missing_records_fail_without_writes(preset_service):
    team=preset_service.create_preset_team('camel_pair')
    before=database_contents(preset_service)
    browser=client(preset_service)
    for suffix in ('?version=0','?version=-1','?version=abc','?version='):
        assert browser.get('/api/teams/'+team['id']+'/preview'+suffix).status_code==400
    for url in ('/api/team-presets/teams/missing/preview','/api/teams/missing/preview',
                '/api/teams/'+team['id']+'/preview?version=999','/api/team-runs/missing/preview'):
        assert browser.get(url).status_code==404
    assert database_contents(preset_service)==before


def test_preset_post_api_is_idempotent_for_roles_and_explicit_team_request(preset_service):
    browser=client(preset_service)
    role=browser.post('/api/team-presets/roles/builder').get_json()
    repeated=browser.post('/api/team-presets/roles/builder').get_json()
    assert repeated['id']==role['id'] and repeated['equivalence_key']==role['equivalence_key']
    catalog=browser.get('/api/team-presets').get_json()
    entry=next(p for p in catalog['roles'] if p['key']=='builder')
    assert entry['equivalence_key']==role['equivalence_key']==entry['role']['equivalence_key']
    assert browser.get('/api/roles').get_json()[0]['equivalence_key']==role['equivalence_key']
    first=browser.post('/api/team-presets/teams/camel_pair',json={'request_id':'api-team'}).get_json()
    again=browser.post('/api/team-presets/teams/camel_pair',json={'request_id':'api-team'}).get_json()
    assert first==again
    for payload in ('null','[]','{invalid'):
        assert browser.post('/api/team-presets/teams/camel_pair',data=payload,content_type='application/json').status_code==400
    assert len(preset_service.list_roles())==2 and len(preset_service.list_teams())==1
