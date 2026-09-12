import difflib
import json
import os
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter,Request,HTTPException,UploadFile,File,Form,Header
from fastapi.responses import FileResponse,Response,HTMLResponse
from pydantic import Field
from dotenv import set_key
from .config import ROOT,DEFAULTS,PROVIDERS,model_for,provider_for,require_external,validate_endpoint
from . import oauth
from .db import dump,uid,now
from .models import Strict,Document
from .storage import Conflict
from .ingestion import ingest

router=APIRouter(prefix='/api')

def store(r):return r.app.state.store
def worker(r):return r.app.state.worker
def decoded(row, fields=('input','proposal','result','usage','details','metadata','warnings','locked')):
    return {k:json.loads(v) if k in fields and v else v for k,v in row.items()}

@router.get('/status')
def status(r:Request):
    s=store(r)
    counts={table:s.db.one(f'SELECT count(*) n FROM {table}')['n'] for table in ('pages','sources','revisions')}
    counts['issues']=s.db.one("SELECT count(*) n FROM issues WHERE status='OPEN'")['n']
    counts['jobs']=s.db.one("SELECT count(*) n FROM jobs WHERE state IN ('QUEUED','RUNNING','NEEDS_REVIEW','COMMITTED')")['n']
    settings=s.settings()
    return {**counts,'provider_ready':bool(settings['external_models_enabled'] and configured(s,settings) and model_for(settings)),'external_models_enabled':settings['external_models_enabled'],'data_dir':str(s.root)}

@router.get('/wiki')
def pages(r:Request,offset:int=0):
    return store(r).db.all('SELECT p.*,r.author,r.reason,r.doc FROM pages p JOIN revisions r ON r.id=p.revision_id ORDER BY p.updated DESC LIMIT 100 OFFSET ?',(max(0,offset),))

@router.get('/wiki/{page_id}')
def page(r:Request,page_id:str):return store(r).page(page_id)

@router.get('/wiki/{page_id}/versions')
def versions(r:Request,page_id:str):
    return store(r).db.all('SELECT id,previous_id,created,author,reason,change_set_id,model FROM revisions WHERE page_id=? ORDER BY created DESC',(page_id,))

@router.get('/wiki/{page_id}/versions/{revision_id}')
def version(r:Request,page_id:str,revision_id:str):
    return store(r).page(page_id,revision_id)

class Edit(Strict):
    expected_revision:str
    document:Document
    reason:str='Human edit'

@router.put('/wiki/{page_id}')
def edit(r:Request,page_id:str,body:Edit):
    result=store(r).save([{**body.document.model_dump(),'page_id':page_id,'expected_revision':body.expected_revision,'reason':body.reason}],author='HUMAN',full=True)
    return result

class Protection(Strict):
    expected_revision:str
    locked:list[str]

@router.put('/wiki/{page_id}/protection')
def protection(r:Request,page_id:str,body:Protection):
    s=store(r)
    with s.db.connection(True) as c:
        page=c.execute('SELECT * FROM pages WHERE id=?',(page_id,)).fetchone()
        if not page or page['revision_id']!=body.expected_revision:raise Conflict('Page changed; refresh before changing protection')
        doc=json.loads(c.execute('SELECT doc FROM revisions WHERE id=?',(page['revision_id'],)).fetchone()['doc'])
        if not set(body.locked).issubset({'*',*[b['id'] for b in doc['blocks']]}):raise ValueError('Unknown section')
        c.execute('UPDATE pages SET locked=? WHERE id=?',(dump(body.locked),page_id))
        s.db.event('protection',f'Updated protection for {page["title"]}',{'page_id':page_id,'locked':body.locked},c)
    return {'ok':True}

class Restore(Strict):
    revision_id:str
    expected_revision:str

