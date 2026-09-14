"""Login gateway for one explicitly configured project/agent.

This is an HTTP authorization boundary, not an execution sandbox. Run its target
agent under an account with exactly the filesystem/network access intended for
the colleague. No fleet credential is returned to the browser.
"""
import argparse
import asyncio
from collections import defaultdict, deque
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

from . import auth, transport
from .validation import body_bytes

COOKIE = '__Secure-agentnode_colleague'
STATIC = Path(__file__).resolve().parents[1] / 'static'


async def json_object(request, limit):
    try:
        value = json.loads(await body_bytes(request, limit))
    except (ValueError, UnicodeError):
        raise HTTPException(422, 'Expected a JSON object') from None
    if not isinstance(value, dict):
        raise HTTPException(422, 'Expected a JSON object')
    return value


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + value


def password_matches(password, encoded):
    try:
        salt, _ = encoded.split(':')
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


def load_config(path):
    path = Path(path).expanduser()
    if path.stat().st_mode & 0o077:
        raise ValueError('Portal configuration must be private (0600)')
    config = json.loads(path.read_text())
    for name in ('project', 'agent'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', config[name]):
            raise ValueError('Invalid portal scope')
    for name in ('origin', 'surface_origin'):
        parsed = urlsplit(config[name])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('Portal requires two explicit HTTPS origins')
    if config['origin'] == config['surface_origin']:
        raise ValueError('Surface must use a different origin from the login/chat page')
    if urlsplit(config['origin']).hostname != urlsplit(config['surface_origin']).hostname:
        raise ValueError('Both portal origins must use the same hostname')
    return config


class Gateway:
    def __init__(self, config):
        self.config = config
        self.attempts = defaultdict(deque)
        self.sessions = {}  # Restart and password rotation invalidate existing logins.
        self.node = config['upstream']
        self.base = '/api/projects/' + config['project']
        self.agent = self.base + '/agents/' + config['agent']

    def session(self, cookies):
        key = cookies.get(COOKIE, '')
        record = self.sessions.get(key)
        if not record or record['expires'] <= time.time():
            self.sessions.pop(key, None)
            return None
        return record

    def prune(self):
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
        self.attempts = defaultdict(deque, {k: v for k, v in self.attempts.items() if v and v[-1] > now - 900})

    async def request(self, method, path, body=None, node=None):
        try:
            response = await asyncio.to_thread(transport.request, node or self.node, method, path, body, 20)
            try:
                data = response.json()
            except ValueError:
                raise HTTPException(502, 'The agent service returned an invalid response') from None
            if response.status_code >= 400:
                raise HTTPException(response.status_code, data.get('error', 'Agent service request failed'))
            return data
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, 'The agent service is temporarily unavailable') from None

    async def status(self):
        return await self.request('GET', self.agent + '/status')

    async def surface_node(self):
        status = await self.status()
        workspace = status.get('surface_ws')
        if not workspace:
            raise HTTPException(404, 'This agent has no Surface workspace yet')
        return dict(self.config['surface_upstream'], token=auth.capability(
            self.node['token'], 'surface', 'presenter', ws=[workspace])), workspace


