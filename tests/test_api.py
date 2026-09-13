import json
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.ingestion import ingest,extract

@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path,False)
    return TestClient(app,headers={'X-Commonplace':'local'})

def test_text_upload_and_extraction_job(client):
    r=client.post('/api/sources/text',json={'title':'Notes','text':'A useful original note.','compile':False})
    assert r.status_code==202
    client.app.state.worker.process(r.json()['job']['id'])
    source=client.get('/api/sources/'+r.json()['source']['id']).json()
    assert source['status']=='EXTRACTED' and source['passages'][0]['text']=='A useful original note.'
    assert client.get('/api/search?q=useful').json()[0]['kind']=='SOURCE'
    assert client.get('/api/sources/'+source['id']+'/original').content==b'A useful original note.'

def test_csrf_and_host_boundary(client):
    assert client.post('/api/demo',headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/status',headers={'Host':'evil.example'}).status_code==400
    other=TestClient(client.app)
    assert other.post('/api/demo').status_code==403

def test_settings_providers_no_secrets(client,monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY','private-or-key');monkeypatch.setenv('DEEPSEEK_API_KEY','private-ds-key')
    r=client.put('/api/settings',json={'api_provider':'deepseek','deepseek_model':'deepseek-flash'})
    assert r.status_code==200 and r.json()['providers']['deepseek']['base_url']=='https://api.deepseek.com'
    assert 'private-' not in r.text
    assert client.put('/api/settings',json={'api_provider':'malicious'}).status_code==400

def test_restore_keeps_identity_and_history(client):
    s=client.app.state.store;source=ingest(s,'Source',b'The original statement.');eid=extract(s,source['id']);passage=s.db.one('SELECT id FROM passages WHERE extraction_id=?',(eid,))['id']
    ch={'title':'Original title','aliases':[],'tags':[],'blocks':[{'id':'one','heading':'One','content':'The original statement.','kind':'Sourced','evidence':[{'passage_id':passage,'quote':'The original statement.'}]}],'related':[],'reason':'Create'}
    result=s.save([ch]);pid=result['pages'][0]['id'];rev=result['pages'][0]['revision_id'];doc=s.page(pid)['revision']['doc'];doc['title']='New title';doc['blocks'].append({'id':'note','heading':'Note','content':'My human note.','kind':'Human note','evidence':[]})
    assert client.put('/api/wiki/'+pid,json={'expected_revision':rev,'document':doc}).status_code==200
    current=s.page(pid)['revision_id']
    assert client.post('/api/wiki/'+pid+'/restore',json={'revision_id':rev,'expected_revision':current}).status_code==200
    restored=s.page(pid)
    assert restored['title']=='New title' and len(restored['revision']['doc']['blocks'])==1
    assert len(client.get('/api/wiki/'+pid+'/versions').json())==3
    assert len(restored['citations'])==1

def test_missing_provider_retains_source(client,monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False)
    r=client.post('/api/sources/text',json={'title':'a','text':'Evidence persists.','compile':True}).json()
    client.app.state.worker.process(r['job']['id'])
    assert client.get('/api/jobs/'+r['job']['id']).json()['state']=='FAILED'
    assert client.get('/api/sources/'+r['source']['id']).json()['passages']

def test_unsupported_upload(client):
    assert client.post('/api/sources/files',files={'file':('malware.exe',b'abc')}).status_code==400


def test_batch_file_requests_continue_after_failure_and_retry_idempotently(client):
    def upload(name,raw,key):
        return client.post('/api/sources/files',files={'file':(name,raw)},data={'title':'folder/'+name,'compile':'false'},headers={'Idempotency-Key':key})
    first=upload('first.md',b'First source.','batch-1')
    assert first.status_code==202
    assert upload('empty.txt',b'','batch-2').status_code==400
    last=upload('last.txt',b'Last source.','batch-3')
    assert last.status_code==202
    retry=upload('first.md',b'First source.','batch-1')
    assert retry.status_code==202
    assert retry.json()['job']['id']==first.json()['job']['id']
    assert len(client.get('/api/sources').json())==2
    for response in (first,last):
        client.app.state.worker.process(response.json()['job']['id'])
        src=client.get('/api/sources/'+response.json()['source']['id']).json()
        assert src['status']=='EXTRACTED'
        assert src['title'].startswith('folder/')