@router.post('/wiki/{page_id}/restore')
def restore(r:Request,page_id:str,body:Restore):
    s=store(r);current=s.page(page_id);old=s.page(page_id,body.revision_id)
    doc={**old['revision']['doc'],'title':current['title'],'aliases':current['revision']['doc']['aliases'],'tags':current['revision']['doc']['tags']}
    return s.save([{**doc,'page_id':page_id,'expected_revision':body.expected_revision,'reason':f'Restored {body.revision_id}'}],author='HUMAN',full=True)

@router.get('/wiki/{page_id}/diff/{revision_id}')
def diff(r:Request,page_id:str,revision_id:str):
    s=store(r);rev=s.page(page_id,revision_id)['revision']
    previous=s.db.one('SELECT markdown FROM revisions WHERE id=?',(rev['previous_id'],))
    return {'diff':'\n'.join(difflib.unified_diff((previous['markdown'] if previous else '').splitlines(),rev['markdown'].splitlines(),fromfile='Previous',tofile='Selected revision',lineterm=''))}

class TextSource(Strict):
    title:str=Field(min_length=1,max_length=200)
    text:str=Field(min_length=1,max_length=1000000)
    compile:bool=True

@router.post('/sources/text',status_code=202)
def add_text(r:Request,body:TextSource,idempotency_key:str|None=Header(None)):
    source=ingest(store(r),body.title,body.text.encode(),'text')
    job=worker(r).submit('compile' if body.compile else 'extract',{'source_id':source['id']},idempotency_key)
    return {'source':source,'job':decoded(job)}

@router.post('/sources/files',status_code=202)
async def upload(r:Request,file:UploadFile=File(...),title:str=Form(''),compile:bool=Form(True),idempotency_key:str|None=Header(None)):
    limit=store(r).settings()['max_upload_bytes']
    raw=await file.read(limit+1)
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in ('.pdf','.md','.txt'):raise ValueError('Supported files: PDF, Markdown, TXT')
    kind={'.pdf':'pdf','.md':'markdown','.txt':'text'}[suffix]
    source=ingest(store(r),title or file.filename or 'Untitled',raw,kind,file.filename or '')
    return {'source':source,'job':decoded(worker(r).submit('compile' if compile else 'extract',{'source_id':source['id']},idempotency_key))}

class URLSource(Strict):
    url:str=Field(min_length=1,max_length=2000)
    title:str=''
    compile:bool=True

@router.post('/sources/url',status_code=202)
def add_url(r:Request,body:URLSource,idempotency_key:str|None=Header(None)):
    return {'job':decoded(worker(r).submit('url',{'url':body.url,'title':body.title,'extract_only':not body.compile},idempotency_key))}

@router.get('/sources')
def sources(r:Request,offset:int=0):
    return [decoded(s) for s in store(r).db.all('SELECT * FROM sources ORDER BY created DESC LIMIT 100 OFFSET ?',(max(offset,0),))]

@router.get('/sources/{source_id}')
def source(r:Request,source_id:str):
    s=store(r);source=s.db.one('SELECT * FROM sources WHERE id=?',(source_id,))
    if not source:raise KeyError('Source not found')
    return {**decoded(source),'extractions':[decoded(e) for e in s.db.all('SELECT * FROM extractions WHERE source_id=? ORDER BY created DESC',(source_id,))],'passages':s.db.all('SELECT * FROM passages WHERE extraction_id=?',(source['extraction_id'],)),'used_by':s.db.all('SELECT DISTINCT p.id,p.title FROM pages p JOIN citations c ON c.revision_id=p.revision_id JOIN passages x ON x.id=c.passage_id JOIN extractions e ON e.id=x.extraction_id WHERE e.source_id=?',(source_id,))}

@router.get('/sources/{source_id}/original')
def original(r:Request,source_id:str):
    s=store(r);source=s.db.one('SELECT * FROM sources WHERE id=?',(source_id,))
    if not source:raise KeyError('Source not found')
    return FileResponse(s.root/source['path'],media_type='application/octet-stream',filename=Path(source['path']).name,headers={'Content-Security-Policy':"sandbox; default-src 'none'",'X-Content-Type-Options':'nosniff'})

