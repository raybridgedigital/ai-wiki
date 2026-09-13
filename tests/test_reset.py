import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend import oauth
from backend.ingestion import ingest,extract
from backend.reset import reset_workspace,resume_reset

@pytest.fixture
def client(tmp_path):return TestClient(create_app(tmp_path,False),headers={'X-Commonplace':'local'})

def test_reset_confirmation_and_busy(client):
    s=client.app.state.store
    ingest(s,'keep',b'Preserve until explicit reset')
    assert client.post('/api/settings/reset',json={'confirmation':'reset'}).status_code==400
    with s.gate.activity():
        assert client.post('/api/settings/reset',json={'confirmation':'RESET'}).status_code==400
    assert client.get('/api/status').json()['sources']==1
    assert TestClient(client.app).post('/api/settings/reset',json={'confirmation':'RESET'}).status_code==403

def test_reset_removes_knowledge_preserves_connections(client):
    s=client.app.state.store;source=ingest(s,'Test',b'Old vertical evidence');extract(s,source['id'])
    (s.root/'recovery'/'external.md').write_text('old recovery')
    client.app.state.worker.submit('research',{'question':'old'})
    oauth.write_token(s,{'access_token':'keep-private'})
    client.put('/api/settings',json={'external_models_enabled':True,'review_first':True})
    assert client.post('/api/settings/reset',json={'confirmation':'RESET'}).status_code==200
    assert client.get('/api/status').json()['sources']==0
    assert client.get('/api/jobs').json()==[]
    assert not list((s.root/'data').rglob('*.txt')) and not list((s.root/'recovery').iterdir())
    assert oauth.read_token(s)['access_token']=='keep-private'
    settings=client.get('/api/settings').json()
    assert settings['review_first'] and not settings['external_models_enabled']
    new=ingest(s,'Fresh',b'New vertical');extract(s,new['id'])
    assert s.search('Old')==[]

def test_full_reset_clears_credentials(client,monkeypatch,tmp_path):
    import os
    monkeypatch.setattr('backend.reset.ROOT',tmp_path)
    monkeypatch.setenv('DEEPSEEK_API_KEY','fake-test-key')
    (tmp_path/'.env').write_text('DEEPSEEK_API_KEY=fake-test-key\nAPP_DATA_DIR=./local-data\n')
    oauth.write_token(client.app.state.store,{'access_token':'remove-private'})
    client.put('/api/settings',json={'review_first':True})
    assert client.post('/api/settings/reset',json={'confirmation':'RESET','forget_connections':True}).status_code==200
    assert not os.getenv('DEEPSEEK_API_KEY') and not oauth.read_token(client.app.state.store)
    assert 'fake-test-key' not in (tmp_path/'.env').read_text()
    assert 'APP_DATA_DIR' in (tmp_path/'.env').read_text()
    assert not client.get('/api/settings').json()['review_first']

def test_interrupted_cleanup_resumes(client,monkeypatch):
    import backend.reset as reset
    s=client.app.state.store;ingest(s,'Old',b'Old source')
    original=reset.shutil.rmtree
    monkeypatch.setattr(reset.shutil,'rmtree',lambda *a,**k:(_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(ValueError,match='interrupted'):reset_workspace(s)
    assert (s.root/'database/reset-pending.json').exists()
    monkeypatch.setattr(reset.shutil,'rmtree',original)
    resume_reset(s)
    assert not (s.root/'database/reset-pending.json').exists()
    assert not list((s.root/'data/raw').iterdir())

def test_reset_removes_committed_pages_and_citations(store,change):
    from backend.reset import TABLES
    store.save([change])
    assert store.db.one('SELECT count(*) n FROM citations')['n']>0
    reset_workspace(store)
    for table in TABLES:
        assert store.db.one('SELECT count(*) n FROM '+table)['n']==0,table
    assert not list((store.root/'wiki').rglob('*.md'))
    assert store.db.all('PRAGMA foreign_key_check')==[]

def test_running_job_blocks_reset(client):
    s=client.app.state.store
    job=client.app.state.worker.submit('compile',{})
    s.db.execute("UPDATE jobs SET state='RUNNING' WHERE id=?",(job['id'],))
    result=client.post('/api/settings/reset',json={'confirmation':'RESET'})
    assert result.status_code==400
    assert s.db.one('SELECT id FROM jobs WHERE id=?',(job['id'],))
