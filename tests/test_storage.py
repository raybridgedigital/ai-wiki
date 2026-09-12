import json
from copy import deepcopy
import pytest
from backend.storage import Conflict,sha
from backend.ingestion import ingest,extract
from backend.db import dump
from backend.jobs import Worker

def test_accumulation_and_immutable_original(store,change,evidence):
    original=(store.root/evidence['source']['path']).read_bytes()
    result=store.save([change]);p=store.page(result['pages'][0]['id'])
    updated=deepcopy(change);updated.update(page_id=p['id'],expected_revision=p['revision_id'])
    updated['blocks']=[{'id':'price','heading':'Price','content':'Basic setup costs $500 in September 2026.','kind':'Sourced','evidence':[{'passage_id':evidence['passage']['id'],'quote':'Setup costs $500 for Basic in September 2026.'}]}]
    store.save([updated]);current=store.page(p['id'])
    assert [b['id'] for b in current['revision']['doc']['blocks']]==['overview','price']
    assert len(current['citations'])==2
    assert (store.root/evidence['source']['path']).read_bytes()==original
    assert 'Setup costs' in (store.root/'wiki/pages'/f'{p["slug"]}.md').read_text()

@pytest.mark.parametrize('bad',[{'passage_id':'nonexistent','quote':'x'},{'passage_id':None,'quote':'Made up quote'}])
def test_invalid_evidence_rejects_entire_batch(store,change,evidence,bad):
    bad=deepcopy(bad);bad['passage_id']=bad['passage_id'] or evidence['passage']['id']
    invalid=deepcopy(change);invalid['title']='Second page';invalid['aliases']=[];invalid['blocks'][0]['evidence']=[bad]
    with pytest.raises(ValueError):store.save([change,invalid])
    assert store.db.one('SELECT count(*) n FROM pages')['n']==0
    assert store.db.one('SELECT count(*) n FROM revisions')['n']==0

def test_stale_revision_and_protection(store,change):
    p=store.page(store.save([change])['pages'][0]['id'])
    update={**change,'page_id':p['id'],'expected_revision':p['revision_id'],'tags':['changed']}
    store.save([update])
    with pytest.raises(Conflict):store.save([update])
    current=store.page(p['id'])
    store.db.execute('UPDATE pages SET locked=? WHERE id=?',(dump(['overview']),p['id']))
    update=deepcopy(update);update['expected_revision']=current['revision_id'];update['blocks'][0]['content']='A rewritten overview.'
    with pytest.raises(Conflict):store.save([update])

def test_idempotent_commit_and_no_cosmetic_revision(store,change):
    worker=Worker(store);j=worker.submit('compile',{'source_id':'example'},'one')
    assert worker.submit('compile',{'source_id':'example'},'one')['id']==j['id']
    store.db.execute("UPDATE jobs SET state='RUNNING' WHERE id=?",(j['id'],))
    result=store.save([change],job_id=j['id'])
    assert store.save([change],job_id=j['id'])==result
    p=store.page(result['pages'][0]['id'])
    same={**change,'page_id':p['id'],'expected_revision':p['revision_id']}
    assert store.save([same])['pages']==[]
    assert store.db.one('SELECT count(*) n FROM revisions')['n']==1

def test_reextraction_retains_old_citations(store,change,evidence):
    p=store.page(store.save([change])['pages'][0]['id'])
    new=extract(store,evidence['source']['id'])
    assert new!=evidence['passage']['extraction_id']
    assert store.page(p['id'])['citations'][0]['extraction_id']==evidence['passage']['extraction_id']

def test_materialization_recovers_committed_job(store,change,monkeypatch):
    worker=Worker(store);j=worker.submit('compile',{},'test')
    store.db.execute("UPDATE jobs SET state='RUNNING' WHERE id=?",(j['id'],))
    materialize=store.materialize
    monkeypatch.setattr(store,'materialize',lambda:(_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(OSError):store.save([change],job_id=j['id'])
    assert store.db.one('SELECT state FROM jobs WHERE id=?',(j['id'],))['state']=='COMMITTED'
    assert store.db.one('SELECT count(*) n FROM revisions')['n']==1
    monkeypatch.setattr(store,'materialize',materialize)
    worker.apply(j['id'])
    assert store.db.one('SELECT state FROM jobs WHERE id=?',(j['id'],))['state']=='COMPLETED'
    assert store.db.one('SELECT count(*) n FROM revisions')['n']==1

def test_external_edit_preserved(store,change):
    p=store.page(store.save([change])['pages'][0]['id']);path=store.root/'wiki/pages'/f'{p["slug"]}.md'
    path.write_text('My external edits')
    store.materialize()
    assert any(f.read_text()=='My external edits' for f in (store.root/'recovery').iterdir())
    assert 'Northstar' in path.read_text()

def test_alias_collision_rolls_back(store,change):
    store.save([change]);other={**change,'title':'Another entity'}
    with pytest.raises(Conflict):store.save([other])
    assert store.db.one('SELECT count(*) n FROM pages')['n']==1

def test_source_deduplication(store):
    one=ingest(store,'a',b'Same content');two=ingest(store,'b',b'Same content')
    assert one['id']==two['id'] and two['duplicate']
    assert len(list((store.root/'data/raw').iterdir()))==1

def test_budgetless_nochange_and_empty_search(store):
    assert store.search('"[] OR ()')==[]

def test_audit_deduplicates_issues(store,change):
    store.save([change]);w=Worker(store);j=w.submit('audit',{})
    w.audit(j['id']);w.audit(j['id'])
    assert store.db.one("SELECT count(*) n FROM issues WHERE kind='orphan'")['n']==1
