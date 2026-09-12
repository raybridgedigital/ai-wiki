#!/usr/bin/env python3
"""One launcher. Run --dev for Vite; otherwise serve the built app on port 8000."""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--dev',action='store_true');args=parser.parse_args()
python=root/'.venv/bin/python'
if not python.exists():raise SystemExit('Run ./setup.sh first to install the application dependencies.')
if not args.dev and not (root/'frontend/dist/index.html').exists():raise SystemExit('Run npm run build in frontend/, or start with --dev.')
processes=[]
def stop(*_):
    for p in processes:
        if p.poll() is None:p.send_signal(signal.SIGINT)
    for p in processes:
        try:p.wait(timeout=8)
        except subprocess.TimeoutExpired:p.terminate()
signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
try:
    processes.append(subprocess.Popen([str(python),'-m','uvicorn','backend.main:app','--host','127.0.0.1','--port','8000'],cwd=root))
    if args.dev:processes.append(subprocess.Popen(['npm','run','dev'],cwd=root/'frontend'))
    print('Commonplace: http://127.0.0.1:'+('5173' if args.dev else '8000'),flush=True)
    while all(p.poll() is None for p in processes):time.sleep(.5)
finally:stop()

