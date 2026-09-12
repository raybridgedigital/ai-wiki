import logging

class OAuthAccessFilter(logging.Filter):
    def filter(self,record):
        if isinstance(record.args,tuple) and len(record.args)==5:
            args=list(record.args)
            if isinstance(args[2],str) and args[2].startswith('/api/oauth/callback'):
                args[2]='/api/oauth/callback';record.args=tuple(args)
        return True

logging.getLogger('uvicorn.access').addFilter(OAuthAccessFilter())

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse,FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .config import data_dir,ROOT
from .db import DB
from .storage import Store,Conflict
from .jobs import Worker
from .api import router

def create_app(root=None,start_worker=True,provider_factory=None):
    store=Store(DB(Path(root) if root else data_dir()))
    worker=Worker(store,provider_factory) if provider_factory else Worker(store)
    @asynccontextmanager
    async def lifespan(app):
        if start_worker:worker.start()
        yield
        if start_worker:worker.stop()
    app=FastAPI(title='Commonplace Wiki',lifespan=lifespan)
    app.state.store=store;app.state.worker=worker
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver'])
    @app.middleware('http')
    async def boundary(request:Request,call_next):
        if request.url.path.startswith('/api') and request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('origin')
            if origin and origin not in ('http://127.0.0.1:5173','http://localhost:5173','http://127.0.0.1:8000','http://localhost:8000','http://testserver'):
                return JSONResponse({'error':'Cross-origin mutation rejected'},403)
            if request.headers.get('x-commonplace')!='local':
                return JSONResponse({'error':'Missing local application request header'},403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        return response
    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse({'error':str(exc)},status_code=409 if isinstance(exc,Conflict) else 400)
    @app.exception_handler(KeyError)
    async def missing(request,exc):return JSONResponse({'error':str(exc)},status_code=404)
    app.include_router(router)
    @app.get('/{path:path}')
    def ui(path:str):
        if path.startswith('api/'):return JSONResponse({'error':'API route not found'},404)
        dist=ROOT/'frontend/dist'
        target=(dist/path).resolve()
        if target.is_relative_to(dist.resolve()) and target.is_file():return FileResponse(target)
        if (dist/'index.html').exists():return FileResponse(dist/'index.html')
        return JSONResponse({'message':'Frontend not built. Run the development launcher.'})
    return app

app=create_app()
