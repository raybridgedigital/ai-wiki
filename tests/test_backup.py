from backend.backup import backup,restore
from backend.storage import Store
from backend.db import DB

def test_complete_backup_restores_evidence_history_and_search(store,change,evidence,tmp_path):
    result=store.save([change]);page=result['pages'][0]
    destination=tmp_path/'backup.zip'
    backup(store,destination)
    restored=restore(destination,tmp_path/'restored')
    new=Store(DB(restored))
    assert new.page(page['id'])['citations'][0]['quote']==evidence['ref']['quote']
    assert new.search('Northstar')
    assert (restored/evidence['source']['path']).read_bytes()==(store.root/evidence['source']['path']).read_bytes()
    assert (restored/'wiki/index.md').exists()

