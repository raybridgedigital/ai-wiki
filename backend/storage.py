import hashlib
import json
import re
from pathlib import Path
import yaml
from .db import dump, now, uid
from .models import Document, Proposal
from .config import DEFAULTS

class Conflict(ValueError):
    pass

def normalize(name):
    return " ".join(name.casefold().split())

def sha(text):
    return hashlib.sha256(text if isinstance(text,bytes) else text.encode()).hexdigest()

class Store:
    def __init__(self, db):
        self.db = db
        self.root = db.root
        from .oauth import migrate_legacy
        migrate_legacy(self)

    def settings(self):
        return {**DEFAULTS, **{r['key']:json.loads(r['value']) for r in self.db.all("SELECT * FROM settings")}}

    def page(self, page_id, revision=None):
        page = self.db.one("SELECT * FROM pages WHERE id=?", (page_id,))
        if not page:
            raise KeyError("Page not found")
        rev = self.db.one("SELECT * FROM revisions WHERE id=? AND page_id=?", (revision or page['revision_id'],page_id))
        if not rev:
            raise KeyError("Revision not found")
        return {**page, 'locked':json.loads(page['locked']), 'revision':{**rev,'doc':json.loads(rev['doc'])}, 'citations':self.db.all("SELECT c.*,p.text,p.locator,e.source_id,e.id extraction_id,s.title source_title,s.path FROM citations c JOIN passages p ON p.id=c.passage_id JOIN extractions e ON e.id=p.extraction_id JOIN sources s ON s.id=e.source_id WHERE c.revision_id=?",(rev['id'],)), 'backlinks':self.db.all("SELECT DISTINCT p.id,p.title FROM pages p JOIN relationships r ON r.revision_id=p.revision_id WHERE r.target_id=?",(page_id,))}

    def evidence(self, ids):
        result = []
        for i in dict.fromkeys(ids):
            row = self.db.one("SELECT p.*,e.source_id,s.title source_title FROM passages p JOIN extractions e ON e.id=p.extraction_id JOIN sources s ON s.id=e.source_id WHERE p.id=?",(i,))
            if not row:
                raise ValueError(f"Evidence passage does not exist: {i}")
            result.append(row)
        return result

    def validate_doc(self, doc, ai=True):
        ids = set()
        for block in doc.blocks:
            if block.id in ids:
                raise ValueError("Duplicate section identifiers")
            ids.add(block.id)
            if ai and block.kind != 'Uncertain' and not block.evidence:
                raise ValueError(f"Missing evidence for {block.heading}")
            if ai and block.kind == 'Human note':
                raise ValueError("AI cannot author a human note")
            seen = set()
            for ev in block.evidence:
                if (ev.passage_id,ev.quote) in seen:
                    raise ValueError("Duplicate evidence")
                seen.add((ev.passage_id,ev.quote))
                p = self.evidence([ev.passage_id])[0]
                if ev.quote not in p['text']:
                    raise ValueError(f"Quote is absent from passage {ev.passage_id}")
        return doc

    def markdown(self, doc, page_id, rev_id, created):
        header = {'id':page_id,'revision':rev_id,'title':doc.title,'created':created,'updated':now(),'aliases':doc.aliases,'tags':doc.tags,'related':[r.target_id for r in doc.related]}
        lines = ['---',yaml.safe_dump(header,allow_unicode=True,sort_keys=False).strip(),'---','',f'# {doc.title}','']
        notes = []
        for block in doc.blocks:
            labels = []
            for ev in block.evidence:
                n = len(notes)+1
                label = f'CIT-{n:03}'
                p = self.evidence([ev.passage_id])[0]
                source = self.db.one("SELECT * FROM sources WHERE id=?",(p['source_id'],))
                notes.append((label,block.id,ev,p,source))
                labels.append(f'[^{label}]')
            lines.extend([f'<!-- block:{block.id} -->',f'## {block.heading}','',f'*{block.kind}*','',block.content+''.join(labels),''])
        if doc.related:
            lines.extend(['## Related',''])
            for r in doc.related:
                target = self.db.one("SELECT title,slug FROM pages WHERE id=?",(r.target_id,))
                if target:
                    lines.append(f'- [{target["title"]}]({target["slug"]}.md)')
            lines.append('')
        for label,_,ev,p,s in notes:
            lines.extend([f'[^{label}]: [{s["title"]}](../../{s["path"]}), {p["locator"]}. {p["source_id"]}; {p["id"]}.',f'    Supporting quote: {ev.quote}',''])
        return '\n'.join(lines), notes

    def save(self, changes, author='AI', job_id=None, model='', prompt_hash='', full=False, allowed_evidence=None):
        proposal = Proposal.model_validate({'changes':changes})
        prepared = []
        for change in proposal.changes:
            old = self.page(change.page_id) if change.page_id else None
            if old and old['revision_id'] != change.expected_revision:
                raise Conflict('This page changed while the proposal was being prepared. Recompile from the current revision.')
            existing = Document.model_validate(old['revision']['doc']) if old else Document(title=change.title)
            oldblocks = {b.id:b for b in existing.blocks}
            incoming = {b.id:b for b in change.blocks}
            locks = old['locked'] if old else []
            if old and ('*' in locks or any(b in locks for b in incoming)) and author == 'AI':
                if '*' in locks or any(oldblocks.get(b) != incoming[b] for b in incoming if b in locks):
                    raise Conflict('A protected page or section needs human review.')
            if full and old:
                for b in locks:
                    if b == '*':
                        if existing.blocks != change.blocks:
                            raise Conflict('Unlock this page before replacing its contents.')
                    elif oldblocks.get(b) != incoming.get(b):
                        raise Conflict('Unlock the affected section before replacing it.')
            if author == 'AI':
                self.validate_doc(Document(title=change.title,blocks=change.blocks),ai=True)
                for b in change.blocks:
                    if allowed_evidence is not None and any(e.passage_id not in allowed_evidence for e in b.evidence):
                        raise ValueError('Proposal cites evidence that was not supplied to the model')
                    if b.id in oldblocks:
                        prior = {(e.passage_id,e.quote) for e in oldblocks[b.id].evidence}
                        current = {(e.passage_id,e.quote) for e in b.evidence}
                        if not prior.issubset(current):
                            raise Conflict('An update removed existing evidence. Review the proposal before changing supported knowledge.')
            if full:
                blocks = change.blocks
            else:
                blocks = [incoming.pop(b.id,b) for b in existing.blocks] + list(incoming.values())
            doc = Document(title=change.title,aliases=change.aliases,tags=change.tags,blocks=blocks,related=change.related)
            # Retained human notes are allowed; only generated blocks have AI requirements.
            self.validate_doc(doc,ai=False)
            if old and doc == existing:
                continue
            page_id = old['id'] if old else uid('PAGE')
            slug = old['slug'] if old else (re.sub(r'[^a-z0-9]+','-',change.title.lower()).strip('-')[:70] or 'page')+'-'+page_id[-6:]
            rev_id = uid('REV')
            md, notes = self.markdown(doc,page_id,rev_id,old['created'] if old else now())
            prepared.append((change,old,doc,page_id,slug,rev_id,md,notes))
        change_set = uid('CHANGE')
        with self.db.connection(True) as c:
            if job_id:
                job = c.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
                if job['change_set_id']:
                    return json.loads(job['result'])
                if job['state'] not in ('RUNNING','NEEDS_REVIEW'):
                    raise Conflict('This job cannot be applied in its current state')
            for change,old,doc,page_id,slug,rev_id,md,notes in prepared:
                current = c.execute("SELECT * FROM pages WHERE id=?",(page_id,)).fetchone()
                if old and (not current or current['revision_id'] != change.expected_revision or current['locked'] != dump(old['locked'])):
                    raise Conflict('Page or protection settings changed; replan required')
                names = {normalize(n) for n in [doc.title,*doc.aliases] if n.strip()}
                for name in names:
                    owner = c.execute("SELECT page_id FROM names WHERE name=?",(name,)).fetchone()
                    if owner and owner['page_id'] != page_id:
                        raise Conflict(f'Title or alias already belongs to another page: {name}')
                for rel in doc.related:
                    if not c.execute("SELECT 1 FROM pages WHERE id=?",(rel.target_id,)).fetchone():
                        raise ValueError('Relationship target does not exist')
                if not old:
                    c.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?)",(page_id,doc.title,slug,rev_id,now(),now(),'[]'))
                else:
                    c.execute("UPDATE pages SET title=?,revision_id=?,updated=? WHERE id=?",(doc.title,rev_id,now(),page_id))
                c.execute("DELETE FROM names WHERE page_id=?",(page_id,))
                c.executemany("INSERT INTO names VALUES(?,?)",[(n,page_id) for n in names])
                c.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",(rev_id,page_id,old['revision_id'] if old else None,now(),author,change.reason,change_set,dump(doc.model_dump()),md,model,prompt_hash))
                for label,block_id,ev,_,_ in notes:
                    c.execute("INSERT INTO citations VALUES(?,?,?,?,?,?)",(uid('CIT'),rev_id,label,block_id,ev.passage_id,ev.quote))
                for rel in doc.related:
                    c.execute("INSERT OR IGNORE INTO relationships VALUES(?,?,?)",(rev_id,rel.target_id,rel.type))
                c.execute("DELETE FROM search_index WHERE id=?",(page_id,))
                c.execute("INSERT INTO search_index VALUES(?,?,?,?)",(page_id,'WIKI',doc.title+' '+' '.join(doc.aliases+doc.tags),md))
            result = {'change_set_id':change_set,'pages':[{'id':p[3],'title':p[2].title,'revision_id':p[5],'created':p[1] is None} for p in prepared]}
            self.db.event('wiki','Knowledge updated' if prepared else 'No knowledge changes needed',result,c)
            if job_id:
                c.execute("UPDATE jobs SET state='COMMITTED',stage='Synchronizing Markdown',change_set_id=?,result=?,updated=? WHERE id=?",(change_set,dump(result),now(),job_id))
        self.materialize()
        if job_id:
            self.db.execute("UPDATE jobs SET state='COMPLETED',stage='Complete',updated=?,error=NULL WHERE id=?",(now(),job_id))
        return result

    def materialize(self):
        pages = self.db.all("SELECT p.*,r.markdown FROM pages p JOIN revisions r ON r.id=p.revision_id ORDER BY p.title")
        outputs = {f'wiki/pages/{p["slug"]}.md':p['markdown'] for p in pages}
        outputs['wiki/index.md'] = '# Knowledge index\n\n'+'\n'.join(f'- [{p["title"]}](pages/{p["slug"]}.md)' for p in pages)+'\n'
        outputs['wiki/log.md'] = '# Activity\n\n'+'\n'.join(f'## {e["created"]} | {e["kind"]}\n\n{e["message"]}\n' for e in self.db.all('SELECT * FROM events ORDER BY created'))
        for relative,text in outputs.items():
            path = self.root / relative
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError('Generated wiki paths cannot be symlinks')
            previous = self.db.one('SELECT hash FROM materialization WHERE path=?',(relative,))
            expected = sha(text)
            if path.exists():
                actual = sha(path.read_bytes())
                if actual != expected and (not previous or actual != previous['hash']):
                    recovery = self.root / 'recovery' / f'{uid("external")}-{path.name}'
                    recovery.write_bytes(path.read_bytes())
                    self.issue('external-edit',None,None,f'External edit preserved in {recovery.name}',{'path':str(recovery)},'deterministic',relative)
            if not path.exists() or sha(path.read_bytes()) != expected:
                temp = path.with_suffix('.tmp')
                temp.write_text(text,encoding='utf8')
                temp.replace(path)
            self.db.execute('INSERT INTO materialization VALUES(?,?) ON CONFLICT(path) DO UPDATE SET hash=excluded.hash',(relative,expected))

    def issue(self,kind,page_id,revision_id,message,details,method='deterministic',key=''):
        fingerprint = sha(f'{kind}:{page_id}:{key or message}')
        self.db.execute("INSERT INTO issues VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET revision_id=excluded.revision_id,message=excluded.message,details=excluded.details,status=CASE WHEN issues.revision_id IS excluded.revision_id THEN issues.status ELSE 'OPEN' END",(uid('ISSUE'),fingerprint,page_id,revision_id,kind,method,message,'OPEN',now(),dump(details)))

    def search(self, query, limit=30):
        words = re.findall(r'\w+',query,flags=re.UNICODE)[:16]
        if not words:
            return []
        q = ' OR '.join('"'+w+'"' for w in words)
        return self.db.all("SELECT id,kind,title,snippet(search_index,3,'','',' … ',24) excerpt FROM search_index WHERE search_index MATCH ? ORDER BY rank LIMIT ?",(q,limit))