@router.get('/extractions/{extraction_id}/passages/{passage_id}')
def passage(r:Request,extraction_id:str,passage_id:str):
    p=store(r).db.one('SELECT * FROM passages WHERE id=? AND extraction_id=?',(passage_id,extraction_id))
    if not p:raise KeyError('Passage not found')
    return p

@router.get('/extractions/{extraction_id}')
def extraction_view(r:Request,extraction_id:str):
    s=store(r);e=s.db.one('SELECT * FROM extractions WHERE id=?',(extraction_id,))
    if not e:raise KeyError('Extraction not found')
    return {**decoded(e),'passages':s.db.all('SELECT * FROM passages WHERE extraction_id=?',(extraction_id,))}

@router.post('/sources/{source_id}/compile',status_code=202)
def compile_source(r:Request,source_id:str,idempotency_key:str|None=Header(None)):
    return decoded(worker(r).submit('compile',{'source_id':source_id},idempotency_key))

@router.post('/sources/{source_id}/reextract',status_code=202)
def reextract(r:Request,source_id:str):return decoded(worker(r).submit('extract',{'source_id':source_id,'reextract':True}))

@router.get('/jobs')
def jobs(r:Request):return [decoded(j) for j in store(r).db.all('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]

@router.get('/jobs/{jid}')
def job(r:Request,jid:str):
    j=store(r).db.one('SELECT * FROM jobs WHERE id=?',(jid,))
    if not j:raise KeyError('Job not found')
    return decoded(j)

@router.post('/jobs/{jid}/retry',status_code=202)
def retry(r:Request,jid:str):
    s=store(r)
    with s.db.connection(True) as c:
        j=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not j or j['state'] not in ('FAILED','COMMITTED','NEEDS_REVIEW'):raise Conflict('Job is not retryable')
        if j['state']=='COMMITTED':pass
        else:c.execute("UPDATE jobs SET state='QUEUED',stage='Retry queued',error=NULL,proposal=NULL,usage='{}' WHERE id=?",(jid,))
    if j['state']=='COMMITTED':worker(r).apply(jid)
    return job(r,jid)

@router.get('/jobs/{jid}/preview')
def preview(r:Request,jid:str):
    s=store(r);j=job(r,jid);changes=[]
    for ch in (j.get('proposal') or {}).get('changes',[]):
        old=s.page(ch['page_id'],ch['expected_revision']) if ch.get('page_id') else None
        before=old['revision']['markdown'] if old else ''
        blocks={b['id']:b for b in old['revision']['doc']['blocks']} if old else {}
        blocks.update({b['id']:b for b in ch['blocks']})
        doc=Document.model_validate({k:ch[k] for k in ('title','aliases','tags','related')}|{'blocks':list(blocks.values())})
        after,_=s.markdown(doc,ch.get('page_id') or 'new-page','proposed',old['created'] if old else now())
        changes.append({'title':ch['title'],'reason':ch['reason'],'diff':'\n'.join(difflib.unified_diff(before.splitlines(),after.splitlines(),fromfile='Before',tofile='Proposed',lineterm=''))})
    return {'changes':changes}

@router.post('/jobs/{jid}/apply')
def apply(r:Request,jid:str):return worker(r).apply(jid)

@router.post('/jobs/{jid}/reject')
def reject(r:Request,jid:str):
    with store(r).db.connection(True) as c:
        j=c.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()
        if not j or j['state'] not in ('NEEDS_REVIEW','REJECTED'):raise Conflict('Only review proposals can be rejected')
        c.execute("UPDATE jobs SET state='REJECTED',stage='Rejected by user',updated=? WHERE id=?",(now(),jid))
    return {'ok':True}

@router.get('/search')
def search(r:Request,q:str=''):return store(r).search(q)

class Question(Strict):question:str=Field(min_length=1,max_length=4000)

@router.post('/research',status_code=202)
def research(r:Request,body:Question):return decoded(worker(r).submit('research',body.model_dump()))

@router.get('/research')
def answers(r:Request):return [decoded(a,('answer','evidence','revisions','usage')) for a in store(r).db.all('SELECT * FROM answers ORDER BY created DESC LIMIT 50')]

@router.get('/research/{answer_id}')
def answer(r:Request,answer_id:str):
    s=store(r);a=s.db.one('SELECT * FROM answers WHERE id=?',(answer_id,))
    if not a:raise KeyError('Answer not found')
    return {**decoded(a,('answer','evidence','revisions','usage')),'passages':s.evidence(json.loads(a['evidence']))}

@router.post('/research/{answer_id}/save',status_code=202)
def save_answer(r:Request,answer_id:str):return decoded(worker(r).submit('save_research',{'answer_id':answer_id}))

class Audit(Strict):semantic:bool=False

@router.post('/maintenance/audit',status_code=202)
def audit(r:Request,body:Audit):return decoded(worker(r).submit('audit',body.model_dump()))

@router.get('/maintenance/issues')
def issues(r:Request):return [decoded(i) for i in store(r).db.all('SELECT * FROM issues ORDER BY created DESC LIMIT 300')]

class IssueState(Strict):status:str=Field(pattern='^(OPEN|RESOLVED|DISMISSED)$')

@router.patch('/maintenance/issues/{iid}')
def issue_state(r:Request,iid:str,body:IssueState):
    store(r).db.execute('UPDATE issues SET status=? WHERE id=?',(body.status,iid));return {'ok':True}

@router.get('/activity')
def activity(r:Request):return [decoded(e) for e in store(r).db.all('SELECT * FROM events ORDER BY created DESC LIMIT 150')]

def configured(s,values):
    if values['api_provider']=='custom':
        if values['custom_auth']=='oauth':return oauth.connected(s,values)
        if values['custom_auth']=='none':return bool(values['custom_base_url'])
    return bool(os.getenv(PROVIDERS[values['api_provider']]['key_env']))

@router.get('/settings')
def settings(r:Request):
    values=store(r).settings()
    return {**values,'key_configured':configured(store(r),values),'oauth_connected':oauth.connected(store(r),values),
        'oauth_redirect_uri':oauth.REDIRECT_URI,
        'providers':{k:{'name':p['name'],'base_url':provider_for({**values,'api_provider':k})['base_url'],'key_configured':bool(os.getenv(p['key_env']))} for k,p in PROVIDERS.items()},'data_dir':str(store(r).root)}

@router.put('/settings')
def update_settings(r:Request,body:dict):
    for key,value in body.items():
        if key not in DEFAULTS:raise ValueError('Unknown setting')
        if key=='api_provider' and value not in PROVIDERS:raise ValueError('Choose a listed API provider')
        if key=='custom_auth' and value not in ('api_key','oauth','none'):raise ValueError('Unknown authentication method')
        default=DEFAULTS[key]
        if type(value)!=type(default):raise ValueError(f'Invalid type for {key}')
        if isinstance(value,int) and not isinstance(value,bool) and not 1<=value<=100000000:raise ValueError('Limit is outside the allowed range')
        if isinstance(value,str) and len(value)>2000:raise ValueError('Setting is too long')
        if key.endswith('_url'):validate_endpoint(value,allow_empty=key!='qwen_base_url')
    previous=store(r).settings()
    # A key saved for one destination must not silently follow an endpoint edit.
    for name in ('custom','qwen'):
        field=name+'_base_url'
        if field in body and body[field].rstrip('/')!=previous[field].rstrip('/'):
            key_env=PROVIDERS[name]['key_env']
            env=ROOT/'.env'
            if env.exists():set_key(str(env),key_env,'')
            os.environ.pop(key_env,None)
    with store(r).db.connection(True) as c:
        for key,value in body.items():c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,dump(value)))
    if oauth.fingerprint(previous)!=oauth.fingerprint(store(r).settings()):oauth.disconnect(store(r))
    return settings(r)

