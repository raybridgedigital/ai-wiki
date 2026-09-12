"""Authorization Code + PKCE for registered public clients of compatible APIs.
No first-party ChatGPT/Grok client IDs or subscription tokens are reused.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import tempfile
from pathlib import Path
from urllib.parse import urlencode
import httpx
from .config import ROOT, require_external, provider_for, validate_endpoint

REDIRECT_URI='http://127.0.0.1:8000/api/oauth/callback'
_lock=threading.RLock()
_pending={}

def fingerprint(settings):
    keys=('custom_base_url','custom_auth','oauth_authorize_url','oauth_token_url','oauth_client_id','oauth_scope','oauth_audience')
    return hashlib.sha256(json.dumps({k:settings[k] for k in keys},sort_keys=True).encode()).hexdigest()

# Private application state, deliberately outside every knowledge workspace.
CREDENTIALS_ROOT=ROOT/'.credentials'

def token_path(store):
    private=CREDENTIALS_ROOT.resolve()
    if private.is_relative_to(store.root.resolve()):
        raise ValueError('Credential storage must be outside the knowledge directory. Choose a dedicated APP_DATA_DIR.')
    workspace=hashlib.sha256(str(store.root.resolve()).encode()).hexdigest()
    return private/workspace/'oauth.json'

def _atomic_private_write(path,data):
    private=CREDENTIALS_ROOT.resolve()
    private.mkdir(mode=0o700,parents=True,exist_ok=True)
    private.chmod(0o700)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    path.parent.chmod(0o700)
    fd,name=tempfile.mkstemp(prefix='.oauth-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)

def migrate_legacy(store):
    """Preserve bytes privately before removing old credentials. Never log secrets."""
    with _lock:
        target=token_path(store)
        legacy_dir=store.root/'credentials'
        for name in ('oauth.json','oauth.tmp'):
            legacy=legacy_dir/name
            if not legacy.exists():continue
            if legacy.is_symlink():raise ValueError('Legacy OAuth storage is a symbolic link; migration requires a regular file.')
            data=legacy.read_bytes()
            destination=target if name=='oauth.json' and not target.exists() else target.parent/('legacy-'+hashlib.sha256(data).hexdigest()+'.json')
            _atomic_private_write(destination,data)
            if destination.read_bytes()!=data:raise ValueError('Credential migration verification failed.')
            legacy.unlink()
        if legacy_dir.exists() and not any(legacy_dir.iterdir()):legacy_dir.rmdir()

def read_token(store):
    with _lock:
        migrate_legacy(store)
        try:
            value=json.loads(token_path(store).read_text())
            return value if isinstance(value,dict) else {}
        except (FileNotFoundError,ValueError):return {}

def connected(store,settings):
    token=read_token(store)
    return bool(token.get('fingerprint')==fingerprint(settings) and token.get('access_token') and (token.get('expires_at',0)>time.time() or token.get('refresh_token')))

def write_token(store,token):
    with _lock:
        migrate_legacy(store)
        _atomic_private_write(token_path(store),json.dumps(token).encode())

def disconnect(store):
    with _lock:
        migrate_legacy(store)
        path=token_path(store)
        path.unlink(missing_ok=True)
        if path.parent.exists():
            for legacy in path.parent.glob('legacy-*.json'):legacy.unlink()
        for state,item in list(_pending.items()):
            if item['root']==str(store.root):del _pending[state]

def start(store):
    require_external(store)
    s=store.settings()
    if s['api_provider']!='custom' or s['custom_auth']!='oauth':raise ValueError('Select Custom API with OAuth authentication first.')
    for field in ('custom_base_url','oauth_authorize_url','oauth_token_url'):validate_endpoint(s[field])
    if not s['oauth_client_id']:raise ValueError('Enter the client ID registered with your OAuth provider.')
    verifier=secrets.token_urlsafe(48);state=secrets.token_urlsafe(32);binding=secrets.token_urlsafe(32)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    with _lock:
        for old,item in list(_pending.items()):
            if item['expires']<time.time() or item['root']==str(store.root):del _pending[old]
        _pending[state]={'root':str(store.root),'verifier':verifier,'binding':binding,'expires':time.time()+600,'settings':s}
    query={'response_type':'code','client_id':s['oauth_client_id'],'redirect_uri':REDIRECT_URI,'scope':s['oauth_scope'],'state':state,'code_challenge':challenge,'code_challenge_method':'S256'}
    if s['oauth_audience']:query['audience']=s['oauth_audience']
    return s['oauth_authorize_url']+'?'+urlencode(query),binding

def exchange(store,s,data,previous=None):
    require_external(store)
    try:
        with httpx.Client(timeout=20,follow_redirects=False) as client:
            response=client.post(s['oauth_token_url'],data={**data,'client_id':s['oauth_client_id']},headers={'Accept':'application/json'})
        if response.status_code!=200:raise ValueError(f'OAuth token exchange returned HTTP {response.status_code}. Check your registered client and API scopes.')
        token=response.json()
        access=token.get('access_token')
        if not isinstance(access,str) or not access or any(c.isspace() for c in access) or token.get('token_type','').lower()!='bearer':
            raise ValueError('OAuth provider must return a Bearer access token.')
        expires=float(token.get('expires_in',3600))
        if not 0<expires<=31536000:raise ValueError('Invalid OAuth token lifetime.')
        require_external(store)
        if fingerprint(store.settings())!=fingerprint(s):raise ValueError('OAuth settings changed. Connect again.')
        result={'access_token':access,'refresh_token':token.get('refresh_token') or (previous or {}).get('refresh_token'),'expires_at':time.time()+expires,'fingerprint':fingerprint(s)}
        write_token(store,result)
        return result
    except httpx.HTTPError:raise ValueError('Could not reach the OAuth token endpoint.')
    except (TypeError,KeyError,json.JSONDecodeError):raise ValueError('OAuth provider returned an invalid token response.')

def callback(store,state,code,binding):
    with _lock:
        item=_pending.get(state)
        if not item or item['root']!=str(store.root) or item['expires']<time.time() or not binding or not secrets.compare_digest(item['binding'],binding):
            raise ValueError('OAuth login expired or browser verification failed. Start again from Settings.')
        del _pending[state]
        if fingerprint(store.settings())!=fingerprint(item['settings']):raise ValueError('OAuth settings changed. Start again.')
        if not code or len(code)>8192:raise ValueError('OAuth authorization was not completed.')
        exchange(store,item['settings'],{'grant_type':'authorization_code','code':code,'redirect_uri':REDIRECT_URI,'code_verifier':item['verifier']})

def authorization_headers(store,settings):
    require_external(store)
    if settings['api_provider']=='custom' and fingerprint(settings)!=fingerprint(store.settings()):
        raise ValueError('Custom connection settings changed. Retry the job.')
    if settings['api_provider']=='custom' and settings['custom_auth']=='oauth':
        with _lock:
            token=read_token(store)
            if token.get('fingerprint')!=fingerprint(settings) or not token.get('access_token'):raise ValueError('Connect the custom OAuth provider in Settings first.')
            if token.get('expires_at',0)<time.time()+30:
                if not token.get('refresh_token'):raise ValueError('OAuth session expired. Connect again in Settings.')
                token=exchange(store,settings,{'grant_type':'refresh_token','refresh_token':token['refresh_token']},token)
            return {'Authorization':'Bearer '+token['access_token']}
    if settings['api_provider']=='custom' and settings['custom_auth']=='none':return {}
    p=provider_for(settings);key=os.getenv(p['key_env'])
    if not key:raise ValueError(f'Save a {p["name"]} API key in Settings first.')
    return {'Authorization':'Bearer '+key}