def create_app(config, *, surface=False, gateway=None):
    gateway = gateway or Gateway(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = gateway
    origin = config['surface_origin' if surface else 'origin']

    def trusted(headers):
        # Forwarded headers are never used to decide which browser origin is trusted.
        return (headers.get('host') == urlsplit(origin).netloc
                and headers.get('origin') in (None, origin)
                and headers.get('sec-fetch-site') != 'cross-site')

    @app.exception_handler(HTTPException)
    async def error(request, exc):
        return JSONResponse({'error': exc.detail}, status_code=exc.status_code)

    class Boundary(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not trusted(request.headers):
                return JSONResponse({'error': 'Untrusted browser origin'}, status_code=403)
            public = not surface and request.url.path in ('/', '/api/login', '/static/portal.css', '/static/portal.js')
            record = gateway.session(request.cookies)
            if not public and not record:
                return JSONResponse({'error': 'Sign in to continue'}, status_code=401)
            if request.method not in ('GET', 'HEAD') and request.url.path != '/api/login':
                if surface or not record or not hmac.compare_digest(request.headers.get('x-csrf-token', ''), record['csrf']):
                    return JSONResponse({'error': 'Invalid form session'}, status_code=403)
            response = await call_next(request)
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            if not surface:
                response.headers['Content-Security-Policy'] = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-src " + config['surface_origin'] + "; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            else:
                response.headers['Content-Security-Policy'] = 'frame-ancestors ' + config['origin'] + ' ' + config['surface_origin']
            return response

    app.add_middleware(Boundary)

    async def relay(ws, target, path, record, workspace=None):
        await ws.accept()
        try:
            async with transport.websocket(target, path, max_size=2**22) as upstream:
                async def receive():
                    while True:
                        # Observation only. Surface output is controlled through agent chat.
                        message = await ws.receive_json()
                        if message.get('type') == 'ping':
                            await ws.send_json({'type': 'pong'})
                        elif workspace and message.get('type') == 'resync':
                            await upstream.send(json.dumps({'type': 'resync'}))
                        else:
                            await ws.send_json({'type': 'error', 'error': 'Use the conversation to request changes'})

                async def send():
                    async for message in upstream:
                        if not gateway.session(ws.cookies):
                            break
                        if isinstance(message, bytes):
                            await ws.send_bytes(message)
                        else:
                            await ws.send_text(message)

                async def expires():
                    while gateway.session(ws.cookies):
                        await asyncio.sleep(min(5, max(0.1, record['expires'] - time.time())))
                        if workspace and (await gateway.status()).get('surface_ws') != workspace:
                            return

                tasks = [asyncio.create_task(f()) for f in (receive, send, expires)]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
        finally:
            try:
                await ws.close(code=4401 if not gateway.session(ws.cookies) else 1000)
            except (RuntimeError, WebSocketDisconnect):
                pass

    if surface:
        @app.websocket('/ws')
        async def surface_stream(ws: WebSocket):
            record = gateway.session(ws.cookies)
            if not record or not trusted(ws.headers):
                await ws.close(code=4403)
                return
            try:
                node, workspace = await gateway.surface_node()
                # Client-supplied workspace, access token and presentation scope are ignored.
                path = '/ws?' + urlencode({'ws': workspace, 'view': 'presenter', 'presentation': 'portal'})
                await relay(ws, node, path, record, workspace=workspace)
            except HTTPException:
                await ws.close(code=4404)

        @app.get('/{path:path}')
        async def surface_read(path: str, req: Request):
            # The upstream enforces workspace asset namespaces as a second boundary.
            if path and not path.startswith(('shell/', 'assets/', 'apps/')) and path not in ('__surface/bridge.js', 'api/workspaces', 'favicon.ico'):
                raise HTTPException(403, 'This page is outside the assigned workspace')
            node, workspace = await gateway.surface_node()
            query = dict(req.query_params)
            query.pop('access', None)
            query.pop('token', None)
            query['ws'] = workspace
            try:
                response = await asyncio.to_thread(transport.request, node, 'GET', '/' + path + '?' + urlencode(query), None, 20)
            except Exception:
                raise HTTPException(502, 'Surface is temporarily unavailable') from None
            return Response(response.content, status_code=response.status_code,
                            media_type=response.headers.get('content-type', 'application/octet-stream'))
        return app

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'portal.html')

    @app.get('/static/{path:path}')
    async def static(path: str):
        if path not in ('portal.css', 'portal.js', 'history-view.js', 'vendor/marked.js', 'vendor/purify.min.js'):
            raise HTTPException(404, 'Unknown asset')
        return FileResponse(STATIC / path)

    @app.post('/api/login')
    async def login(req: Request):
        if req.url.scheme != 'https' or req.headers.get('origin') != origin:
            raise HTTPException(403, 'Use the HTTPS login page')
        body = await json_object(req, limit=4096)
        username, password = body.get('username'), body.get('password')
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
            raise HTTPException(401, 'Invalid username or password')
        gateway.prune()
        # A global account bound also covers a proxy that hides client addresses.
        attempt = gateway.attempts['account']
        if len(attempt) >= 10:
            raise HTTPException(429, 'Too many attempts. Try again in 15 minutes.')
        attempt.append(time.time())
        correct = await asyncio.to_thread(password_matches, password, config['password_hash'])
        if not correct or not hmac.compare_digest(username.encode(), config['username'].encode()):
            raise HTTPException(401, 'Invalid username or password')
        attempt.clear()
        if len(gateway.sessions) >= 16:
            gateway.sessions.pop(next(iter(gateway.sessions)))
        key = secrets.token_urlsafe(48)
        gateway.sessions[key] = {'expires': time.time() + 8 * 3600, 'csrf': secrets.token_urlsafe(32)}
        response = JSONResponse({'ok': True})
        response.set_cookie(COOKIE, key, secure=True, httponly=True, samesite='strict', max_age=8 * 3600, path='/')
        return response

    @app.post('/api/logout')
    async def logout(req: Request):
        gateway.sessions.pop(req.cookies.get(COOKIE), None)
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/api/me')
    async def me(req: Request):
        return {'name': config.get('name', 'Agent workspace'), 'username': config['username'],
                'csrf': gateway.session(req.cookies)['csrf'], 'surface_origin': config['surface_origin']}

    @app.get('/api/status')
    async def status():
        current = await gateway.status()
        # No project paths, configuration, provider auth or other agents are returned.
        return {k: current[k] for k in ('status', 'alive', 'running', 'approval', 'model', 'queued', 'active', 'surface_ws') if k in current}

    @app.get('/api/history')
    async def history(req: Request):
        query = dict(req.query_params)
        if set(query) - {'before', 'after', 'event', 'limit'} or any(len(v) > 256 for v in query.values()):
            raise HTTPException(422, 'Invalid history cursor')
        return await gateway.request('GET', gateway.agent + '/history?' + urlencode(query))

    @app.post('/api/send')
    async def send(req: Request):
        body = await json_object(req, limit=128 * 1024)
        if set(body) - {'text', 'request_id'}:
            raise HTTPException(422, 'Only text and a request ID are accepted')
        if not isinstance(body.get('text'), str) or not body['text'].strip() or len(body['text']) > 100000:
            raise HTTPException(422, 'Enter a message of at most 100000 characters')
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', request_id):
            raise HTTPException(422, 'Invalid request ID')
        current = await gateway.status()
        if not current.get('alive'):
            raise HTTPException(409, 'Jaime must start this agent before it can receive messages')
        return await gateway.request('POST', gateway.base + '/send', {
            'text': body['text'], 'request_id': request_id, 'expected_agent': config['agent'], 'request_source': 'chat', 'require_running': True,
            'origin': config['username']})

    @app.post('/api/interrupt')
    async def interrupt():
        return await gateway.request('POST', gateway.base + '/interrupt', {'expected_agent': config['agent']})

    @app.websocket('/ws/chat')
    async def chat(ws: WebSocket):
        record = gateway.session(ws.cookies)
        if not record or not trusted(ws.headers):
            await ws.close(code=4403)
            return
        query = {k: v for k, v in ws.query_params.items() if k == 'after' and len(v) <= 256}
        path = '/ws/projects/' + config['project'] + '/agents/' + config['agent'] + '/chat?' + urlencode(query)
        await relay(ws, gateway.node, path, record)

    return app


async def serve(config):
    # Both origins share the in-memory login registry, but have separate routers.
    gateway = Gateway(config)
    servers = []
    for surface in (False, True):
        origin = urlsplit(config['surface_origin' if surface else 'origin'])
        server = uvicorn.Server(uvicorn.Config(create_app(config, surface=surface, gateway=gateway),
            host=config.get('bind', '127.0.0.1'), port=origin.port or 443,
            ssl_certfile=config['cert'], ssl_keyfile=config['key'], proxy_headers=False,
            access_log=False, ws_max_size=131072))
        servers.append(server.serve())
    await asyncio.gather(*servers)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    asyncio.run(serve(load_config(args.config)))
