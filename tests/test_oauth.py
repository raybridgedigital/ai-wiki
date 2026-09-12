import stat
import time
from urllib.parse import parse_qs,urlsplit
import pytest
import httpx
from fastapi.testclient import TestClient
from backend.main import create_app
from backend import oauth

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr('backend.api.ROOT',tmp_path)
    return TestClient(create_app(tmp_path,False),base_url='http://127.0.0.1:8000',headers={'X-Commonplace':'local'})

def setup(client):
    assert client.put('/api/settings',json={'external_models_enabled':True,'api_provider':'custom','custom_base_url':'https://model.example/v1','custom_auth':'oauth','oauth_client_id':'registered-public-client','oauth_authorize_url':'https://auth.example/authorize','oauth_token_url':'https://auth.example/token','oauth_scope':'inference'}).status_code==200

def test_switch_defaults_off_and_persists(client,monkeypatch):
    monkeypatch.setattr(httpx.Client,'get',lambda *a,**k:pytest.fail('Network while off'))
    assert client.request('GET','/api/settings').json()['external_models_enabled'] is False
    assert client.request('POST','/api/settings/test').status_code==400
    assert client.request('POST','/api/oauth/start').status_code==400
    assert client.put('/api/settings',json={'external_models_enabled':True}).status_code==200
    assert client.put('/api/settings',json={'external_models_enabled':False}).status_code==200
    assert client.request('GET','/api/status').json()['provider_ready'] is False

def test_pkce_callback_single_use_and_no_secret_exposure(client,monkeypatch):
    setup(client)
    url=client.post('/api/oauth/start').json()['authorization_url'];q=parse_qs(urlsplit(url).query)
    assert q['code_challenge_method']==['S256'] and 'code_verifier' not in q
    calls=[]
    def post(self,url,**kwargs):
        calls.append((url,kwargs))
        assert kwargs['data']['code_verifier']
        return httpx.Response(200,json={'access_token':'secret-access','refresh_token':'secret-refresh','token_type':'Bearer','expires_in':3600})
    monkeypatch.setattr(httpx.Client,'post',post)
    state=q['state'][0]
    assert client.get('/api/oauth/callback',params={'state':state,'code':'auth-code'}).status_code==200
    assert len(calls)==1
    result=client.get('/api/settings')
    assert result.json()['oauth_connected'] is True and 'secret-' not in result.text
    assert stat.S_IMODE(oauth.token_path(client.app.state.store).stat().st_mode)==0o600
    assert client.get('/api/oauth/callback',params={'state':state,'code':'auth-code'}).status_code==400

def test_callback_binding_and_off(client,monkeypatch):
    setup(client)
    url=client.post('/api/oauth/start').json()['authorization_url'];state=parse_qs(urlsplit(url).query)['state'][0]
    outsider=TestClient(client.app,base_url='http://127.0.0.1:8000')
    assert outsider.get('/api/oauth/callback',params={'state':state,'code':'code'}).status_code==400
    client.put('/api/settings',json={'external_models_enabled':False})
    monkeypatch.setattr(httpx.Client,'post',lambda *a,**k:pytest.fail('No token exchange while off'))
    assert client.get('/api/oauth/callback',params={'state':state,'code':'code'}).status_code==400

def test_refresh_and_disconnect_on_destination_change(client,monkeypatch):
    setup(client);store=client.app.state.store;s=store.settings()
    oauth.write_token(store,{'access_token':'old','refresh_token':'refresh','expires_at':time.time()-1,'fingerprint':oauth.fingerprint(s)})
    def post(self,url,**kwargs):
        assert url=='https://auth.example/token' and kwargs['data']['grant_type']=='refresh_token'
        return httpx.Response(200,json={'access_token':'new','token_type':'Bearer','expires_in':3600})
    monkeypatch.setattr(httpx.Client,'post',post)
    assert oauth.authorization_headers(store,s)=={'Authorization':'Bearer new'}
    assert oauth.read_token(store)['refresh_token']=='refresh'
    client.put('/api/settings',json={'custom_base_url':'https://different.example/v1'})
    assert not oauth.connected(store,store.settings()) and not oauth.token_path(store).exists()

def test_custom_key_does_not_follow_destination(client,monkeypatch):
    setup(client)
    monkeypatch.setenv('CUSTOM_API_KEY','old-private-key')
    client.put('/api/settings',json={'custom_base_url':'https://different.example/v1'})
    assert not client.get('/api/settings').json()['providers']['custom']['key_configured']

@pytest.mark.parametrize('url',['http://remote.example/v1','https://user:pass@model.example','https://model.example/v1?secret=foo','file:///tmp/model'])
def test_invalid_endpoints_rejected(client,url):
    assert client.put('/api/settings',json={'custom_base_url':url}).status_code==400

def test_legacy_credentials_migrate_without_changing_bytes(client):
    store=client.app.state.store
    legacy=store.root/'credentials'/'oauth.json';legacy.parent.mkdir()
    payload=b'{"access_token":"migration-secret","refresh_token":"renewal-secret"}'
    legacy.write_bytes(payload)
    oauth.migrate_legacy(store)
    assert oauth.token_path(store).read_bytes()==payload
    assert not legacy.parent.exists()
    assert not oauth.token_path(store).is_relative_to(store.root)
    assert stat.S_IMODE(oauth.token_path(store).parent.stat().st_mode)==0o700
    oauth.migrate_legacy(store)
    assert oauth.read_token(store)['access_token']=='migration-secret'

def test_migration_write_failure_preserves_original(client,monkeypatch):
    store=client.app.state.store;legacy=store.root/'credentials'/'oauth.json';legacy.parent.mkdir();legacy.write_text('{"access_token":"keep-me"}')
    def fail(*args):raise OSError('disk unavailable')
    monkeypatch.setattr(oauth,'_atomic_private_write',fail)
    with pytest.raises(OSError):oauth.migrate_legacy(store)
    assert legacy.exists()

def test_existing_token_preserved_and_disconnect_removes_migrated_copies(client):
    store=client.app.state.store;oauth.write_token(store,{'access_token':'current'})
    legacy=store.root/'credentials'/'oauth.json';legacy.parent.mkdir();legacy.write_text('{"access_token":"old"}')
    oauth.migrate_legacy(store)
    assert oauth.read_token(store)['access_token']=='current'
    assert len(list(oauth.token_path(store).parent.glob('legacy-*.json')))==1
    assert not legacy.exists()
    oauth.disconnect(store)
    assert not list(oauth.token_path(store).parent.glob('*.json'))

def test_credentials_cannot_be_configured_inside_knowledge(client,monkeypatch):
    store=client.app.state.store
    monkeypatch.setattr(oauth,'CREDENTIALS_ROOT',store.root/'secrets')
    with pytest.raises(ValueError,match='outside'):oauth.write_token(store,{'access_token':'never-write'})
    assert not (store.root/'secrets').exists()

def test_knowledge_backup_excludes_private_credentials(client,tmp_path):
    import zipfile
    from backend.backup import backup
    store=client.app.state.store
    oauth.write_token(store,{'access_token':'unique-oauth-secret','refresh_token':'unique-refresh-secret'})
    archive=backup(store,tmp_path/'knowledge.zip')
    with zipfile.ZipFile(archive) as z:
        assert not any('credentials' in n for n in z.namelist())
        assert all(b'unique-oauth-secret' not in z.read(n) and b'unique-refresh-secret' not in z.read(n) for n in z.namelist())
