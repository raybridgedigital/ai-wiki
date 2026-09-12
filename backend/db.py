import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import create_engine

def now():
    return datetime.now(timezone.utc).isoformat()

def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"

def dump(value):
    return json.dumps(value, ensure_ascii=False)

def unpack(row):
    return dict(row) if row else None

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,title TEXT NOT NULL,type TEXT NOT NULL,path TEXT NOT NULL,hash TEXT UNIQUE NOT NULL,location TEXT,created TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',extraction_id TEXT,status TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS ingestions(id TEXT PRIMARY KEY,source_id TEXT REFERENCES sources(id),created TEXT,location TEXT);
CREATE TABLE IF NOT EXISTS extractions(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),created TEXT NOT NULL,extractor TEXT NOT NULL,hash TEXT NOT NULL,warnings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS passages(id TEXT PRIMARY KEY,extraction_id TEXT NOT NULL REFERENCES extractions(id),text TEXT NOT NULL,locator TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS passage_extraction ON passages(extraction_id);
CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY,title TEXT NOT NULL,slug TEXT UNIQUE NOT NULL,revision_id TEXT,created TEXT NOT NULL,updated TEXT NOT NULL,locked TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS names(name TEXT PRIMARY KEY,page_id TEXT NOT NULL REFERENCES pages(id));
CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY,page_id TEXT NOT NULL REFERENCES pages(id),previous_id TEXT,created TEXT NOT NULL,author TEXT NOT NULL,reason TEXT NOT NULL,change_set_id TEXT NOT NULL,doc TEXT NOT NULL,markdown TEXT NOT NULL,model TEXT NOT NULL,prompt_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS revision_page ON revisions(page_id,created);
CREATE TABLE IF NOT EXISTS citations(id TEXT PRIMARY KEY,revision_id TEXT NOT NULL REFERENCES revisions(id),label TEXT NOT NULL,block_id TEXT NOT NULL,passage_id TEXT NOT NULL REFERENCES passages(id),quote TEXT NOT NULL,UNIQUE(revision_id,label));
CREATE TABLE IF NOT EXISTS relationships(revision_id TEXT NOT NULL REFERENCES revisions(id),target_id TEXT NOT NULL REFERENCES pages(id),type TEXT NOT NULL,PRIMARY KEY(revision_id,target_id,type));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,input TEXT NOT NULL,state TEXT NOT NULL,stage TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,idempotency_key TEXT UNIQUE NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,proposal TEXT,result TEXT,error TEXT,usage TEXT NOT NULL DEFAULT '{}',change_set_id TEXT);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,created TEXT NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issues(id TEXT PRIMARY KEY,fingerprint TEXT UNIQUE NOT NULL,page_id TEXT,revision_id TEXT,kind TEXT NOT NULL,method TEXT NOT NULL,message TEXT NOT NULL,status TEXT NOT NULL,created TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS answers(id TEXT PRIMARY KEY,question TEXT NOT NULL,answer TEXT NOT NULL,created TEXT NOT NULL,evidence TEXT NOT NULL,revisions TEXT NOT NULL,usage TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS materialization(path TEXT PRIMARY KEY,hash TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(id UNINDEXED,kind UNINDEXED,title,body,tokenize='unicode61');
"""

class DB:
    def __init__(self, root):
        self.root = root
        for folder in ("database", "data/raw", "data/extracted", "wiki/pages", "recovery"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{root / 'database/knowledge.db'}", connect_args={"timeout": 20, "check_same_thread": False})
        with self.connection() as c:
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            c.commit()

    @contextmanager
    def connection(self, write=False):
        proxy = self.engine.raw_connection()
        c = proxy.driver_connection
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
            yield c
            if write:
                c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            proxy.close()

    def one(self, sql, args=()):
        with self.connection() as c:
            return unpack(c.execute(sql, args).fetchone())

    def all(self, sql, args=()):
        with self.connection() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connection(True) as c:
            c.execute(sql, args)

    def event(self, kind, message, details=None, c=None):
        values = (uid("EVT"), now(), kind, message, dump(details or {}))
        if c is not None:
            c.execute("INSERT INTO events VALUES(?,?,?,?,?)", values)
        else:
            self.execute("INSERT INTO events VALUES(?,?,?,?,?)", values)
