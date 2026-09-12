"""Consistent, offline-readable backups. Restore into a new directory only."""
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from .config import ROOT
from .storage import sha,Store
from .db import DB

def backup(store,destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Backup destination already exists')
    with tempfile.TemporaryDirectory() as temp:
        snapshot=Path(temp)/'knowledge.db'
        # SQLite's backup API provides a transactionally consistent database image.
        with store.db.connection() as source:
            target=sqlite3.connect(snapshot)
            try:source.backup(target)
            finally:target.close()
        db=sqlite3.connect(snapshot)
        raw=[r[0] for r in db.execute('SELECT path FROM sources')]
        extraction=[f'data/extracted/{r[0]}.json' for r in db.execute('SELECT id FROM extractions')]
        db.close()
        files={name:store.root/name for name in raw+extraction}
        files['database/knowledge.db']=snapshot
        files['config/wiki-rules.md']=ROOT/'config/wiki-rules.md'
        hashes={name:sha(path.read_bytes()) for name,path in files.items()}
        destination.parent.mkdir(parents=True,exist_ok=True)
        tempzip=destination.with_suffix(destination.suffix+'.partial')
        with zipfile.ZipFile(tempzip,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,path in files.items():archive.write(path,name)
            archive.writestr('manifest.json',json.dumps({'format':1,'hashes':hashes},indent=2))
        tempzip.replace(destination)
    return destination

def restore(archive_path,destination):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Restore destination must be a new directory; existing data is never overwritten')
    with zipfile.ZipFile(archive_path) as archive:
        infos=archive.infolist()
        if len(infos)>100000 or sum(i.file_size for i in infos)>10*1024**3:raise ValueError('Backup exceeds restore safety limits')
        manifest=json.loads(archive.read('manifest.json'))
        if manifest.get('format')!=1:raise ValueError('Unsupported backup format')
        for name,digest in manifest['hashes'].items():
            path=Path(name)
            if path.is_absolute() or '..' in path.parts or str(path) not in archive.namelist():raise ValueError('Unsafe backup path')
            if sha(archive.read(name))!=digest:raise ValueError('Backup integrity check failed: '+name)
        destination.mkdir(parents=True)
        for name in manifest['hashes']:
            path=destination/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.read(name))
    store=Store(DB(destination))
    with store.db.connection(True) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Restored database failed integrity validation')
        if c.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Restored database has broken references')
    store.materialize()
    return destination

