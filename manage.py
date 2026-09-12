#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from backend.config import ROOT,data_dir
from backend.db import DB
from backend.storage import Store
from backend.backup import backup,restore

parser=argparse.ArgumentParser(description='Commonplace workspace maintenance')
commands=parser.add_subparsers(dest='command',required=True)
b=commands.add_parser('backup');b.add_argument('destination')
r=commands.add_parser('restore');r.add_argument('archive');r.add_argument('destination')
commands.add_parser('repair')
commands.add_parser('status')
args=parser.parse_args()
if args.command=='restore':
    restored=restore(args.archive,args.destination)
    print('Restored to',restored)
    print('Set APP_DATA_DIR to this directory before starting the app. Review the backed-up config/wiki-rules.md before copying it into the project config directory.')
else:
    store=Store(DB(data_dir()))
    if args.command=='backup':print('Created',backup(store,args.destination))
    elif args.command=='repair':
        store.materialize()
        print('Markdown synchronized from committed revisions.')
    else:
        print(json.dumps({'directory':str(store.root),'pages':store.db.one('SELECT count(*) n FROM pages')['n'],'sources':store.db.one('SELECT count(*) n FROM sources')['n']},indent=2))

