import json
import pytest
import httpx
from backend.provider import OpenRouter,ProviderError
from backend.models import Plan
from backend.jobs import Worker
from backend.config import model_for

@pytest.mark.parametrize('provider,endpoint,env,model',[('openrouter','https://openrouter.ai/api/v1/chat/completions','OPENROUTER_API_KEY','example/model'),('deepseek','https://api.deepseek.com/chat/completions','DEEPSEEK_API_KEY','deepseek-flash'),('qwen','https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions','DASHSCOPE_API_KEY','qwen-plus'),('kimi','https://api.moonshot.ai/v1/chat/completions','MOONSHOT_API_KEY','kimi-k2.5')])
def test_provider_routes_endpoint_and_separate_key(store,monkeypatch,provider,endpoint,env,model):
    store.db.execute('INSERT INTO settings VALUES(?,?)',('external_models_enabled','true'))
    monkeypatch.setenv(env,'test-key')
    store.db.execute('INSERT INTO settings VALUES(?,?)',('api_provider',json.dumps(provider)))
    store.db.execute('INSERT INTO settings VALUES(?,?)',('default_model',json.dumps('example/model')))
    job=Worker(store).submit('compile',{})
    calls=[]
    def post(self,url,**kwargs):
        calls.append((url,kwargs))
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"actions":[]}'}}],'usage':{'prompt_tokens':100,'completion_tokens':10}})
    monkeypatch.setattr(httpx.Client,'post',post)
    p=OpenRouter(store,job['id'],'compiler');p.generate('Return JSON',{},Plan)
    assert calls[0][0]==endpoint
    assert calls[0][1]['json']['model']==model
    assert calls[0][1]['headers']['Authorization']=='Bearer test-key'

def test_input_budget_prevents_call(store,monkeypatch):
    store.db.execute('INSERT INTO settings VALUES(?,?)',('external_models_enabled','true'))
    monkeypatch.setenv('OPENROUTER_API_KEY','fake-test-key')
    store.db.execute('INSERT INTO settings VALUES(?,?)',('default_model','"example/model"'))
    store.db.execute('INSERT INTO settings VALUES(?,?)',('max_input_tokens','1'))
    job=Worker(store).submit('compile',{})
    monkeypatch.setattr(httpx.Client,'post',lambda *a,**k:pytest.fail('No request should be sent'))
    with pytest.raises(ProviderError,match='budget'):OpenRouter(store,job['id'],'compiler').generate('Return JSON',{},Plan)

def test_provider_role_overrides_are_independent(store):
    s=store.settings();s.update(api_provider='deepseek',compiler_model='openrouter/model',deepseek_compiler_model='deepseek-custom')
    assert model_for(s)=='deepseek-custom'
    s['api_provider']='openrouter'
    assert model_for(s)=='openrouter/model'

@pytest.mark.parametrize('provider', ['openrouter','deepseek','qwen','kimi','openai','grok','custom'])
def test_external_off_blocks_every_provider(store,monkeypatch,provider):
    store.db.execute('INSERT INTO settings VALUES(?,?)',('api_provider',json.dumps(provider)))
    monkeypatch.setattr(httpx.Client,'post',lambda *a,**k:pytest.fail('External request while off'))
    job=Worker(store).submit('compile',{})
    with pytest.raises(ValueError,match='External models are disabled'):
        OpenRouter(store,job['id'],'compiler').generate('JSON',{},Plan)

def test_switch_off_stops_existing_provider_and_retry(store,monkeypatch):
    store.db.execute('INSERT INTO settings VALUES(?,?)',('external_models_enabled','true'))
    store.db.execute('INSERT INTO settings VALUES(?,?)',('default_model','"test-model"'))
    monkeypatch.setenv('OPENROUTER_API_KEY','test-key')
    job=Worker(store).submit('compile',{});provider=OpenRouter(store,job['id'],'compiler');calls=[]
    def post(*a,**k):
        calls.append(True)
        store.db.execute("UPDATE settings SET value='false' WHERE key='external_models_enabled'")
        return httpx.Response(429)
    monkeypatch.setattr(httpx.Client,'post',post)
    monkeypatch.setattr('backend.provider.time.sleep',lambda _:None)
    with pytest.raises(ValueError,match='External models are disabled'):provider.generate('JSON',{},Plan)
    assert len(calls)==1

@pytest.mark.parametrize('auth',['none','api_key','oauth'])
def test_custom_connection_routes_auth(store,monkeypatch,auth):
    from backend import oauth
    import time
    for key,value in {'external_models_enabled':True,'api_provider':'custom','custom_base_url':'http://127.0.0.1:9000/v1','custom_auth':auth,'custom_model':'my-model'}.items():
        store.db.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(value)))
    monkeypatch.setenv('CUSTOM_API_KEY','custom-secret')
    if auth=='oauth':oauth.write_token(store,{'access_token':'oauth-secret','expires_at':time.time()+3600,'fingerprint':oauth.fingerprint(store.settings())})
    def post(self,url,**kwargs):
        assert url=='http://127.0.0.1:9000/v1/chat/completions'
        assert kwargs['json']['model']=='my-model'
        expected={'none':None,'api_key':'Bearer custom-secret','oauth':'Bearer oauth-secret'}[auth]
        assert kwargs['headers'].get('Authorization')==expected
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"actions":[]}'}}]})
    monkeypatch.setattr(httpx.Client,'post',post)
    job=Worker(store).submit('compile',{})
    assert OpenRouter(store,job['id'],'compiler').generate('JSON',{},Plan).actions==[]


@pytest.mark.parametrize('budget,expected',[ (16000,[4096,8192]), (4096,[4096]) ])
def test_truncated_response_retry_respects_budget(store,monkeypatch,budget,expected):
    for key,value in [('external_models_enabled',True),('default_model','test'),('max_output_tokens',budget)]:
        store.db.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(value)))
    monkeypatch.setenv('OPENROUTER_API_KEY','test-key')
    calls=[]
    def post(*args,**kwargs):
        calls.append(kwargs['json']['max_tokens'])
        return httpx.Response(200,json={'choices':[{'finish_reason':'length' if len(calls)==1 else 'stop','message':{'content':'{"actions":[]}'}}]})
    monkeypatch.setattr(httpx.Client,'post',post)
    job=Worker(store).submit('compile',{});provider=OpenRouter(store,job['id'],'compiler')
    if budget==4096:
        with pytest.raises(ProviderError,match='budget'):provider.generate('JSON',{},Plan)
    else:
        assert provider.generate('JSON',{},Plan).actions==[]
    assert calls==expected
