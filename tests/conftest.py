import pytest
from backend.db import DB
from backend.storage import Store
from backend.ingestion import ingest,extract

@pytest.fixture
def store(tmp_path):return Store(DB(tmp_path))

@pytest.fixture
def evidence(store):
    source=ingest(store,'Northstar Voice','Northstar Voice answers calls. Setup costs $500 for Basic in September 2026.'.encode())
    eid=extract(store,source['id'])
    p=store.db.one('SELECT * FROM passages WHERE extraction_id=?',(eid,))
    return {'source':source,'passage':p,'ref':{'passage_id':p['id'],'quote':'Northstar Voice answers calls.'}}

@pytest.fixture
def change(evidence):return {'page_id':None,'expected_revision':None,'title':'Northstar Voice','aliases':['Northstar Receptionist'],'tags':['voice'],'blocks':[{'id':'overview','heading':'Overview','content':'Northstar Voice answers calls.','kind':'Sourced','evidence':[evidence['ref']]}],'related':[],'reason':'Initial evidence'}

@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path,monkeypatch):
    monkeypatch.setattr('backend.oauth.CREDENTIALS_ROOT',tmp_path.parent/(tmp_path.name+'-private'))
