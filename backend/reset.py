"""Explicit workspace reset, serialized against HTTP requests and worker jobs."""
from contextlib import contextmanager
import json
import os
import shutil
import threading
from dotenv import set_key
from .config import ROOT,PROVIDERS
from .db import dump

class WorkspaceGate:
    def __init__(self):
        self.lock=threading.Lock();self.active=0;self.resetting=False

    @contextmanager
    def activity(self):
        with self.lock:
            if self.resetting:raise ValueError('Workspace reset is in progress. Try again shortly.')
            self.active+=1
        try:yield
        finally:
            with self.lock:self.active-=1

    @contextmanager
    def reset(self):
        with self.lock:
            if self.resetting or self.active:raise ValueError('The workspace is busy. Wait for processing and other requests to finish, then reset again.')
            self.resetting=True
        try:yield
        finally:
            with self.lock:self.resetting=False

TABLES=('citations','relationships','names','revisions','pages','passages','extractions','ingestions','sources','jobs','answers','issues','events','materialization','search_index')

def finish_reset(store,forget_connections):
    from . import oauth
    # Marker survives failures; startup completes cleanup before processing resumes.
    marker=store.root/'database/reset-pending.json'
    if forget_connections:
        oauth.disconnect(store)
        env=ROOT/'.env'
        for provider in PROVIDERS.values():
            name=provider['key_env']
            if env.exists():set_key(str(env),name,'')
            os.environ.pop(name,None)
        if env.exists():env.chmod(0o600)
    with store.db.connection(True) as c:
        c.execute('PRAGMA secure_delete=ON')
        for table in TABLES:c.execute('DELETE FROM '+table)
        if forget_connections:c.execute('DELETE FROM settings')
        c.execute("INSERT INTO settings VALUES('external_models_enabled','false') ON CONFLICT(key) DO UPDATE SET value='false'")
    for name in ('data','wiki','recovery'):
        path=store.root/name
        if path.is_symlink():path.unlink()
        elif path.exists():shutil.rmtree(path)
    for name in ('data/raw','data/extracted','wiki/pages','recovery'):
        (store.root/name).mkdir(parents=True,exist_ok=True)
    with store.db.connection() as c:
        c.execute('VACUUM')
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    marker.unlink(missing_ok=True)

def resume_reset(store):
    marker=store.root/'database/reset-pending.json'
    if marker.exists():finish_reset(store,json.loads(marker.read_text())['forget_connections'])

def reset_workspace(store,forget_connections=False):
    with store.gate.reset():
        if store.db.one("SELECT id FROM jobs WHERE state IN ('RUNNING','COMMITTED') LIMIT 1"):
            raise ValueError('Wait for running jobs and committed-output recovery to finish before resetting.')
        marker=store.root/'database/reset-pending.json'
        with marker.open('w') as f:
            f.write(dump({'forget_connections':forget_connections}));f.flush();os.fsync(f.fileno())
        try:finish_reset(store,forget_connections)
        except Exception:
            # Keep ordinary operations blocked until restart completes cleanup.
            store.reset_failed=True
            raise ValueError('Reset cleanup was interrupted. Restart the app to finish it before adding new data.')
