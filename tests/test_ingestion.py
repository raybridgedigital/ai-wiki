import socket
import pytest
import pymupdf
from backend.ingestion import fetch_public,ingest,extract

@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:pass@example.com','http://127.0.0.1','http://[::1]','http://169.254.169.254'])
def test_reject_unsafe_urls(url):
    with pytest.raises(ValueError):fetch_public(url,100)

def test_reject_private_dns(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.1',80))])
    with pytest.raises(ValueError):fetch_public('http://looks-public.example',100)

def test_pdf_and_empty_scan(store):
    pdf=pymupdf.open();page=pdf.new_page();page.insert_text((72,72),'A PDF statement preserved as evidence.')
    source=ingest(store,'PDF',pdf.tobytes(),'pdf');eid=extract(store,source['id'])
    assert 'page 1' in store.db.one('SELECT locator FROM passages WHERE extraction_id=?',(eid,))['locator']
    blank=pymupdf.open();blank.new_page();source=ingest(store,'Scanned',blank.tobytes(),'pdf')
    with pytest.raises(ValueError,match='No usable text'):extract(store,source['id'])

def test_html_script_not_extracted(store):
    source=ingest(store,'HTML',b'<html><script>steal()</script><article>Evidence here.</article></html>','html');eid=extract(store,source['id'])
    text=store.db.one('SELECT text FROM passages WHERE extraction_id=?',(eid,))['text']
    assert 'Evidence here.' in text and 'steal' not in text


def test_html_main_excludes_megamenu_and_keeps_article_header(store):
    raw=b'<header>Unrelated NASA highlights</header><main><article><header>Science Objectives</header><p>Look for ancient life.</p></article></main><footer>Links</footer>'
    source=ingest(store,'NASA-shaped webpage',raw,'html');eid=extract(store,source['id'])
    text=''.join(p['text'] for p in store.db.all('SELECT text FROM passages WHERE extraction_id=?',(eid,)))
    assert text=='Science Objectives\nLook for ancient life.'


def test_chunks_preserve_text_and_avoid_midword_splits():
    from backend.ingestion import text_chunks
    text=('Perseverance studies ancient life.\n'*200)+'x'*4000
    chunks=list(text_chunks(text))
    assert ''.join(part for _,part in chunks)==text
    assert all(part==text[start:start+len(part)] and len(part)<=3000 for start,part in chunks)
    assert chunks[0][1].endswith('\n')