class Credential(Strict):
    api_key:str=Field(min_length=10,max_length=1000)
    provider:str='openrouter'

@router.post('/settings/credential')
def credential(r:Request,body:Credential):
    if body.provider not in PROVIDERS:raise ValueError('Unknown provider')
    if any(c.isspace() for c in body.api_key):raise ValueError('API keys must not contain whitespace')
    key_env=PROVIDERS[body.provider]['key_env']
    env=ROOT/'.env'
    if not env.exists():env.touch(mode=0o600)
    set_key(str(env),key_env,body.api_key)
    env.chmod(0o600)
    os.environ[key_env]=body.api_key
    return {'key_configured':True}

@router.post('/settings/test')
def test_connection(r:Request):
    import httpx
    require_external(store(r))
    values=store(r).settings();p=provider_for(values)
    validate_endpoint(p['base_url'])
    headers=oauth.authorization_headers(store(r),values)
    try:
        with httpx.Client(timeout=20,follow_redirects=False) as client:
            response=client.get(p['base_url']+'/models',headers=headers)
        if response.status_code!=200:raise ValueError(f'{p["name"]} returned HTTP {response.status_code}; check your credentials and account access')
        models=[m['id'] for m in response.json().get('data',[]) if isinstance(m,dict) and 'id' in m]
        chosen=model_for(values)
        return {'message':f'{p["name"]} model listing reachable. '+('Selected model is listed.' if chosen in models else 'Selected model was not listed; check its identifier.')+' This does not verify paid inference access.','model_available':chosen in models,'models':models}
    except httpx.HTTPError:raise ValueError('Could not reach the selected provider. Check your network and try again.')
    except (KeyError,TypeError,json.JSONDecodeError):raise ValueError('Provider returned an invalid model listing.')

