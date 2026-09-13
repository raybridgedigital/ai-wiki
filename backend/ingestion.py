import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit,urljoin
from bs4 import BeautifulSoup
import pymupdf
from .db import uid,now,dump
from .storage import sha

def fetch_public(url, limit):
    for _ in range(5):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Use a public HTTP or HTTPS URL without credentials')
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme=='https' else 80)
        addresses = socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0].split('%')[0]).is_global for a in addresses):
            raise ValueError('Only public internet addresses are allowed')
        # Connect to the already validated IP, with the original hostname for TLS.
        address = addresses[0][4]
        sock = socket.socket(addresses[0][0],socket.SOCK_STREAM)
        sock.settimeout(20)
        conn = http.client.HTTPConnection(host,port,timeout=20)
        try:
            sock.connect(address)
            if parsed.scheme=='https':
                sock = ssl.create_default_context().wrap_socket(sock,server_hostname=host)
            conn.sock = sock
            path = (parsed.path or '/') + (('?'+parsed.query) if parsed.query else '')
            conn.request('GET',path,headers={'Host':parsed.netloc,'User-Agent':'Commonplace/0.1','Accept-Encoding':'identity','Accept':'text/html,text/plain'})
            response = conn.getresponse()
            if response.status in (301,302,303,307,308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Redirect has no destination')
                url = urljoin(url,location)
                continue
            if response.status != 200:
                raise ValueError(f'Website returned HTTP {response.status}')
            if response.getheader('Content-Encoding','identity') != 'identity':
                raise ValueError('Compressed webpage response is unsupported; paste the text instead')
            content_type = response.getheader('Content-Type','')
            if not any(x in content_type for x in ('text/html','text/plain','application/xhtml')):
                raise ValueError('URL must return a webpage or plain text')
            raw = response.read(limit+1)
            if len(raw)>limit:
                raise ValueError('Webpage exceeds the download limit')
            return raw,url,content_type
        finally:
            conn.close()
            sock.close()
    raise ValueError('Too many redirects')

def ingest(store, title, raw, kind='text', location='', metadata=None):
    if not raw:
        raise ValueError('The source is empty')
    if len(raw)>store.settings()['max_upload_bytes']:
        raise ValueError('Source exceeds the upload size limit')
    digest = sha(raw)
    duplicate = store.db.one('SELECT * FROM sources WHERE hash=?',(digest,))
    if duplicate:
        store.db.execute('INSERT INTO ingestions VALUES(?,?,?,?)',(uid('INGEST'),duplicate['id'],now(),location))
        return {**duplicate,'duplicate':True}
    sid = uid('SRC')
    extension = {'pdf':'pdf','html':'html','markdown':'md','text':'txt'}.get(kind,'txt')
    relative = f'data/raw/{sid}.{extension}'
    with (store.root/relative).open('xb') as f:
        f.write(raw)
    store.db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?)',(sid,title or 'Untitled source',kind,relative,digest,location,now(),dump(metadata or {}),None,'NEW',None))
    store.db.event('source',f'Added {title}',{'source_id':sid})
    return store.db.one('SELECT * FROM sources WHERE id=?',(sid,))

def extract(store, sid):
    source = store.db.one('SELECT * FROM sources WHERE id=?',(sid,))
    if not source:
        raise KeyError('Source not found')
    raw = (store.root/source['path']).read_bytes()
    if sha(raw)!=source['hash']:
        raise ValueError('Original source hash mismatch. Restore the original from backup.')
    units,warnings = [],[]
    extractor = 'plain-text-v1'
    if source['type']=='pdf':
        extractor = 'pymupdf-'+pymupdf.VersionBind
        with pymupdf.open(stream=raw,filetype='pdf') as pdf:
            if pdf.is_encrypted:
                raise ValueError('Encrypted PDF: upload an unlocked copy')
            if len(pdf)>store.settings()['max_pdf_pages']:
                raise ValueError('PDF exceeds the page limit')
            for i,page in enumerate(pdf):
                text = page.get_text()
                if not text.strip():
                    warnings.append(f'Page {i+1} has no extracted text; images were not interpreted.')
                units.append((text,f'page {i+1}'))
    elif source['type']=='html':
        extractor = 'beautifulsoup-html-v2'
        soup=BeautifulSoup(raw,'html.parser')
        for tag in soup(['script','style','nav','footer','noscript','iframe']):
            tag.decompose()
        content = soup.find('main') or soup.find(attrs={'role':'main'}) or soup.find('article') or soup
        units=[(content.get_text('\n',strip=True),'webpage text')]
        warnings.append('Snapshot contains fetched HTML; JavaScript content and images are not interpreted.')
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('Text files must use UTF-8 encoding')
        units=[(text,'text')]
    if not any(t.strip() for t,_ in units):
        raise ValueError('No usable text found. Scanned PDFs need OCR before upload.')
    if sum(len(t) for t,_ in units)>store.settings()['max_text_chars']:
        raise ValueError('Extracted text exceeds the configured limit. Split the document or raise the limit.')
    eid=uid('EXT')
    passages=[]
    for text,location in units:
        for start,part in text_chunks(text):
            if part.strip():
                passages.append((f'{eid}:P-{len(passages)+1:03}',eid,part,f'{location}, characters {start}–{start+len(part)}'))
    snapshot={'id':eid,'source_id':sid,'extractor':extractor,'passages':[{'id':p[0],'text':p[2],'locator':p[3]} for p in passages]}
    (store.root/'data/extracted'/f'{eid}.json').write_text(dump(snapshot),encoding='utf8')
    with store.db.connection(True) as c:
        c.execute('INSERT INTO extractions VALUES(?,?,?,?,?,?)',(eid,sid,now(),extractor,sha(dump(units)),dump(warnings)))
        c.executemany('INSERT INTO passages VALUES(?,?,?,?)',passages)
        c.execute("UPDATE sources SET extraction_id=?,status='EXTRACTED',error=NULL WHERE id=?",(eid,sid))
        c.execute("DELETE FROM search_index WHERE id=?",(sid,))
        c.execute('INSERT INTO search_index VALUES(?,?,?,?)',(sid,'SOURCE',source['title'],'\n'.join(p[2] for p in passages)))
    return eid


def text_chunks(text, limit=3000):
    """Keep exact offsets while preferring paragraph/sentence boundaries."""
    start=0
    while start<len(text):
        end=min(start+limit,len(text))
        if end<len(text):
            lower=start+limit//2
            boundary=text.rfind('\n',lower,end)
            if boundary<0:
                boundary=text.rfind('. ',lower,end)
            if boundary<0:
                boundary=text.rfind(' ',lower,end)
            if boundary>=0:
                end=boundary+1
        yield start,text[start:end]
        start=end
