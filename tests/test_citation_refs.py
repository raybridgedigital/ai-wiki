import json
import pytest
import httpx
from backend.citation_refs import prepare,resolve,spans,ReferencedResearch
from backend.models import ResearchResult,Proposal
from backend.jobs import Worker
from backend.provider import OpenRouter


def block(ref):
    return {'id':'fact','heading':'Fact','content':'Approved dated policies govern their stated topic.','kind':'Sourced','evidence':[{'evidence_id':ref}]}


def test_case_punctuation_and_markdown_are_never_retyped():
    text='Document authority: approved dated policies govern their stated topic.\nPrice: **€79–149**. “Quoted” text\u00a0here.'
    payload,wire,catalog=prepare({'passages':[{'id':'p1','text':text}]},ResearchResult)
    first=next(iter(catalog))
    result=resolve(wire.model_validate({'blocks':[block(first)]}),ResearchResult,catalog)
    assert result.blocks[0].evidence[0].quote==catalog[first]['quote']
    assert result.blocks[0].evidence[0].quote in text
    assert 'quote' not in wire.model_json_schema()['$defs']['CitationRef']['properties']


def test_unknown_reference_and_free_text_quote_rejected():
    _,wire,catalog=prepare({'passages':[{'id':'p1','text':'Real evidence.'}]},ResearchResult)
    with pytest.raises(ValueError,match='unknown evidence'):
        resolve(wire.model_validate({'blocks':[block('not-in-request')]}),ResearchResult,catalog)
    b=block('E0001');b['evidence'][0]['quote']='invented'
    with pytest.raises(ValueError):wire.model_validate({'blocks':[b]})


def test_retained_multi_sentence_quotes_are_selectable():
    quote='One sentence. Another sentence.'
    payload={'passages':[{'id':'p','text':quote}], 'existing_pages':[{'doc':{'evidence':[{'passage_id':'p','quote':quote}]}}]}
    request,_,catalog=prepare(payload,Proposal)
    ref=request['existing_pages'][0]['doc']['evidence'][0]['evidence_id']
    assert catalog[ref]=={'passage_id':'p','quote':quote}


def test_repeated_passage_excerpts_never_expose_unsupplied_text():
    request,_,catalog=prepare({'passages':[{'id':'p','text':'One excerpt.'},{'id':'p','text':'Another excerpt.'}]},ResearchResult)
    assert {v['quote'] for v in catalog.values()}=={'One excerpt.','Another excerpt.'}
    assert len(catalog)==2


def test_unicode_long_lines_and_offsets():
    text='\n  A “quote”\u00a0with whitespace.\r\n'+('word '*2000)+'x'*1000
    for start,end,quote in spans(text):
        assert text[start:end]==quote
        assert 0<len(quote)<=600


def test_provider_selects_refs_then_persists_exact_quotes(store,monkeypatch,evidence):
    for key,value in [('external_models_enabled',True),('default_model','test')]:
        store.db.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(value)))
    monkeypatch.setenv('OPENROUTER_API_KEY','test-key')
    def post(*args,**kwargs):
        request=json.loads(kwargs['json']['messages'][1]['content'])
        ref=request['evidence_catalog'][0]['evidence_id']
        assert 'CITATION PROTOCOL' in kwargs['json']['messages'][0]['content']
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'blocks':[block(ref)]})}}]})
    monkeypatch.setattr(httpx.Client,'post',post)
    j=Worker(store).submit('research',{})
    result=Worker(store).generate_cited(OpenRouter(store,j['id'],'research'),'Test',{'passages':[evidence['passage']]},ResearchResult)
    assert result.blocks[0].evidence[0].quote in evidence['passage']['text']


def test_real_provider_compile_and_research_pipeline(store,monkeypatch):
    from backend.ingestion import ingest,extract
    for key,value in [('external_models_enabled',True),('default_model','test'),('max_output_tokens',24000)]:
        store.db.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(value)))
    monkeypatch.setenv('OPENROUTER_API_KEY','test-key')
    # Long enough to exercise the analysis batches before planning and drafting.
    src=ingest(store,'Policy',('Approved policy applies.\n'*500).encode())
    extract(store,src['id'])
    calls=[]
    def post(*args,**kwargs):
        messages=kwargs['json']['messages'];payload=json.loads(messages[1]['content'])
        if 'evidence_catalog' not in payload:
            calls.append('plan');value={'actions':[{'type':'CREATE_PAGE','title':'Policy','reason':'Evidence'}]}
        else:
            ref=payload['evidence_catalog'][0]['evidence_id']
            b=block(ref);b['content']='Approved policy applies.'
            if 'plan' in payload:
                calls.append('draft');value={'changes':[{'title':'Policy','blocks':[b],'reason':'Evidence'}]}
            else:
                calls.append('analysis/research');value={'blocks':[b]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(value)}}]})
    monkeypatch.setattr(httpx.Client,'post',post)
    w=Worker(store);j=w.submit('compile',{'source_id':src['id']});w.process(j['id'])
    job=store.db.one('SELECT * FROM jobs WHERE id=?',(j['id'],))
    assert job['state']=='COMPLETED',job['error']
    assert calls.count('analysis/research')>=2 and 'draft' in calls
    for c in store.db.all('SELECT passage_id,quote FROM citations'):
        assert c['quote'] in store.db.one('SELECT text FROM passages WHERE id=?',(c['passage_id'],))['text']
    j=w.submit('research',{'question':'Approved policy'});w.process(j['id'])
    job=store.db.one('SELECT * FROM jobs WHERE id=?',(j['id'],))
    assert job['state']=='COMPLETED',job['error']