@router.post('/oauth/start')
def oauth_start(r:Request,response:Response):
    url,binding=oauth.start(store(r))
    response.set_cookie('commonplace_oauth',binding,httponly=True,samesite='lax',max_age=600,path='/api/oauth/callback')
    return {'authorization_url':url}

@router.get('/oauth/callback')
def oauth_callback(r:Request,state:str='',code:str=''):
    import html
    try:
        oauth.callback(store(r),state,code,r.cookies.get('commonplace_oauth',''))
        message='OAuth connected. Return to Settings to select your model.'
        status=200
    except ValueError as e:message=str(e);status=400
    response=HTMLResponse('<!doctype html><meta name="viewport" content="width=device-width"><title>Commonplace OAuth</title><h1>Commonplace</h1><p>'+html.escape(message)+'</p><a href="/settings">Return to Settings</a>',status_code=status,headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"})
    response.delete_cookie('commonplace_oauth',path='/api/oauth/callback')
    return response

@router.post('/oauth/disconnect')
def oauth_disconnect(r:Request):
    oauth.disconnect(store(r));return {'ok':True}

@router.get('/export/wiki')
def export(r:Request):
    import io,zipfile
    s=store(r);s.materialize();out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for path in (s.root/'wiki').rglob('*.md'):z.write(path,str(path.relative_to(s.root)))
    return Response(out.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="commonplace-wiki.zip"'})

@router.post('/demo',status_code=202)
def demo(r:Request):
    jobs=[]
    for path in sorted((ROOT/'tests/fixtures/demo').glob('*.txt')):
        source=ingest(store(r),'Synthetic demo · '+path.stem,path.read_bytes(),'text',metadata={'synthetic':True})
        jobs.append(decoded(worker(r).submit('extract',{'source_id':source['id']})))
    return jobs
