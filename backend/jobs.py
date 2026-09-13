import fcntl
import json
import re
import threading
import time
from datetime import datetime,timezone,timedelta
from .config import ROOT
from .db import uid,now,dump
from .storage import Conflict,sha
from .ingestion import extract,ingest,fetch_public
from .models import Plan,Proposal,ResearchResult,AuditResult,Document
from .provider import OpenRouter
from .quotes import source_quote

def prompt(name):
    return (ROOT/'backend/prompts'/f'{name}.md').read_text()+'\n'+(ROOT/'config/wiki-rules.md').read_text()

class Worker:
    def __init__(self,store,provider_factory=OpenRouter):
        self.store,self.db,self.provider_factory=store,store.db,provider_factory
        self.stop_event=threading.Event()
        self.thread=None

    def submit(self,kind,payload,key=None):
        key=key or uid('REQUEST')
        with self.db.connection(True) as c:
            old=c.execute('SELECT * FROM jobs WHERE idempotency_key=?',(key,)).fetchone()
            if old:
                original=json.loads(old['input']);original.pop('_source_id',None)
                if old['kind']!=kind or original!=payload:
                    raise Conflict('Idempotency key was already used for different input')
                return dict(old)
            jid=uid('JOB')
            c.execute('INSERT INTO jobs(id,kind,input,state,stage,created,updated,idempotency_key) VALUES(?,?,?,?,?,?,?,?)',(jid,kind,dump(payload),'QUEUED','Waiting',now(),now(),key))
        return self.db.one('SELECT * FROM jobs WHERE id=?',(jid,))

    def start(self):
        self.lock=(self.store.root/'database/worker.lock').open('a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('Another Commonplace worker already owns this data directory')
        self.db.execute("UPDATE jobs SET state='QUEUED',stage='Recovering interrupted work',updated=? WHERE state='RUNNING' AND change_set_id IS NULL",(now(),))
        try:
            self.store.materialize()
            self.db.execute("UPDATE jobs SET state='COMPLETED',stage='Recovered committed output',error=NULL WHERE state='COMMITTED'")
        except Exception:
            self.db.event('repair','Committed content is available; Markdown output needs repair')
        self.thread=threading.Thread(target=self.loop,daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        if hasattr(self,'lock'):
            self.lock.close()

    def loop(self):
        while not self.stop_event.wait(.5):
            job=self.db.one("SELECT * FROM jobs WHERE state='QUEUED' ORDER BY created LIMIT 1")
            if job:
                self.process(job['id'])

    def stage(self,jid,message):
        self.db.execute('UPDATE jobs SET stage=?,updated=? WHERE id=?',(message,now(),jid))

    def process(self,jid):
        if self.store.reset_failed:return
        try:
            with self.store.gate.activity():self._process(jid)
        except ValueError:
            return  # Reset may have started between polling and dispatch.

    def _process(self,jid):
        job=self.db.one('SELECT * FROM jobs WHERE id=?',(jid,))
        if not job or job['state']!='QUEUED':
            return
        self.db.execute("UPDATE jobs SET state='RUNNING',attempts=attempts+1,error=NULL WHERE id=?",(jid,))
        data=json.loads(job['input'])
        try:
            if job['kind']=='url':
                self.stage(jid,'Fetching public webpage')
                if not data.get('_source_id'):
                    raw,url,ctype=fetch_public(data['url'],self.store.settings()['max_url_bytes'])
                    source=ingest(self.store,data.get('title') or url,raw,'html' if 'html' in ctype else 'text',url,{'content_type':ctype,'requested_url':data['url']})
                    data={**data,'_source_id':source['id']}
                    self.db.execute('UPDATE jobs SET input=? WHERE id=?',(dump(data),jid))
                self.compile(jid,{**data,'source_id':data['_source_id']})
            elif job['kind'] in ('compile','extract'):
                self.compile(jid,{**data,'extract_only':job['kind']=='extract' or data.get('extract_only',False)})
            elif job['kind']=='research':
                self.research(jid,data)
            elif job['kind']=='save_research':
                self.compile(jid,data)
            elif job['kind']=='audit':
                result=self.audit(jid,data.get('semantic',False))
                self.complete(jid,result)
            else:
                raise ValueError('Unknown job type')
        except Exception as exc:
            current=self.db.one('SELECT * FROM jobs WHERE id=?',(jid,))
            committed=bool(current['change_set_id'])
            state='COMMITTED' if committed else 'NEEDS_REVIEW' if isinstance(exc,Conflict) else 'FAILED'
            # Model validation errors may contain source text; do not persist that payload.
            message=str(exc) if isinstance(exc,(ValueError,KeyError)) and type(exc).__name__!='ValidationError' else 'Invalid model output or processing failure. Inspect the stage and retry after correcting the input/configuration.'
            self.db.execute('UPDATE jobs SET state=?,error=?,updated=? WHERE id=?',(state,message[:1000],now(),jid))
            self.db.event('job',f'{job["kind"]}: {state.lower().replace("_"," ")}',{'job_id':jid,'error':message[:1000]})
            if data.get('source_id'):
                self.db.execute('UPDATE sources SET error=? WHERE id=?',(message[:1000],data['source_id']))

    def complete(self,jid,result):
        self.db.execute("UPDATE jobs SET state='COMPLETED',stage='Complete',result=?,updated=? WHERE id=?",(dump(result),now(),jid))

    def context_pages(self,text):
        results=self.store.search(text[:1500],10)
        ids=[r['id'] for r in results if r['kind']=='WIKI'][:6]
        return [self.store.page(i) for i in ids]

    def generate_cited(self,provider,instructions,payload,schema):
        """Retry a rejected quote once; never relax stored citation validation."""
        allowed={p['id'] for p in payload['passages']}
        request=payload
        for attempt in range(2):
            result=provider.generate(instructions,request,schema)
            documents=([Document(title=c.title,blocks=c.blocks) for c in result.changes]
                       if isinstance(result,Proposal) else [Document(title='Evidence',blocks=result.blocks)])
            if any(e.passage_id not in allowed for d in documents for b in d.blocks for e in b.evidence):
                raise ValueError('Model cited a passage outside the supplied evidence')
            # Store exact source spans, never the model's typographic version.
            for doc in documents:
                for block in doc.blocks:
                    for evidence in block.evidence:
                        text=self.store.evidence([evidence.passage_id])[0]['text']
                        evidence.quote=source_quote(evidence.quote,text)
            try:
                for doc in documents:
                    self.store.validate_doc(doc)
                return result
            except ValueError as exc:
                if not str(exc).startswith('Quote is absent from passage '):
                    raise
                self.db.event('citation_validation','Model quote rejected',{'mismatches':[{'passage_id':e.passage_id,'quote':e.quote} for d in documents for b in d.blocks for e in b.evidence if e.quote not in self.store.evidence([e.passage_id])[0]['text']]})
                if attempt:
                    raise ValueError(f'{exc}. The model could not provide an exact source quote after one correction attempt. No wiki changes were saved; your source is preserved. Retry compilation.') from exc
                request={**payload,'citation_correction':{
                    'error':str(exc),'previous_result':result.model_dump(),
                    'instruction':'Return a corrected full result. Copy each quote verbatim from its supplied passage, including whitespace and punctuation. Never combine text across passages or paraphrase a quote. Remove unsupported claims or mark them Uncertain without evidence.'}}

    def generate_planned(self,provider,payload,plan,pages):
        if not any(a.type!='NO_CHANGE' for a in plan.actions):
            return Proposal(changes=[])
        request={**payload,'plan':plan.model_dump()}
        for attempt in range(2):
            proposal=self.generate_cited(provider,prompt('compiler_update'),request,Proposal)
            errors=self.check_plan(proposal,plan,pages)
            if not errors:
                return proposal
            self.db.event('plan_validation','Draft exceeded its plan',{'errors':errors})
            if attempt:
                raise ValueError('The model still proposed edits outside the approved plan after one correction attempt. No wiki changes were saved. '+ '; '.join(errors))
            request={**payload,'plan':plan.model_dump(),'plan_correction':{
                'errors':errors,'previous_result':proposal.model_dump(),
                'instruction':'Return the full corrected proposal within the SAME plan. Omit unchanged blocks. Modify existing blocks only when their IDs are listed in section_ids for that page. Use a new unique block ID for genuinely new sections. Do not move an unapproved rewrite to a new block. Keep all other existing content and evidence intact.'}}

    @staticmethod
    def check_plan(proposal,plan,pages):
        updates={a.page_id:a for a in plan.actions if a.type=='UPDATE_PAGE'}
        creates={a.title for a in plan.actions if a.type=='CREATE_PAGE'}
        existing={p['id']:Document.model_validate(p['revision']['doc']) for p in pages}
        errors=[]
        for change in proposal.changes:
            if change.page_id:
                action=updates.get(change.page_id)
                if not action or change.expected_revision!=action.expected_revision or change.page_id not in existing:
                    errors.append(f'Update to {change.page_id} was not in the validated plan or has the wrong revision')
                    continue
                blocks={b.id:b for b in existing[change.page_id].blocks}
                # Echoing an identical block is not an edit. Omit it from the
                # incremental update so storage retains the original untouched.
                change.blocks=[b for b in change.blocks if b!=blocks.get(b.id)]
                outside=[b.id for b in change.blocks if b.id in blocks and b.id not in action.section_ids]
                if outside:
                    errors.append(f'Page {change.page_id}: unplanned sections {outside}; permitted existing sections: {action.section_ids}')
            elif change.title not in creates:
                errors.append(f'New page {change.title!r} was not in the validated plan')
        return errors

    def compile(self,jid,data):
        self.stage(jid,'Reading source evidence')
        if data.get('answer_id'):
            answer=self.db.one('SELECT * FROM answers WHERE id=?',(data['answer_id'],))
            if not answer:
                raise KeyError('Research answer not found')
            evidence=self.store.evidence(json.loads(answer['evidence']))
            subject=answer['question']
            candidate=json.loads(answer['answer'])
        else:
            source=self.db.one('SELECT * FROM sources WHERE id=?',(data['source_id'],))
            if not source:
                raise KeyError('Source not found')
            previous=self.db.one('SELECT extractor FROM extractions WHERE id=?',(source['extraction_id'],))
            upgrade_html=source['type']=='html' and previous and previous['extractor']=='beautifulsoup-html-v1'
            eid=extract(self.store,source['id']) if not source['extraction_id'] or data.get('reextract') or upgrade_html else source['extraction_id']
            if data.get('extract_only'):
                self.complete(jid,{'source_id':source['id'],'extraction_id':eid})
                return
            evidence=self.store.evidence([r['id'] for r in self.db.all('SELECT id FROM passages WHERE extraction_id=?',(eid,))])
            subject=source['title']
            candidate=None
        provider=self.provider_factory(self.store,jid,'compiler')
        incoming=evidence
        if sum(len(p['text']) for p in evidence)>10000:
            compact=[]
            for offset in range(0,len(evidence),2):
                self.stage(jid,f'Analyzing source passages {offset+1}–{min(offset+2,len(evidence))} of {len(evidence)}')
                batch=evidence[offset:offset+2]
                result=self.generate_cited(provider,prompt('researcher'),{'question':'Extract the significant factual information from all supplied passages with exact supporting quotations. Preserve concrete values, dates and limitations.','passages':batch},ResearchResult)
                self.store.validate_doc(Document(title='Analysis',blocks=result.blocks))
                allowed={p['id'] for p in batch}
                for block in result.blocks:
                    for ev in block.evidence:
                        if ev.passage_id not in allowed:
                            raise ValueError('Analysis used evidence outside its batch')
                        p=next(p for p in batch if p['id']==ev.passage_id)
                        compact.append({**p,'text':ev.quote})
            incoming=compact
        pages=self.context_pages(subject+' '+' '.join(p['text'] for p in incoming)[:1000])
        oldids=[c['passage_id'] for p in pages for c in p['citations']]
        existing_evidence=self.store.evidence(oldids)
        payload={'subject':subject,'passages':incoming+existing_evidence,'existing_pages':[{'page_id':p['id'],'revision_id':p['revision_id'],'locked':p['locked'],'document':p['revision']['doc']} for p in pages], 'catalog':self.db.all('SELECT id,title FROM pages ORDER BY updated DESC LIMIT 100'),'research_candidate':candidate}
        self.stage(jid,'Planning wiki changes')
        plan=provider.generate(prompt('compiler_plan'),payload,Plan)
        if len(plan.actions)>self.store.settings()['max_pages']:
            raise ValueError('Plan exceeds the page-change limit')
        read={p['id']:p['revision_id'] for p in pages}
        for action in plan.actions:
            if action.type=='UPDATE_PAGE' and (action.page_id not in read or action.expected_revision!=read[action.page_id]):
                raise Conflict('Plan targeted a page revision not supplied in context. Retry with a more specific source title.')
        self.stage(jid,'Drafting evidence-backed updates')
        proposal=self.generate_planned(provider,payload,plan,pages)
        allowed={p['id'] for p in payload['passages']}
        package={'changes':proposal.model_dump()['changes'],'plan':plan.model_dump(),'allowed_evidence':list(allowed),'model':provider.model,'prompt_hash':sha(prompt('compiler_plan')+prompt('compiler_update'))}
        self.db.execute('UPDATE jobs SET proposal=? WHERE id=?',(dump(package),jid))
        if self.store.settings()['review_first']:
            self.db.execute("UPDATE jobs SET state='NEEDS_REVIEW',stage='Ready for review' WHERE id=?",(jid,))
            return
        self.apply(jid)

    def apply(self,jid):
        job=self.db.one('SELECT * FROM jobs WHERE id=?',(jid,))
        if job['state'] in ('COMMITTED','COMPLETED') and job['change_set_id']:
            self.store.materialize()
            self.complete(jid,json.loads(job['result']))
            return json.loads(job['result'])
        if job['state'] not in ('NEEDS_REVIEW','RUNNING') or not job['proposal']:
            raise Conflict('This job has no applicable proposal')
        package=json.loads(job['proposal'])
        self.stage(jid,'Validating and committing')
        result=self.store.save(package['changes'],job_id=jid,model=package['model'],prompt_hash=package['prompt_hash'],allowed_evidence=set(package['allowed_evidence']))
        data=json.loads(job['input'])
        source_id=data.get('source_id') or data.get('_source_id')
        if source_id:
            self.db.execute("UPDATE sources SET status='COMPILED',error=NULL WHERE id=?",(source_id,))
        return result

    def research(self,jid,data):
        question=data['question']
        pages=self.context_pages(question)
        evidence_ids=[c['passage_id'] for p in pages for c in p['citations']]
        source_ids=[r['id'] for r in self.store.search(question,12) if r['kind']=='SOURCE']
        words=set(re.findall(r'\w+',question.lower()))
        extra=[]
        for sid in source_ids:
            source=self.db.one('SELECT extraction_id FROM sources WHERE id=?',(sid,))
            extra+=self.db.all('SELECT * FROM passages WHERE extraction_id=?',(source['extraction_id'],))
        extra.sort(key=lambda p:sum(w in p['text'].lower() for w in words),reverse=True)
        evidence_ids+= [p['id'] for p in extra[:6]]
        evidence=self.store.evidence(evidence_ids)
        self.stage(jid,'Researching local evidence')
        provider=self.provider_factory(self.store,jid,'research')
        result=self.generate_cited(provider,prompt('researcher'),{'question':question,'wiki_pages':[{'page_id':p['id'],'revision_id':p['revision_id'],'doc':p['revision']['doc']} for p in pages],'passages':evidence},ResearchResult)
        self.store.validate_doc(Document(title=question,blocks=result.blocks))
        allowed={p['id'] for p in evidence}
        if any(e.passage_id not in allowed for b in result.blocks for e in b.evidence):
            raise ValueError('Answer cites evidence not retrieved for this question')
        aid=uid('ANSWER')
        cited=list({e.passage_id for b in result.blocks for e in b.evidence})
        self.db.execute('INSERT INTO answers VALUES(?,?,?,?,?,?,?)',(aid,question,dump(result.model_dump()),now(),dump(cited),dump({p['id']:p['revision_id'] for p in pages}),dump(provider.usage)))
        self.complete(jid,{'answer_id':aid})

    def audit(self,jid,semantic=False):
        self.stage(jid,'Checking wiki integrity')
        pages=[self.store.page(p['id']) for p in self.db.all('SELECT id FROM pages')]
        found=[]
        def issue(kind,p,message,method='deterministic'):
            self.store.issue(kind,p['id'],p['revision_id'],message,{},method)
            found.append((p['id'],kind,message))
        for p in pages:
            doc=Document.model_validate(p['revision']['doc'])
            try:
                self.store.validate_doc(doc,ai=False)
            except ValueError as e:
                issue('citation-integrity',p,str(e))
            if not doc.related and not p['backlinks']:
                issue('orphan',p,'No incoming or outgoing page relationships.')
            if sum(len(b.content) for b in doc.blocks)<100:
                issue('thin-page',p,'This page has fewer than 100 characters of content.')
            if sum(len(b.content) for b in doc.blocks)>self.store.settings()['large_page_chars']:
                issue('large-page',p,'Consider dividing this large page into focused concepts.','heuristic')
            for b in doc.blocks:
                if not b.evidence and b.kind not in ('Human note','Uncertain'):
                    issue('missing-evidence',p,f'Section “{b.heading}” has no supporting passages.','heuristic')
                for title in re.findall(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]',b.content):
                    if not self.db.one('SELECT * FROM names WHERE name=?',(' '.join(title.casefold().split()),)):
                        issue('broken-link',p,f'Unresolved wiki link: {title}')
            for rel in doc.related:
                if not self.db.one('SELECT id FROM pages WHERE id=?',(rel.target_id,)):
                    issue('broken-link',p,f'Missing relationship target: {rel.target_id}')
        checked={p['id'] for p in pages}
        for previous in self.db.all("SELECT * FROM issues WHERE status='OPEN' AND method IN ('deterministic','heuristic')"):
            if previous['page_id'] in checked and (previous['page_id'],previous['kind'],previous['message']) not in found:
                self.db.execute("UPDATE issues SET status='RESOLVED' WHERE id=?",(previous['id'],))
        semantic_status='not checked'
        if semantic:
            provider=self.provider_factory(self.store,jid,'linter')
            for start in range(0,len(pages),3):
                batch=pages[start:start+3]
                self.stage(jid,f'Reviewing meaning: pages {start+1}–{min(start+3,len(pages))}')
                result=provider.generate(prompt('linter'),{'pages':[{'page_id':p['id'],'revision_id':p['revision_id'],'doc':p['revision']['doc'],'evidence':p['citations']} for p in batch]},AuditResult)
                for f in result.findings:
                    target=next((p for p in batch if p['id']==f.page_id),None)
                    if not target:
                        raise ValueError('Audit referred to a page outside its context')
                    issue(f.kind,target,f.message,'model')
            semantic_status='checked'
        self.store.materialize()
        self.db.event('audit','Wiki audit completed',{'pages':len(pages),'findings':len(found),'semantic':semantic_status})
        return {'pages_checked':len(pages),'findings':len(found),'semantic':semantic_status}
