"""agentnode server: projects (agents) on this machine, host tools, Surface bridge, voice,
and — on the control node — the fleet directory, discovery, proxy and the control UI."""
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from . import auth as browser_auth
from . import transport, policy, history as transcripts
from .broadcast import send_all
from .screen_control import ScreenControl, geometry as screen_geometry, input_message
from .validation import json_object, body_bytes, project_fields
from . import config, voice as voicemod
try:
    from . import voicestream          # needs numpy + faster-whisper (control node); plain nodes run without it
except ImportError:
    voicestream = None
from .host import ScreenStreamer, platform_info
from .session import ProjectSession
from .delivery import DeliveryError
from .presentation import StageState, StageConflict, DisplayRegistry, DisplayChoice, clean_tiles
from .storage import read_json, write_json
from urllib.parse import quote, urlsplit, urlencode
from .util import http_json, log

VERSION = "0.1.0"
NODE = config.node()
if NODE.get('lodge'):
    os.environ['LODGE_MODE'] = '1'
if os.environ.get('LODGE_MODE') == '1':
    os.environ['CODEX_HOME'] = str(config.HOME / 'codex')
    for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN'):
        os.environ.pop(key, None)
STATIC = config.REPO / "static"
HUB_URL = f"http://127.0.0.1:{NODE['hub_port']}" if NODE.get("surface_dir") else None

app = FastAPI()
if os.environ.get('LODGE_MODE') == '1':
    from .lodge import router as lodge_router
    app.include_router(lodge_router)

@app.exception_handler(HTTPException)
async def http_error(request,exc):
    return JSONResponse({'error':exc.detail,'ok':False},status_code=exc.status_code,headers=exc.headers)

@app.exception_handler(DeliveryError)
async def delivery_error(request, exc):
    return JSONResponse({'error': str(exc), 'ok': False}, status_code=422)
@app.exception_handler(policy.PolicyError)
async def policy_error(request, exc):
    return JSONResponse({'error':str(exc),'ok':False}, status_code=exc.status)

sessions: dict[str, ProjectSession] = {}
screen: ScreenStreamer | None = None
control_clients: set = set()
# Short-lived replay for the browser that made a voice request. This contains
# only Stage composition, never speech, chat or credentials.
voice_stages: dict[str, tuple[float, dict]] = {}
human_screen=ScreenControl()
presentation_stage = StageState(config.HOME / 'presentation-stage.json')
presenter_displays = DisplayRegistry(config.HOME / 'presenter-displays.json')
presentation_stage.migrate_displays(presenter_displays.records)
if presentation_stage.revision == 0 and not presentation_stage.tiles and not presentation_stage.outputs:
    initial_conductor = next((p for p in config.projects() if p.get('conductor')),None)
    if initial_conductor:
        presentation_stage.replace([{'node':NODE['name'],'project':initial_conductor['id'],'kind':'fleet','pinned':False}],0)
LOOP: asyncio.AbstractEventLoop | None = None
_lifecycle_users = 0
_lifecycle_lock = asyncio.Lock()


# --------------------------------------------------------------------------
# Browser origins are checked independently of authentication.
# --------------------------------------------------------------------------
PUBLIC = ("/", "/healthz", "/api/node/info", "/static/", "/favicon.ico")


def presenter_claims(headers, query):
    from http.cookies import SimpleCookie
    value=headers.get('x-agentnode-token') or query.get('token')
    if not value:
        try:
            jar=SimpleCookie();jar.load(headers.get('cookie',''));value=jar['agentnode_presenter'].value
        except Exception:return None
    data=browser_auth.claims(value,NODE['token'],'conductor')
    return data if data and data.get('role')=='presenter' else None


def presenter_tiles(grant):
    return presentation_stage.snapshot(grant['display'])['tiles']


def presenter_path(grant, path, method, query):
    if method=='GET' and path in ('/api/control/tree','/api/control/displays','/api/auth/check','/api/node/info'):return True
    if method=='GET' and path=='/api/control/presentation':return query.get('display')==grant['display']
    if method=='DELETE' and path=='/api/auth/session':return True
    if method=='POST' and path in ('/api/auth/session','/api/control/presenter/surface-access'):return True
    for tile in presenter_tiles(grant):
        if tile['kind']=='screen':
            prefix='' if tile['node']==NODE['name'] else '/n/'+tile['node']
            if path==prefix+'/ws/screen':return True
    return False


def _authorized(headers, query, client_host) -> bool:
    if presenter_claims(headers,query):return True
    # Transitional compatibility for the external local Surface bridge/MCPs.
    # Never let a browser's arbitrary Host or a forwarded proxy inherit it.
    try: local_host = urlsplit('//'+headers.get('host','')).hostname
    except ValueError: local_host = None
    if (os.environ.get('LODGE_MODE') != '1' and client_host in ("127.0.0.1", "::1", "localhost")
        and local_host in ('127.0.0.1','::1','localhost')
        and not any(headers.get(k) for k in ('forwarded','x-forwarded-for','x-forwarded-host'))):
        return True
    tok = headers.get("x-agentnode-token") or query.get("token") or ""
    return browser_auth.valid_token(tok, NODE["token"]) or browser_auth.valid_cookie(headers, NODE["token"])


def _trusted_browser(headers, scheme):
    if headers.get('sec-fetch-site') == 'cross-site': return False
    origin = headers.get('origin')
    if origin is None: return True  # native clients and MCP callers have no browser origin
    scheme = {'ws':'http','wss':'https'}.get(scheme,scheme)
    try:
        actual = urlsplit(origin)
        expected = urlsplit(scheme+'://'+headers.get('host',''))
        return (actual.scheme == expected.scheme and actual.hostname is not None
                and actual.hostname == expected.hostname
                and (actual.port or (443 if actual.scheme=='https' else 80)) == (expected.port or (443 if scheme=='https' else 80))
                and not actual.username and not actual.password and actual.path in ('','/')
                and not actual.query and not actual.fragment)
    except ValueError: return False


class Auth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        p = request.url.path
        grant=presenter_claims(request.headers,request.query_params)
        request.state.presenter=grant
        if grant and p not in PUBLIC and not p.startswith('/static/') and not presenter_path(grant,p,request.method,request.query_params):
            return JSONResponse({'error':'This link only permits viewing its assigned presentation'},status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and p != "/api/auth/session" and (config.HOME / "deployment-maintenance.json").exists():
            return JSONResponse({"error": "Deployment in progress; retry shortly"}, status_code=503, headers={"Retry-After": "15"})
        if p not in PUBLIC and not p.startswith('/static/') and not _trusted_browser(request.headers,request.url.scheme):
            return JSONResponse({'error':'untrusted browser origin'},status_code=403)
        if p in PUBLIC or p.startswith("/static/") or _authorized(request.headers, request.query_params, request.client.host if request.client else ""):
            return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)


app.add_middleware(Auth)


async def ws_authorized(ws: WebSocket) -> bool:
    grant=presenter_claims(ws.headers,ws.query_params)
    if grant and not presenter_path(grant,ws.url.path,'GET',ws.query_params):
        await ws.close(code=4403);return False
    if not _trusted_browser(ws.headers,ws.url.scheme):
        await ws.close(code=4403)
        return False
    if _authorized(ws.headers, ws.query_params, ws.client.host if ws.client else ""):
        return True
    await ws.close(code=4401)
    return False


@app.post('/api/auth/session')
async def browser_session(req: Request):
    grant=presenter_claims(req.headers,req.query_params)
    if req.url.scheme != 'https' or (not grant and not browser_auth.valid_token(req.headers.get('x-agentnode-token'), NODE['token'])):
        return JSONResponse({'error':'HTTPS and a valid node token are required'},status_code=401)
    response=JSONResponse({'ok':True},headers={'Cache-Control':'no-store'})
    if grant:
        response.set_cookie('agentnode_presenter',req.headers.get('x-agentnode-token'),max_age=21600,secure=True,httponly=True,samesite='strict',path='/')
        return response
    response.set_cookie(browser_auth.COOKIE,browser_auth.issue(NODE['token'],req.headers.get('host','')),
                        max_age=12*3600,secure=True,httponly=True,samesite='strict',path='/')
    return response


@app.delete('/api/auth/session')
async def browser_logout():
    response=JSONResponse({'ok':True})
    response.delete_cookie(browser_auth.COOKIE,secure=True,httponly=True,samesite='strict')
    response.delete_cookie('agentnode_presenter',secure=True,httponly=True,samesite='strict')
    return response


@app.get('/api/auth/check')
async def browser_auth_check():
    """Protected, body-free check for a configured local Surface reverse proxy.

    The proxy must forward the browser's cookie and original control authority,
    with X-Forwarded-For set so it cannot inherit loopback MCP authentication.
    """
    return Response(status_code=204, headers={'Cache-Control': 'no-store'})


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
async def _resume_after_maintenance(s):
    while (config.HOME / 'deployment-maintenance.json').exists(): await asyncio.sleep(1)
    if s.desired_running and policy.approval(s.agent,s.project)=='approved':
        try: await s.start()
        except DeliveryError as exc: log('Deferred resume:',exc)


@app.on_event("startup")
async def _startup():
    global _lifecycle_users
    async with _lifecycle_lock:
        await _start_runtime()
        _lifecycle_users += 1


async def _start_runtime():
    global screen, LOOP
    if LOOP is not None:
        return
    LOOP = asyncio.get_event_loop()
    screen = ScreenStreamer(LOOP, fps=NODE["fps"], frame_w=NODE["frame_width"], jpeg_q=NODE.get("jpeg_quality",65), backend=config.host_backend(NODE)) if NODE.get("host_tools", True) else None
    for p in config.projects():
        s = ProjectSession(LOOP, p, NODE, HUB_URL)
        sessions[p["id"]] = s
        if s.desired_running:
            try: await s.start()
            except policy.PolicyError:
                _relays['__resume_'+p['id']] = asyncio.create_task(_resume_after_maintenance(s))
    _prev_status: dict = {}

    def _on_status(sess):
        prev = _prev_status.get(sess.project["id"])
        _prev_status[sess.project["id"]] = sess.status
        if prev == "working" and sess.status != "working":
            if voicestream:
                voicestream.on_turn_end(sess.project["id"])
        asyncio.ensure_future(notify_control({
            "type": "project_status", "node": NODE["name"], "project": sess.project["id"], "status": sess.status,
            "tool": sess.current_tool, "alive": bool(sess.proc and sess.proc.poll() is None), "agent": sess.active_id, "ts": time.time()}))
    ProjectSession.on_status = _on_status
    _unused = lambda sess: asyncio.ensure_future(notify_control({
        "type": "project_status", "node": NODE["name"], "project": sess.project["id"], "status": sess.status,
        "tool": sess.current_tool, "alive": bool(sess.proc and sess.proc.poll() is None), "agent": sess.active_id, "ts": time.time()}))
    if NODE.get("control"):
        if os.environ.get('LODGE_MODE') == '1':
            from . import lodge_surface
            await lodge_surface.start()
        else:
            threading.Thread(target=voicemod.load_wake_model, daemon=True).start()
        _start_relays()
        _restore_watches()
        _relays["__watch_supervisor"] = asyncio.create_task(_watch_supervisor())
    log(f"agentnode '{NODE['name']}' v{VERSION} · {len(sessions)} project(s) · control={NODE.get('control')}")


@app.on_event('shutdown')
async def _shutdown():
    """HTTP and HTTPS share one runtime; release it after the final listener stops."""
    global _lifecycle_users, LOOP, screen
    async with _lifecycle_lock:
        _lifecycle_users = max(0, _lifecycle_users - 1)
        if _lifecycle_users or LOOP is None: return
        if os.environ.get('LODGE_MODE') == '1' and NODE.get('control'):
            from . import lodge_surface
            await lodge_surface.stop()
        ProjectSession.on_status = None
        tasks = list(_relays.values()) + list(_watches.values())
        if voicestream:
            for vs in list(voicestream.SESSIONS):
                tasks.extend(vs.tasks)
                vs.close()
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _relays.clear()
        _watches.clear()
        outcomes = await asyncio.gather(*(s.stop(preserve_running=True) for s in sessions.values()),return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome,Exception): log('session shutdown failed:',outcome)
        sessions.clear()
        if screen:
            await screen.close()
            screen = None
        await asyncio.gather(*(asyncio.wait_for(ws.close(code=1001),2) for ws in list(control_clients)),return_exceptions=True)
        control_clients.clear()
        LOOP = None


def get_session(pid: str) -> ProjectSession | None:
    return sessions.get(pid) if isinstance(pid,str) else None


# --------------------------------------------------------------------------
# node + projects
# --------------------------------------------------------------------------
def release_identity():
    manifest=read_json(config.REPO/'release-manifest.json',{})
    controller=read_json(Path.home()/'.local/share/agentnode-deploy/status.json',{})
    return {'revision':manifest.get('revision'),'controller_version':controller.get('controller_version'),'deployment':controller.get('status')}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "name": NODE["name"], "projects": len(sessions), "release":release_identity()}


@app.get("/api/node/info")
async def node_info():
    return {"name": NODE["name"], "version": VERSION, "platform": platform_info(), "control": bool(NODE.get("control")),
            "wake_word": NODE.get("wake_word"), "port": NODE["port"], "tls_port": NODE["tls_port"], "host_tools": bool(screen),
            "hub_port": NODE["hub_port"] if NODE.get("surface_dir") else None,
            "hub_tls_port": NODE["hub_tls_port"] if NODE.get("surface_dir") else None,
            "screen": screen.size if screen else None, "screen_error": screen.error if screen else None,
            "ui_version": int((STATIC / "index.html").stat().st_mtime), "release":release_identity(), "capabilities":{"agent_history":1,"human_screen_control":bool(screen),"lodge_codex":os.environ.get("LODGE_MODE")=="1"}}


@app.get("/api/projects")
async def list_projects():
    return {"items": [{**s.project, **{k: v for k, v in s.status_payload().items() if k != "type"}} for s in sessions.values()]}


@app.post('/api/control/presenter/access')
async def presenter_access(req: Request):
    body=await json_object(req);display=body.get('display')
    if not isinstance(display,str) or display not in presenter_displays.records:raise HTTPException(422,'Select a registered presentation display')
    value=browser_auth.capability(NODE['token'],'conductor','presenter',display=display)
    return {'access':value,'display':display}


@app.post('/api/control/presenter/surface-access')
async def presenter_surface_access(req: Request):
    grant=getattr(req.state,'presenter',None) if req else None
    if not grant:raise HTTPException(403,'Presenter link required')
    body=await json_object(req);node,ws=body.get('node'),body.get('ws')
    n={'token':NODE['token']} if node==NODE['name'] else _node_entry(node)
    if not n:raise HTTPException(404,'Unknown machine')
    allowed=False
    for tile in presenter_tiles(grant):
        if tile['kind']!='surface' or tile['node']!=node:continue
        assigned=tile.get('workspace')
        if not assigned and tile.get('project'):
            if node==NODE['name']:
                s=get_session(tile['project']);assigned=s.surface_ws if s else None
            else:
                info=await asyncio.to_thread(_remote,n,'GET','/api/projects/'+quote(tile['project'],safe=''))
                assigned=info.get('surface_ws')
        if assigned and assigned==ws:allowed=True;break
    if not allowed:raise HTTPException(403,'Workspace is not in this presentation')
    return {'access':browser_auth.capability(n['token'],'surface','presenter',ws=[ws])}


@app.post('/api/surface/access')
async def surface_access(req: Request):
    body=await json_object(req)
    ws=body.get('ws')
    if not isinstance(ws,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',ws):raise HTTPException(422,'Workspace required')
    role='presenter' if body.get('readonly') else 'operator'
    return {'access':browser_auth.capability(NODE['token'],'surface',role,ws=[ws])}


@app.get('/api/models')
async def list_models():
    from .model_catalog import catalog
    result = catalog(NODE, config.projects(), [a for s in sessions.values() for a in s.agents])
    if os.environ.get('LODGE_MODE') == '1': result['backends'] = {'codex': result['backends']['codex']}
    return result


@app.post("/api/projects")
async def create_project(req: Request):
    body = await json_object(req)
    project_fields(body)
    d = str(body.get("dir", "")).strip()
    if not d:
        return JSONResponse({"error": "dir required"}, status_code=400)
    d = str(Path(d).expanduser().resolve())
    name = (body.get("name") or Path(d).name).strip()
    try:
        pid = config.validate_id(body.get("id") or config.slug(name))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    if pid in sessions:
        return JSONResponse({"error": f"project '{pid}' exists"}, status_code=409)
    from .backends import model_for
    backend = body.get('backend') or NODE.get('backend', 'claude')
    try:
        model = model_for(backend, NODE, {'backend': backend, 'model': body.get('model')})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    Path(d).mkdir(parents=True, exist_ok=True)
    p = {"id": pid, "name": name, "dir": d, "backend": backend, "model": model,
         "host_tools": body.get("host_tools", os.environ.get("LODGE_MODE") != "1"), "conductor": bool(body.get("conductor")), "approval":"pending", "createdAt": time.time()}
    for key in ('instructions', 'mcps', 'codex_yolo', 'opencode_yolo'):
        if key in body:
            p[key] = body[key]
    items = config.projects()
    items.append(p)
    config.save_projects(items)
    s = ProjectSession(LOOP, p, NODE, HUB_URL)
    if os.environ.get('LODGE_MODE') == '1':
        s.rename_agent(s.active_id, name)
    sessions[pid] = s
    if HUB_URL and body.get("workspace", True):
        try:
            ws = await asyncio.get_event_loop().run_in_executor(None, lambda: http_json("POST", HUB_URL + "/api/workspaces", {"name": name}))
            if ws.get("id"):
                await s.select_workspace(ws["id"])
        except Exception as e:
            log("workspace create failed:", e)
    await notify_control({"type": "projects_changed", "node": NODE["name"]})
    return {"ok": True, "project": p, "agent": next(a for a in s.list_agents() if a['id'] == s.active_id),
            "surface_ws": s.surface_ws, "approval":"pending", "started":False}


@app.get("/api/projects/{pid}")
async def project_info(pid: str):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    return {**s.project, **s.status_payload(), "last_result": s.last_result}


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    s = sessions.pop(pid, None)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    await s.stop()
    config.save_projects([p for p in config.projects() if p["id"] != pid])
    await notify_control({"type": "projects_changed", "node": NODE["name"]})
    return {"ok": True}


@app.patch("/api/projects/{pid}")
async def update_project(pid: str, req: Request):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    body = await json_object(req)
    project_fields(body)
    if 'backend' in body and body['backend'] != s.backend:
        raise HTTPException(422, 'Create a new named agent to change backend; saved sessions belong to their original CLI')
    if body.get('model'):
        from .backends import model_for
        try:
            model_for(s.backend, NODE, agent={'backend': s.backend, 'model': body['model']})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        s.agent['model'] = body['model']
        s._save_state()
    for k in ("name", "model", "host_tools", "conductor", "instructions", "mcps", "codex_yolo", "opencode_yolo"):
        if k in body:
            s.project[k] = body[k]
    config.save_projects([s.project if p["id"] == pid else p for p in config.projects()])
    return {"ok": True, "project": s.project}


@app.post("/api/projects/{pid}/{action}")
async def project_action(pid: str, action: str, req: Request):
    if action == "agents":
        return await project_new_agent(pid, req)
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    body = await json_object(req,allow_empty=True)
    if body.get('expected_agent') is not None and body['expected_agent'] != s.active_id:
        raise HTTPException(409,'Active agent changed; review the target')
    if action == "attachments":
        from .attachments import save_attachment
        return await asyncio.to_thread(save_attachment,s.data,body)
    if action == "start":
        await s.start(expected_agent=body.get("expected_agent"))
    elif action == "stop":
        await s.stop(expected_agent=body.get("expected_agent"))
    elif action == "restart":
        await s.restart(fresh=False,expected_agent=body.get("expected_agent"))
    elif action == "new-session":
        await s.restart(fresh=True,expected_agent=body.get("expected_agent"))
    elif action == "interrupt":
        await s.interrupt(expected_agent=body.get("expected_agent"))
    elif action == "workspace":
        await s.select_workspace(body.get("id"),expected_agent=body.get("expected_agent"))
    elif action == "send":
        return await s.send_user(body.get("text", ""), files=body.get("files"), annotations=body.get("annotations"),
                          voice=body.get("voice"), origin=body.get("origin"), request_id=body.get('request_id'), expected_agent=body.get('expected_agent'),request_source=body.get('request_source'),presentation_display=body.get('presentation_display'),require_running=body.get('require_running',False))
    else:
        return JSONResponse({"error": "unknown action"}, status_code=404)
    await notify_control({"type": "projects_changed", "node": NODE["name"]})
    return {"ok": True, "status": s.status_payload()}


@app.get("/api/projects/{pid}/agents")
async def project_agents(pid: str):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    return {"active": s.active_id, "items": s.list_agents()}


@app.get("/api/projects/{pid}/sessions")
async def project_sessions(pid: str):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    return {"items": await asyncio.get_event_loop().run_in_executor(None, s.claude_sessions)}


@app.post("/api/projects/{pid}/agents")
async def project_new_agent(pid: str, req: Request):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    body = await json_object(req)
    project_fields(body)
    try:
        a = await s.new_agent(body.get("name") or None, body.get("session_id") or None, body.get('backend'), body.get('model'))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await notify_control({"type": "projects_changed", "node": NODE["name"]})
    return {"ok": True, "agent": a, "approval":"pending", "started":False}


@app.post("/api/projects/{pid}/agents/{aid}/switch")
async def project_switch_agent(pid: str, aid: str, req: Request):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    body=await json_object(req,allow_empty=True)
    if body.get('expected_agent') is not None and body['expected_agent']!=s.active_id:raise HTTPException(409,'Active agent changed; review before activating')
    try:
        await s.switch_agent(aid,expected_agent=body.get('expected_agent'))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    await notify_control({"type": "projects_changed", "node": NODE["name"]})
    return {"ok": True, "agent": s.agent}


@app.patch("/api/projects/{pid}/agents/{aid}")
async def project_rename_agent(pid: str, aid: str, req: Request):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    body = await json_object(req)
    s.rename_agent(aid, str(body.get("name", "")).strip() or aid)
    s.broadcast_status()
    return {"ok": True}


@app.delete("/api/projects/{pid}/agents/{aid}")
async def project_delete_agent(pid: str, aid: str):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    try:
        await s.delete_agent(aid)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.get("/api/projects/{pid}/result")
async def project_result(pid: str, wait: int = 0, turn_id: str | None = None):
    s = get_session(pid)
    if not s:
        return JSONResponse({"error": "unknown project"}, status_code=404)
    if not turn_id:
        recent = [t for t in s.ledger.items.values() if t['agent'] == s.active_id]
        turn_id = recent[-1]['id'] if recent else None
    if not turn_id:
        return {'pending':False, 'status':'no_turn', 'result':''}
    deadline = asyncio.get_running_loop().time() + max(0, min(wait, 600))
    while True:
        result = s.ledger.result(turn_id)
        if result is None:
            return JSONResponse({'error':'unknown turn'}, status_code=404)
        if not result['pending'] or asyncio.get_running_loop().time() >= deadline:
            return result
        await asyncio.sleep(.05)


@app.get("/api/projects/{pid}/history")
async def project_history(pid: str):
    s = get_session(pid)
    return JSONResponse(s.history if s else [])


def agent_session(pid, aid):
    s = get_session(pid)
    if not s or not any(a['id']==aid for a in s.agents): raise HTTPException(404,'Unknown agent')
    return s


@app.get('/api/projects/{pid}/agents/{aid}/history')
async def agent_history(pid: str, aid: str, before: str | None = None, after: str | None = None, limit: int = 40, event: str | None = None):
    s = agent_session(pid,aid)
    try:
        path = s.history_path(aid)
        data = await asyncio.to_thread(transcripts.detail,path,event) if event else await asyncio.to_thread(transcripts.page,path,before,after,limit)
        return JSONResponse(data,headers={'Cache-Control':'no-store'})
    except ValueError as exc: raise HTTPException(409,str(exc)) from exc


@app.get('/api/projects/{pid}/agents/{aid}/status')
async def agent_status(pid: str, aid: str):
    return agent_session(pid,aid).viewed_status(aid)


@app.post('/api/projects/{pid}/agents/{aid}/approval')
async def agent_approval(pid: str, aid: str, req: Request):
    policy.check_mutation()
    body=await json_object(req)
    state=body.get('approval')
    if state not in ('approved','pending','revoked'):raise HTTPException(422,'Invalid approval')
    s=agent_session(pid,aid)
    async with s.restart_lock:
        policy.check_mutation()
        a=next(a for a in s.agents if a['id']==aid)
        if state!='approved' and aid==s.active_id:await s._stop()
        a['approval']=state
        s._save_state()
    s.broadcast_status()
    return {'ok':True,'approval':state}


@app.websocket('/ws/projects/{pid}/agents/{aid}/chat')
async def ws_agent_view(ws: WebSocket, pid: str, aid: str):
    await ws.accept()
    if not await ws_authorized(ws):return
    s=get_session(pid)
    if not s or not any(a['id']==aid for a in s.agents):
        await ws.close(code=4404);return
    viewer={'agent':aid,'clients':{ws},'initializing':True,'pending':[],'lock':asyncio.Lock()}
    s.viewers[ws]=viewer
    try:
        path=s.history_path(aid)
        after=ws.query_params.get('after');resumed=bool(after)
        try: snapshot=await asyncio.to_thread(transcripts.page,path,None,after)
        except ValueError: resumed=False;snapshot=await asyncio.to_thread(transcripts.page,path)
        await ws.send_json(s.viewed_status(aid))
        await ws.send_json({'type':'history_page','append':resumed,**snapshot})
        # A socket buffers live events until its snapshot has been delivered.
        through=snapshot.get('through')
        while snapshot.get('more'):
            snapshot=await asyncio.to_thread(transcripts.page,path,None,snapshot['cursor'],through=through)
            await ws.send_json({'type':'history_page','append':True,**snapshot})
        async with viewer['lock']:
            for ev in viewer['pending']:
                cur=ev.get('cursor')
                if cur and cur.split(':')[0]==snapshot['revision'] and int(cur.split(':')[1])<=int(snapshot['cursor'].split(':')[1]):continue
                await ws.send_json(transcripts.preview(ev,ev.get('event_id','')))
            viewer['pending'].clear();viewer['initializing']=False
        while True:
            message=await ws.receive_json()
            if message.get('type')=='ping':await ws.send_json({'type':'pong'})
            else:await ws.send_json({'type':'delivery_error','error':'This is an observation socket; use an explicit targeted command'})
    except (WebSocketDisconnect, RuntimeError):pass
    finally:s.viewers.pop(ws,None)


@app.websocket("/ws/projects/{pid}/chat")
async def ws_chat(ws: WebSocket, pid: str):
    await ws.accept()
    if not await ws_authorized(ws):
        return
    s = get_session(pid)
    if not s:
        await ws.close(code=4404)
        return
    s.clients.add(ws)
    try:
        await ws.send_text(json.dumps(s.history_payload()))
        await ws.send_text(json.dumps(s.status_payload()))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            k = msg.get("type")
            if k != 'ping':
                try:
                    policy.check_mutation()
                    if msg.get('expected_agent') is not None and msg['expected_agent'] != s.active_id:
                        raise policy.PolicyError('Active agent changed; review the target')
                except policy.PolicyError as exc:
                    await ws.send_json({'type':'delivery_error','error':str(exc),'status':exc.status})
                    continue
            try:
                if k == "send":
                    try:
                        receipt = await s.send_user(msg.get("text", ""), annotations=msg.get("annotations") or None,
                                                    voice=msg.get("voice") or None, request_id=msg.get('request_id'),expected_agent=msg.get('expected_agent'),request_source=msg.get('request_source'),presentation_display=msg.get('presentation_display'))
                        await ws.send_json({'type':'receipt', **receipt})
                    except DeliveryError as exc:
                        await ws.send_json({'type':'delivery_error','request_id':msg.get('request_id'),'error':str(exc)})
                elif k == "interrupt":
                    await s.interrupt(expected_agent=msg.get("expected_agent"))
                elif k == "restart":
                    await s.restart(fresh=False,expected_agent=msg.get("expected_agent"))
                elif k == "new_session":
                    await s.restart(fresh=True,expected_agent=msg.get("expected_agent"))
                elif k == "select_workspace":
                    await s.select_workspace(msg.get("id"),expected_agent=msg.get("expected_agent"))
                elif k == "switch_agent":
                    try:
                        await s.switch_agent(msg.get("id"),expected_agent=msg.get("expected_agent"))
                    except ValueError:
                        pass
                elif k == "new_agent":
                    project_fields(msg)
                    await s.new_agent(msg.get("name") or None, msg.get("session_id") or None, msg.get('backend'), msg.get('model'))
                elif k == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except DeliveryError as exc:
                await ws.send_json({'type':'delivery_error','error':str(exc),'status':getattr(exc,'status',422)})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log("ws_chat error", e)
    finally:
        s.clients.discard(ws)


# --------------------------------------------------------------------------
# host: screen + computer
# --------------------------------------------------------------------------
@app.websocket("/ws/screen")
async def ws_screen(ws: WebSocket):
    await ws.accept()
    if not await ws_authorized(ws):
        return
    if screen is None:
        await ws.close(code=4403)
        return
    screen.clients.add(ws)
    try:
        if screen.latest:
            frame,metadata=getattr(screen,"latest_packet",(screen.latest,None))
            if metadata and ws.query_params.get("metadata")=="1":await ws.send_json(metadata)
            await ws.send_bytes(frame)
        while True:
            await asyncio.wait_for(ws.receive_text(),30)
            if not await ws_authorized(ws):return
    except Exception:
        pass
    finally:
        screen.clients.discard(ws)


@app.get("/screenshot.jpg")
async def screenshot():
    if screen is None:
        return JSONResponse({"error": "host tools are disabled on this node (it is the control machine)"}, status_code=403)
    data = await asyncio.get_event_loop().run_in_executor(None, screen.screenshot_jpeg)
    return Response(content=data, media_type="image/jpeg")


async def perform_computer(action,coordinate=None,text=None,amount=None):
    if not NODE.get('host_tools',True) or screen is None:raise policy.PolicyError('This machine does not expose screen control',403)
    if screen.backend=='wayland':
        return await asyncio.to_thread(wayland_perform,action,coordinate,text,amount)
    import importlib.util
    spec=importlib.util.spec_from_file_location('agentnode_computer',config.REPO/'mcp/computer_mcp.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return await asyncio.to_thread(mod.perform,action,coordinate,text,amount,_via_node=False)


@app.post('/api/host/computer')
async def host_computer(req: Request):
    body=await json_object(req)
    async with human_screen.lock:
        policy.check_mutation()
        if body.get('action') not in ('screenshot','cursor_position'):human_screen.check()
        res=await perform_computer(body.get('action',''),body.get('coordinate'),body.get('text'),body.get('amount'))
        img=res.pop('_image',None)
        if img:res['image']=base64.b64encode(img).decode()
        return res


@app.websocket('/ws/host/control')
async def ws_human_control(ws: WebSocket):
    await ws.accept()
    if not await ws_authorized(ws):return
    try:
        async with human_screen.lock:
            policy.check_mutation();g=screen_geometry(screen);human_screen.acquire(ws)
        await ws.send_json({'type':'control_ready','geometry':g,'server_time':time.time()})
        log('human screen control acquired')
        while True:
            body=await asyncio.wait_for(ws.receive_json(),15)
            if not await ws_authorized(ws):return
            if body.get('type')=='ping':
                human_screen.check(ws);await ws.send_json({'type':'geometry','geometry':screen_geometry(screen)});continue
            try:
                async with human_screen.lock:
                    policy.check_mutation();human_screen.check(ws)
                    current=screen_geometry(screen)
                    if type(body.get('at')) not in (int,float) or not -5<time.time()-body['at']<3:raise policy.PolicyError('Input expired; try again',409)
                    if type(body.get('frame_at')) not in (int,float) or not 0<=time.time()-body['frame_at']<5:raise policy.PolicyError('Displayed frame is stale; wait for the live screen',409)
                    args=input_message(body,current)
                    await perform_computer(*args)
                await ws.send_json({'type':'ack'})
            except policy.PolicyError as exc:
                await ws.send_json({'type':'error','error':str(exc),'status':exc.status,'geometry':screen_geometry(screen)})
    except policy.PolicyError as exc:
        await ws.send_json({'type':'error','error':str(exc),'status':exc.status})
    except (WebSocketDisconnect,asyncio.TimeoutError,RuntimeError):pass
    finally:
        human_screen.release(ws)
        try:await ws.close()
        except RuntimeError:pass
        log('human screen control released')


def wayland_perform(action, coordinate=None, text=None, amount=None) -> dict:
    """Computer actions on a Wayland desktop via the portal backend; coordinates arrive in FRAME_W-wide space."""
    import io
    from .wayland import desktop
    d = desktop()
    img = d.image()
    if img is None:
        return {"status": "error", "message": "no frame from the desktop yet"}
    from .screenshots import dimensions, encode
    fw, fh = dimensions(*img.size)
    k, ky = img.width / fw, img.height / fh
    if action == "screenshot":
        shot = encode(img)
        return {"status": "success", "action": "screenshot", "width": shot['width'], "height": shot['height'], "_image": shot['jpeg']}
    if coordinate and action in ("mouse_move", "left_click", "right_click", "middle_click", "double_click", "scroll"):
        d.move(coordinate[0] * k, coordinate[1] * ky)
        time.sleep(0.05)
    if action == "mouse_move":
        pass
    elif action == "left_click":
        d.click("left")
    elif action == "right_click":
        d.click("right")
    elif action == "middle_click":
        d.click("middle")
    elif action == "double_click":
        d.click("left", 2)
    elif action == "type":
        d.type_text(text or "")
    elif action == "key":
        d.key(text or "")
    elif action == "scroll":
        d.scroll(int(amount or 0))
    elif action == "cursor_position":
        return {"status": "success", "message": "cursor position is not reported on Wayland; take a screenshot (the cursor is drawn in it)"}
    else:
        return {"status": "error", "message": f"unknown action: {action}"}
    time.sleep(0.15)
    return {"status": "success", "action": action}


# --------------------------------------------------------------------------
# Surface hub integration (external-agent mode)
# --------------------------------------------------------------------------
def _session_for_ws(ws_id: str | None) -> ProjectSession | None:
    for s in sessions.values():
        if ws_id and s.surface_ws == ws_id:
            return s
    return None


@app.get("/api/surface/workspaces")
async def surface_workspaces():
    if not HUB_URL:
        return {"ok": False, "items": [], "error": "no Surface hub on this node"}
    try:
        r = await asyncio.get_event_loop().run_in_executor(None, lambda: http_json("GET", HUB_URL + "/api/workspaces"))
        return {"ok": True, "items": r.get("items", []), "hub_port": NODE["hub_port"], "hub_tls_port": NODE["hub_tls_port"]}
    except Exception as e:
        return {"ok": False, "items": [], "error": str(e)}


@app.post("/api/surface/workspaces")
async def surface_create_workspace(req: Request):
    body = await json_object(req)
    r = await asyncio.get_event_loop().run_in_executor(None, lambda: http_json("POST", HUB_URL + "/api/workspaces", body))
    return r


@app.post("/api/surface/event")
async def surface_event(req: Request):
    body = await json_object(req)
    s = _session_for_ws(body.get("ws"))
    if s:
        await s.send_user(str(body.get("text", "")), files=body.get("files") or [], kind="surface_event")
    return {"ok": bool(s)}


@app.post("/api/surface/context")
async def surface_context(req: Request):
    body = await json_object(req)
    s = _session_for_ws(body.get("ws"))
    if s:
        s.push_context(str(body.get("note", "")), body.get("key") or None)
        s.broadcast_status()
    return {"ok": bool(s)}


@app.post("/api/surface/interrupt")
async def surface_interrupt(req: Request):
    body = await json_object(req)
    s = _session_for_ws(body.get("ws"))
    if s:
        await s.interrupt()
    return {"ok": bool(s)}


@app.post("/api/surface/reset")
async def surface_reset(req: Request):
    return {"ok": True}


# --------------------------------------------------------------------------
# voice
# --------------------------------------------------------------------------
@app.get("/api/voice/config")
async def voice_config():
    sec = config.secrets_()
    return {"tts_server": bool(sec.get("ELEVENLABS_API_KEY")), "stt_server": bool(sec.get("OPENAI_API_KEY")),
            "wake_local": voicemod.wake_available(), "wake_word": NODE.get("wake_word"),
            "tls_port": NODE["tls_port"] if (config.TLS / "cert.pem").exists() else None}


_vlog: list = []


@app.post("/api/voice/log")
async def api_voice_log(req: Request):
    """Browser-side voice pipeline debug lines (ring buffer, readable at GET /api/voice/log)."""
    body = await json_object(req)
    for line in (body.get("lines") or [])[:50]:
        _vlog.append({"t": time.time(), "ua": (req.headers.get("user-agent") or "")[:40], "line": str(line)[:300]})
    del _vlog[:-500]
    return {"ok": True}


@app.get("/api/voice/log")
async def api_voice_log_get(n: int = 80):
    return {"items": _vlog[-n:]}


@app.post("/api/say")
async def api_say(req: Request):
    body = await json_object(req)
    s = get_session(body.get('project') or '')
    if not s or s.response_channel()!='voice':
        return {'ok':True,'spoken':False,'response_channel':'chat',
                'message':'This request requires a text reply. Respond or ask your question in chat; do not call voice tools.'}
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    looks_like_question = text.rstrip().endswith("?") or bool(re.match(
        r"^(want|shall|should|do you|would you|could you|can you|which|what|where|when|who|how|is it|are you|did you|any preference)\b",
        text, re.I))
    ev = {"type": "say", "text": text[:400], "expect_reply": bool(body.get("expect_reply")) or looks_like_question,
          "project": body.get("project") or "", "node": NODE["name"], "ts": time.time(),
          'response_channel':'voice','turn_id':s.current_turn,'voice_target':s.voice_target()}
    if s:
        s._record(ev)
        await s.broadcast(ev)
    if voicestream:
        voicestream.on_say(body.get("project") or "", ev["expect_reply"], ev["voice_target"])
    await notify_control(ev)
    return {"ok": True,'spoken':True,'response_channel':'voice'}


DEFAULT_VOICE = "onwK4e9ZLuTAKqWW03F9"   # ElevenLabs "Daniel": steady British broadcaster


@app.get("/api/tts/voices")
async def api_tts_voices():
    key = config.secrets_().get("ELEVENLABS_API_KEY")
    if not key:
        return {"items": [], "default": DEFAULT_VOICE}
    try:
        items = await asyncio.get_event_loop().run_in_executor(None, voicemod.tts_voices, key)
        return {"items": items, "default": config.secrets_().get("ELEVENLABS_VOICE") or DEFAULT_VOICE}
    except Exception as e:
        return {"items": [], "default": DEFAULT_VOICE, "error": str(e)[:200]}


@app.post("/api/tts")
async def api_tts(req: Request):
    sec = config.secrets_()
    key = sec.get("ELEVENLABS_API_KEY")
    if not key:
        return JSONResponse({"error": "no ELEVENLABS_API_KEY configured"}, status_code=400)
    body = await json_object(req)
    text = str(body.get("text", ""))[:900].strip()
    voice_id = body.get("voice") or sec.get("ELEVENLABS_VOICE") or DEFAULT_VOICE
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, voicemod.tts_elevenlabs, key, voice_id, text)
        return Response(content=data, media_type="audio/mpeg")
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)


@app.post("/api/stt")
async def api_stt(req: Request):
    sec = config.secrets_()
    key = sec.get("OPENAI_API_KEY")
    if not key:
        return JSONResponse({"error": "no OPENAI_API_KEY configured"}, status_code=400)
    audio = await body_bytes(req,8*1024*1024)
    ctype = req.headers.get("content-type", "audio/webm")
    lang = req.query_params.get("lang") or None
    prompt = req.query_params.get("prompt") or f"Hey {NODE.get('wake_word', 'conductor').capitalize()}"
    model = sec.get("STT_MODEL") or "gpt-4o-transcribe"
    try:
        text = await asyncio.get_event_loop().run_in_executor(None, voicemod.stt_openai, key, model, audio, ctype, lang, prompt)
        return {"text": text}
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)


@app.post("/api/wake")
async def api_wake(req: Request):
    audio = await body_bytes(req,8*1024*1024)
    lang = req.query_params.get("lang") or None
    pats = voicemod.wake_patterns(NODE.get("wake_word", "conductor"))
    try:
        return await asyncio.get_event_loop().run_in_executor(None, voicemod.wake_screen, audio, lang, pats)
    except Exception as e:
        return JSONResponse({"available": False, "text": "", "wake": False, "error": str(e)[:200]}, status_code=500)


# --------------------------------------------------------------------------
# control node: fleet directory, discovery, focus channel, proxy
# --------------------------------------------------------------------------
_roster_cache = {"text": "", "ts": 0.0}


async def fleet_roster() -> str:
    """One-line-per-machine roster, prepended to the conductor's messages so it never works from a stale picture."""
    if time.time() - _roster_cache["ts"] < 20 and _roster_cache["text"]:
        return _roster_cache["text"]
    try:
        tree = await control_tree()
        lines = []
        for n in tree["nodes"]:
            projs = ", ".join(f"{p['id']}{'' if p.get('alive') else ' (stopped)'}" for p in n.get("projects", [])) or "no projects"
            caps = "screen+input" if n.get("info", {}).get("host_tools") else "no screen"
            lines.append(f"- {n['name']}{' (control, this machine)' if n.get('local') else ''}: {projs} [{caps}{'' if n.get('reachable') else ', UNREACHABLE'}]")
        _roster_cache.update(text="[fleet] machines and projects right now (use these ids with the conductor tools; do NOT search filesystems for agents):\n" + "\n".join(lines), ts=time.time())
    except Exception as e:
        log("roster failed", e)
    return _roster_cache["text"]


def _presentation_destination(display=None):
    try:return presenter_displays.resolve(display)
    except DisplayChoice:raise
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


async def _bind_stage_tiles(tiles):
    try:tiles = clean_tiles(tiles)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    for tile in tiles:
        if tile['kind'] != 'surface' or tile.get('workspace'):continue
        node = _node_entry(tile['node'])
        if not node:continue  # Unknown/offline references remain visible with their normal error UI.
        if node.get('local'):
            session = get_session(tile['project'])
            workspace = session.surface_ws if session else None
        else:
            try:
                status = await asyncio.to_thread(_remote,node,'GET','/api/projects/'+quote(tile['project'],safe=''))
                workspace = status.get('surface_ws')
            except Exception:
                raise HTTPException(422, 'Cannot resolve the Surface workspace. Reconnect the worker before presenting it.')
        if workspace:tile['workspace'] = workspace
    return tiles


async def notify_control(ev: dict):
    if ev.get('type') in ('focus', 'stage', 'pin'):
        conductor = _conductor_session()
        if ev.get('by') and type(ev.get('base_revision')) is not int:
            raise HTTPException(422, 'Read list_displays and supply base_revision before changing Stage.')
        caller = sessions.get(ev.get('by')) if isinstance(ev.get('by'),str) else None
        voice_target = (caller.voice_target() if caller and getattr(caller,'current_turn',None)
                        and caller.response_channel() == 'voice' else None)
        started = getattr(caller,'current_stage_reset_revision',None)
        if caller and caller.current_turn and started is not None and started < presentation_stage.last_reset_revision:
            raise StageConflict('The human reset Stage during this task. Leave it cleared and ask for a new request before presenting again.')
        display = _presentation_destination(ev.get('display'))
        presentation_stage.check(ev.get('base_revision'))
        revision = presentation_stage.revision
        target = StageState(None, data={'tiles':presentation_stage.snapshot(display)['tiles']})
        try:target._apply(ev, (NODE['name'], conductor.project['id'] if conductor else None))
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
        tiles = await _bind_stage_tiles(target.tiles)
        # Workspace discovery awaits I/O; a newer human edit/reset always wins.
        if display is not None:_presentation_destination(display)
        presentation_stage.replace(tiles,revision,display=display)
        ev = {**ev, 'type':'presentation_stage', 'presentation':presentation_stage.snapshot()}
        if voice_target:
            scoped = presentation_stage.snapshot(display)
            voice_stages.pop(voice_target, None)
            voice_stages[voice_target] = (time.monotonic(), scoped)
            while len(voice_stages) > 128:voice_stages.pop(next(iter(voice_stages)))
            ev.update(voice_target=voice_target, voice_presentation=scoped)
    if ev.get("type") in ("projects_changed", "nodes_changed"):
        _roster_cache["ts"] = 0.0
    await send_all(control_clients, ev)


def voice_stage_replay(client):
    saved = voice_stages.get(client)
    if not saved:return {}
    created, snapshot = saved
    if time.monotonic()-created > 3600 or snapshot['revision'] < presentation_stage.last_reset_revision:
        voice_stages.pop(client, None)
        return {}
    return {'voice_target':client, 'voice_presentation':snapshot}


@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    """Streaming voice: binary PCM16/16k in, JSON state out. Runs the whole pipeline on the control node."""
    await ws.accept()
    if not await ws_authorized(ws):
        return
    if voicestream is None:
        await ws.send_json({"type": "error", "text": "voice streaming is not available on this node"})
        await ws.close()
        return
    vs = voicestream.VoiceSession(ws, NODE, get_session, lang=ws.query_params.get("lang") or "en-US",
                                  project=ws.query_params.get("project") or "conductor")
    try:
        await vs.set_state("wake")
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            if m.get("bytes") is not None:
                await vs.feed(m["bytes"])
            elif m.get("text"):
                try:
                    await vs.control(json.loads(m["text"]))
                except Exception as e:
                    log("[voice] bad control msg", e)
    except Exception as e:
        log("[voice] session ended:", str(e)[:120])
    finally:
        vs.close()


@app.websocket("/ws/control")
async def ws_control(ws: WebSocket):
    await ws.accept()
    if not await ws_authorized(ws):
        return
    control_clients.add(ws)
    try:
        await ws.send_text(json.dumps({'type':'presentation_stage','presentation':presentation_stage.snapshot(),
                                      **voice_stage_replay(ws.query_params.get('voice_client'))}))
        while True:
            await asyncio.wait_for(ws.receive_text(),30)
            if not await ws_authorized(ws):return
    except Exception:
        pass
    finally:
        control_clients.discard(ws)


def _node_entry(name: str) -> dict | None:
    if name in ("local", NODE["name"]):
        return {"name": NODE["name"], "url": f"http://127.0.0.1:{NODE['port']}", "token": NODE["token"], "local": True}
    return next((n for n in config.nodes() if n["name"] == name), None)


def _remote(n: dict, method: str, path: str, body=None, timeout=8.0, raw=False):
    r = transport.request(n,method,path,body,timeout)
    if raw:
        return r
    r.raise_for_status()
    return r.json()


@app.get("/api/control/tree")
async def control_tree(req: Request = None):
    loop = asyncio.get_event_loop()
    me = {"name": NODE["name"], "url": f"http://127.0.0.1:{NODE['port']}", "local": True, "reachable": True,
          "info": (await node_info()), "projects": (await list_projects())["items"], "hub_port": NODE["hub_port"] if HUB_URL else None,
          "hub_tls_port": NODE["hub_tls_port"] if HUB_URL else None, "host": "127.0.0.1",
          "surface_gateway_port": NODE.get('surface_gateway_port')}
    out = [me]

    async def probe(n):
        try:
            info = await loop.run_in_executor(None, lambda: _remote(n, "GET", "/api/node/info", timeout=6))
            projs = await loop.run_in_executor(None, lambda: _remote(n, "GET", "/api/projects", timeout=6))
            return {**n, "token": None, "reachable": True, "info": info, "projects": projs.get("items", []),
                    "hub_port": info.get("hub_port"), "hub_tls_port": info.get("hub_tls_port"),
                    "host": re.sub(r"^https?://", "", n["url"]).split(":")[0].split("/")[0]}
        except Exception as e:
            return {**n, "token": None, "reachable": False, "error": str(e)[:120], "projects": [],
                    "host": re.sub(r"^https?://", "", n["url"]).split(":")[0].split("/")[0]}
    out += await asyncio.gather(*(probe(n) for n in config.nodes()))
    grant=getattr(req.state,'presenter',None) if req else None
    if grant:
        tiles=presenter_tiles(grant);names={t['node'] for t in tiles}
        fleet=any(t['kind']=='fleet' for t in tiles)
        out=[n for n in out if fleet or n['name'] in names]
        for n in out:
            projects={t.get('project') for t in tiles if t['node']==n['name']}
            n['projects']=[{k:v for k,v in p.items() if k in ('id','name','surface_ws','status','alive','conductor')} for p in n['projects'] if fleet or p['id'] in projects]
            for key in ('token','ssh','ca','ca_file'):n.pop(key,None)
    return {"nodes": out}


# ---- relays: the control node listens to every other machine's control channel and forwards
#      status / say / change events to its own UI clients, so the sidebar and voice stay live fleet-wide
_relays: dict[str, asyncio.Task] = {}


async def _relay_node(n: dict):
    while True:
        try:
            async with transport.websocket(n,'/ws/control',ping_interval=20,max_size=4 * 1024 * 1024) as ws:
                async for raw in ws:
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        continue
                    if ev.get("type") in ("project_status", "say", "projects_changed", "nodes_changed"):
                        ev.setdefault("node", n["name"])
                        if ev.get("type") == "projects_changed":
                            _roster_cache["ts"] = 0.0
                        await notify_control(ev)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(5)


def _start_relays():
    for key in list(_relays):
        if not key.startswith('__'):
            _relays.pop(key).cancel()
    for n in config.nodes():
        _relays[n["name"]] = asyncio.ensure_future(_relay_node(n))


_watches: dict[str, asyncio.Task] = {}


def _conductor_session() -> ProjectSession | None:
    return next((s for s in sessions.values() if s.project.get("conductor")), None)


def _watch_records():
    return read_json(config.HOME / 'watches.json', {})


def _save_watch(key, record):
    records = _watch_records()
    records[key] = record
    write_json(config.HOME / 'watches.json', records)


WATCH_CHECKIN_SECONDS = 240


async def _watch_checkin(key, record, node_entry):
    """Persist a bounded progress message before delivery so retries keep one receipt."""
    latest = _watch_records().get(key, {})
    record['next_checkin'] = latest.get('next_checkin', record.get('next_checkin', time.time()+WATCH_CHECKIN_SECONDS))
    if not record.get('progress_delivery') and time.time() < record['next_checkin']:
        return
    c = get_session(record['recipient_project'])
    if not c or c.active_id != record['recipient_agent']:
        return
    if not record.get('progress_delivery'):
        try:
            st = await asyncio.to_thread(_remote, node_entry, 'GET',
                f"/api/projects/{quote(record['project'], safe='')}", timeout=10)
            if st.get('turn_id') == record['turn_id']:
                detail = f"Status: {st.get('status')}; current tool: {str(st.get('tool') or 'none reported')[:200]}."
            else:
                detail = 'The exact turn is still pending; the current agent status belongs to another turn or cannot be correlated.'
        except Exception:
            detail = 'The worker status is unreachable; completion is unknown.'
        seq = record.get('progress_seq', 0)+1
        record['progress_delivery'] = dict(seq=seq, text=(
            f"[delegate-progress] {record['node']}/{record['project']}; turn {record['turn_id']}\n"
            f"Four minutes without a reported update. Requested: {record['brief']}\n{detail}\n"
            "Give the human a concise factual check-in in the original response channel. Do not resend the task or claim completion. The watcher will deliver its outcome."))
        _save_watch(key, record)
    pending = record['progress_delivery']
    await c.send_user(pending['text'], kind='delegate_progress', origin=f"{record['node']}/{record['project']}",
        request_id=f"watch-progress-{key}-{pending['seq']}", expected_agent=record['recipient_agent'],
        request_source='automation', response_channel=record.get('response_channel','chat'), voice=record.get('voice'),presentation_display=record.get('presentation_display'),presentation_reset_revision=record.get('presentation_reset_revision',0))
    record.update(progress_seq=pending['seq'], next_checkin=time.time()+WATCH_CHECKIN_SECONDS)
    record.pop('progress_delivery', None)
    _save_watch(key, record)


async def _watch_delegate(key, record):
    """Poll an immutable turn; durable outcomes survive controller restarts."""
    node, project, turn = record['node'], record['project'], record['turn_id']
    n = _node_entry(node)
    record.setdefault('next_checkin', time.time()+WATCH_CHECKIN_SECONDS)
    try:
        r = record.get('result')
        while r is None and time.time() < record['deadline']:
            try:
                candidate = await asyncio.to_thread(_remote, n, 'GET',
                    f"/api/projects/{quote(project, safe='')}/result?wait=20&turn_id={quote(turn, safe='')}", timeout=25)
                if candidate.get('turn_id') != turn:
                    raise ValueError('delegate did not return the requested turn')
                if not candidate.get('pending'):
                    r = candidate
                    break
            except Exception as exc:
                record['last_error'] = str(exc)[:240]
                await asyncio.sleep(2)
            try:
                await _watch_checkin(key, record, n)
            except Exception as exc:
                record['last_error'] = str(exc)[:240]
                _save_watch(key, record)
        if r is None:
            r = {'turn_id':turn, 'is_error':True, 'subtype':'timeout',
                 'result':'Delegate monitoring timed out; completion is unknown. Query this turn before retrying the command.'}
        record.update(result=r, status='awaiting_delivery')
        _save_watch(key, record)
        failed = bool(r.get('is_error'))
        await notify_control({'type':'delegation','phase':'failed' if failed else 'done', 'node':node,
            'project':project, 'turn_id':turn, 'is_error':failed, 'summary':r.get('result','')[:160], 'ts':time.time()})
        c = get_session(record['recipient_project'])
        if not c or c.active_id != record['recipient_agent']:
            record['last_error'] = 'The receiving conductor agent is no longer active; result retained for recovery.'
            _save_watch(key, record)
            return
        text = r.get('result') or '(no text)'
        receipt = await c.send_user(
            f"[delegate-result] {node}/{project} {'failed or needs inspection' if failed else 'completed'}; turn {turn}\n"
            f"Requested: {record['brief']}\n\n{text[:80000]}\n\nFull result: /n/{node}/api/projects/{project}/result?turn_id={quote(turn, safe='')}",
            kind='delegate_result', origin=f'{node}/{project}', request_id='watch-'+key,
            expected_agent=record['recipient_agent'],request_source='automation',response_channel=record.get('response_channel','chat'),voice=record.get('voice'),presentation_display=record.get('presentation_display'),presentation_reset_revision=record.get('presentation_reset_revision',0))
        record.update(status='relayed', receipt=receipt)
        _save_watch(key, record)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        record['last_error'] = str(exc)[:240]
        _save_watch(key, record)
    finally:
        if record.get('status') == 'awaiting_delivery':
            record['attempts'] = record.get('attempts',0)+1
            record['retry_at'] = time.time()+min(60,2**min(record['attempts'],6))
            _save_watch(key,record)
        if _watches.get(key) is asyncio.current_task():
            _watches.pop(key, None)


async def _watch_supervisor():
    while True:
        await asyncio.sleep(2)
        if not (config.HOME / 'deployment-maintenance.json').exists(): _restore_watches()


def _restore_watches():
    for key, record in _watch_records().items():
        if record['status'] in ('watching','awaiting_delivery') and key not in _watches and record.get('retry_at',0)<=time.time():
            _watches[key] = asyncio.create_task(_watch_delegate(key, record))


@app.post('/api/control/watch')
async def control_watch(req: Request):
    body = await json_object(req)
    node, project, turn = (body.get(k) for k in ('node','project','turn_id'))
    if not all(isinstance(v,str) and 0 < len(v) <= 100 for v in (node,project,turn)) or not _node_entry(node):
        return JSONResponse({'error':'node, project and turn_id are required'},status_code=422)
    c = _conductor_session()
    if not c: return JSONResponse({'error':'no receiving conductor'},status_code=409)
    key = hashlib.sha256(json.dumps([node,project,turn]).encode()).hexdigest()
    record = _watch_records().get(key)
    if not record:
        record = dict(node=node, project=project, turn_id=turn, brief=str(body.get('brief') or '')[:200],
                      deadline=time.time()+3*3600, status='watching', recipient_project=c.project['id'], recipient_agent=c.active_id,
                      response_channel=c.response_channel(), next_checkin=time.time()+WATCH_CHECKIN_SECONDS,
                      presentation_reset_revision=getattr(c,'current_stage_reset_revision',None) if getattr(c,'current_stage_reset_revision',None) is not None else presentation_stage.last_reset_revision,
                      presentation_display=((c.ledger.get(c.current_turn) or {}).get('input',{}).get('presentation_display') if getattr(c,'current_turn',None) else None),
                      voice={'client_id':c.voice_target()} if c.voice_target() else None)
        _save_watch(key, record)
    if record['status'] != 'relayed' and key not in _watches:
        _watches[key] = asyncio.create_task(_watch_delegate(key, record))
    return {'ok':True,'watching':key,'turn_id':turn,'status':record['status']}


@app.post('/api/control/watch/news')
async def control_watch_news(req: Request):
    body = await json_object(req)
    updated = 0
    for key, record in _watch_records().items():
        if record['status']=='watching' and all(record.get(k)==body.get(k) for k in ('node','project','turn_id')):
            record['next_checkin'] = time.time()+WATCH_CHECKIN_SECONDS
            _save_watch(key, record)
            updated += 1
    return {'ok':True, 'updated':updated}


@app.get('/api/control/watches')
async def control_watches():
    records = _watch_records()
    return {'items':[f"{r['node']}/{r['project']}" for r in records.values() if r['status'] != 'relayed'],
            'records':records}


@app.exception_handler(DisplayChoice)
async def display_choice_error(req, exc):
    return JSONResponse({'error':str(exc),'code':'needs_display_selection','displays':exc.displays,
                         'presentation':presentation_stage.snapshot()},status_code=409)


@app.exception_handler(StageConflict)
async def stage_conflict_error(req, exc):
    display = getattr(req.state,'presentation_display',None)
    return JSONResponse({'error':str(exc),'code':'stage_changed','presentation':presentation_stage.snapshot(display)},status_code=409)


@app.get('/api/control/displays')
async def list_displays(req: Request):
    data=presenter_displays.listing(presentation_stage)
    grant=getattr(req.state,'presenter',None) if req else None
    if grant:
        for key in ('items','known'):data[key]=[x for x in data.get(key,[]) if x['id']==grant['display']]
    return data


@app.put('/api/control/displays/{display}')
async def register_display(display: str, req: Request):
    body = await json_object(req)
    agent = req.headers.get('user-agent', '').lower()
    mobile = body.get('client') != 'desktop' or any(x in agent for x in ('android', 'iphone', 'ipad', 'mobile'))
    before = presenter_displays.describe(display) if display in presenter_displays.records else None
    try:
        item = presenter_displays.register(display,body.get('name'),mobile=mobile,connection=body.get('connection','legacy'),previous_connection=body.get('previous_connection'))
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    if before != item:await notify_control({'type':'displays_changed'})
    return item


@app.delete('/api/control/displays/{display}')
async def leave_display(display: str, connection: str = 'legacy'):
    presenter_displays.leave(display,connection)
    await notify_control({'type':'displays_changed'})
    return {'ok':True}


@app.post('/api/control/displays/{display}/ack')
async def acknowledge_display(display: str, req: Request):
    body = await json_object(req)
    try:presenter_displays.acknowledge(display,body.get('connection','legacy'),body.get('revision'),presentation_stage)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    return {'ok':True}


@app.get('/api/control/presentation')
async def get_presentation(display: str | None = None):
    if display is not None and display not in presenter_displays.records:
        raise HTTPException(404,'Unknown presenter screen.')
    return presentation_stage.snapshot(display)


@app.post('/api/control/presentation')
async def publish_presentation(req: Request):
    body = await json_object(req)
    if body.get('display') is not None and not isinstance(body['display'],str):
        raise HTTPException(422,'Invalid presenter display.')
    req.state.presentation_display = body.get('display')
    if type(body.get('base_revision')) is not int:raise HTTPException(422,'Supply the current stage revision.')
    source = body.get('source')
    if not isinstance(source,str) or not source or len(source)>128:raise HTTPException(422,'Supply a presentation source.')
    if body.get('action') not in (None,'reset'):raise HTTPException(422,'Invalid presentation action.')
    if body.get('action') == 'reset':
        conductor = next((p for p in config.projects() if p.get('conductor')),None)
        presentation_stage.reset((NODE['name'],conductor['id'] if conductor else None),body['base_revision'])
    else:
        display = _presentation_destination(body.get('display'))
        presentation_stage.check(body['base_revision'])
        tiles = await _bind_stage_tiles(body.get('tiles'))
        if display is not None:_presentation_destination(display)
        presentation_stage.replace(tiles,body['base_revision'],display=display)
    snapshot = presentation_stage.snapshot()
    await notify_control({'type':'presentation_stage','presentation':snapshot,'source':source})
    return presentation_stage.snapshot(body.get('display'))


@app.post("/api/control/focus")
async def control_focus(req: Request):
    body = await json_object(req)
    if body.get('display') is not None and not isinstance(body['display'],str):
        raise HTTPException(422,'Invalid presenter display.')
    req.state.presentation_display = body.get('display')
    ev = {"type": "focus", "display": body.get("display"), "node": body.get("node"), "project": body.get("project"), "by": body.get("by"), "soft": bool(body.get("soft")),
          "view": body.get("view"), "mode": body.get("mode") or "replace", "pin": bool(body.get("pin")), "base_revision":body.get("base_revision"), "ts": time.time()}
    await notify_control(ev)
    return {"ok": True, "presentation":presentation_stage.snapshot()}


@app.post("/api/control/pin")
async def control_pin(req: Request):
    body = await json_object(req)
    if body.get('display') is not None and not isinstance(body['display'],str):
        raise HTTPException(422,'Invalid presenter display.')
    req.state.presentation_display = body.get('display')
    await notify_control({"type": "pin", "display": body.get("display"), "node": body.get("node"), "project": body.get("project"), "pinned": body.get("pinned", True), "by": body.get("by"), "base_revision":body.get("base_revision"), "ts": time.time()})
    return {"ok":True,"presentation":presentation_stage.snapshot()}


@app.post("/api/control/stage")
async def control_stage(req: Request):
    body = await json_object(req)
    if body.get('display') is not None and not isinstance(body['display'],str):
        raise HTTPException(422,'Invalid presenter display.')
    req.state.presentation_display = body.get('display')
    tiles = body.get('tiles') if isinstance(body,dict) else None
    if not isinstance(tiles,list) or len(tiles)>6:
        return JSONResponse({'error':'Supply up to six stage views.'},status_code=422)
    expanded = set()
    clean = []
    for tile in tiles:
        if not isinstance(tile,dict): return JSONResponse({'error':'Invalid stage view.'},status_code=422)
        node,project = tile.get('node'),tile.get('project')
        view = tile.get('view') or ('both' if project else 'screen')
        if not isinstance(node,str) or not node or len(node)>100 or (project is not None and (not isinstance(project,str) or not project or len(project)>64)) or view not in ('screen','surface','both'):
            return JSONResponse({'error':'Each view needs a machine and a valid screen/surface selection.'},status_code=422)
        if view in ('screen','both'): expanded.add((node,None,'screen'))
        if view in ('surface','both'): expanded.add((node,project,'surface'))
        clean.append({'node':node,'project':project,'view':view})
    if len(expanded)>6:
        return JSONResponse({'error':'The stage holds six views; Both uses two. Choose a smaller composition.'},status_code=422)
    await notify_control({"type": "stage", "display": body.get("display"), "tiles": clean, "by": str(body.get('by') or '')[:128], "base_revision":body.get("base_revision"), "ts": time.time()})
    return {"ok":True,"presentation":presentation_stage.snapshot()}


@app.get("/api/control/discover")
async def control_discover(subnet: str | None = None):
    """Scan the local /24 for agentnodes (port 8444 answering /api/node/info)."""
    port = 8444
    if not subnet:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "192.168.1.1"
        subnet = str(ipaddress.ip_network(ip + "/24", strict=False))
    try:net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:raise HTTPException(422,'Supply a valid IPv4 subnet')
    if net.version!=4 or net.num_addresses>256:
        raise HTTPException(422,'Discovery supports an IPv4 subnet of at most 256 addresses')
    private_ranges=[ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
    if not any(net.subnet_of(private) for private in private_ranges):
        raise HTTPException(422,'Discovery is limited to private LAN addresses')
    known = {urlsplit(n['url']).hostname for n in config.nodes()}
    found = []
    slots=asyncio.Semaphore(16)

    async def probe(ip):
        async with slots:await probe_one(ip)

    async def probe_one(ip):
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(str(ip), port), timeout=0.6)
            w.close()
            await w.wait_closed()
        except Exception:
            return
        try:
            info = await asyncio.get_event_loop().run_in_executor(None, lambda: http_json("GET", f"http://{ip}:{port}/api/node/info", timeout=3, max_bytes=65536))
            if not isinstance(info,dict) or not isinstance(info.get('name'),str) or not 1<=len(info['name'])<=100:return
            found.append({"ip": str(ip), "url": f"http://{ip}:{port}", "info": info, "known": str(ip) in known,
                          "self": info.get("name") == NODE["name"]})
        except Exception:
            pass
    await asyncio.gather(*(probe(ip) for ip in net.hosts()))
    return {"subnet": subnet, "found": [f for f in found if not f["self"]]}


@app.get("/api/control/nodes")
async def control_nodes():
    return {"items": [{**n, "token": "•••" if n.get("token") else ""} for n in config.nodes()]}


@app.post("/api/control/nodes")
async def control_add_node(req: Request):
    body = await json_object(req)
    if os.environ.get('LODGE_MODE') == '1':
        name = body.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', name):
            raise HTTPException(422, 'Use a machine name with letters, numbers, dashes or underscores.')
        if name == NODE['name'] or any(n['name'] == name for n in config.nodes()):
            raise HTTPException(409, 'That machine name is already connected. Choose another name.')
        if not body.get('token'):
            raise HTTPException(422, 'A machine connection token is required.')
    existing = next((n for n in config.nodes() if n['name'] == body.get('name')), {})
    n = dict(existing)
    for field in ('name','url','token','ssh','ca','ca_file','surface_gateway_port'):
        if field in body:
            value = body[field]
            if field == 'token' and value == '•••': continue
            if field == 'surface_gateway_port':
                if type(value) is not int or not 1 <= value <= 65535: raise HTTPException(422,'Invalid gateway port')
            elif not isinstance(value,str) or len(value)>4096: raise HTTPException(422,'Invalid node '+field)
            n[field] = value
    n.setdefault('name','');n.setdefault('token','')
    n['url'] = str(n.get('url','')).rstrip('/')
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, lambda: _remote(n, "GET", "/api/node/info", timeout=5))
        # token check: a protected endpoint
        await asyncio.get_event_loop().run_in_executor(None, lambda: _remote(n, "GET", "/api/projects", timeout=5))
    except Exception as e:
        return JSONResponse({"error": f"cannot reach or unauthorized: {str(e)[:120]}"}, status_code=400)
    if os.environ.get('LODGE_MODE') == '1' and not info.get('capabilities', {}).get('lodge_codex'):
        raise HTTPException(422, 'Enable Lodge mode on this machine with setup --lodge --backend codex, then restart its services.')
    if os.environ.get('LODGE_MODE') == '1':
        from . import lodge_surface
        try:
            lodge_surface.assign(n, info)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    n["name"] = n["name"] or info["name"]
    items = [x for x in config.nodes() if x["name"] != n["name"]]
    items.append(n)
    config.save_nodes(items)
    _start_relays()
    await notify_control({"type": "nodes_changed"})
    return {"ok": True, "node": {**n, "token": "•••"}, "info": info}


@app.delete("/api/control/nodes/{name}")
async def control_del_node(name: str):
    nodes = config.nodes()
    removed = [node for node in nodes if node['name'] == name]
    config.save_nodes([x for x in nodes if x["name"] != name])
    if os.environ.get('LODGE_MODE') == '1':
        from . import lodge_surface
        for node in removed:
            if node.get('lodge_surface_slot'):
                await lodge_surface.disconnect(node['lodge_surface_slot'])
    _start_relays()
    await notify_control({"type": "nodes_changed"})
    return {"ok": True}


@app.api_route("/n/{node}/{path:path}", methods=["GET", "POST", "DELETE", "PATCH"])
async def proxy_http(node: str, path: str, req: Request):
    n = _node_entry(node)
    if not n:
        return JSONResponse({"error": "unknown node"}, status_code=404)
    body = None
    if req.method in ("POST", "PATCH"):
        body = await json_object(req,allow_empty=True)
    qs = ("?" + str(req.url.query)) if req.url.query else ""

    def do():
        return _remote(n, req.method, "/" + path + qs, body, timeout=630 if "result" in path else 60, raw=True)
    try:
        r = await asyncio.get_event_loop().run_in_executor(None, do)
        return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


@app.websocket("/n/{node}/ws/{path:path}")
async def proxy_ws(ws: WebSocket, node: str, path: str):
    await ws.accept()
    if not await ws_authorized(ws):
        return
    n = _node_entry(node)
    if not n:
        await ws.close(code=4404)
        return
    query=urlencode([(key,value) for key,value in ws.query_params.multi_items() if key!='token'])
    upstream_path='/ws/'+path+('?' + query if query else '')
    pumps = []
    try:
        async with transport.websocket(n,upstream_path,max_size=32 * 1024 * 1024,ping_interval=20) as up:
            async def pump_down():
                async for m in up:
                    if not await ws_authorized(ws):return
                    if isinstance(m, bytes):
                        await ws.send_bytes(m)
                    else:
                        await ws.send_text(m)

            async def pump_up():
                while True:
                    m = await ws.receive()
                    if not await ws_authorized(ws):return
                    if m.get("type") == "websocket.disconnect":
                        return
                    if m.get("text") is not None:
                        await up.send(m["text"])
                    elif m.get("bytes") is not None:
                        await up.send(m["bytes"])
            pumps = [asyncio.create_task(pump_down()),asyncio.create_task(pump_up())]
            await asyncio.wait(pumps,return_when=asyncio.FIRST_COMPLETED)
    except Exception as e:
        log("ws proxy", node, path, str(e)[:120])
    finally:
        for task in pumps: task.cancel()
        await asyncio.gather(*pumps,return_exceptions=True)
        try: await asyncio.wait_for(ws.close(),2)
        except Exception: pass
# --------------------------------------------------------------------------
# static UI
# --------------------------------------------------------------------------
@app.get("/")
async def index(request: Request = None):
    source = (STATIC / 'index.html').read_text()
    if os.environ.get('LODGE_MODE') == '1':
        # The phone reaches this document over Tor. Keep startup in one response:
        # parallel parser-blocking asset requests can each wait for a new circuit.
        def script(match):
            path = (STATIC / match.group(1)).resolve()
            if not path.is_relative_to(STATIC.resolve()): raise ValueError('Invalid bundled script')
            text = re.sub(r'</script', r'<\\/script', path.read_text(), flags=re.I)
            return '<script>' + text + '\n</script>'
        def style(match):
            path = (STATIC / match.group(1)).resolve()
            if not path.is_relative_to(STATIC.resolve()): raise ValueError('Invalid bundled stylesheet')
            text = re.sub(r'</style', r'<\\/style', path.read_text(), flags=re.I)
            return '<style>' + text + '\n</style>'
        source = re.sub(r'<script src="/static/([^"\n]+)"></script>', script, source)
        source = re.sub(r'<link rel="stylesheet" href="/static/([^"\n]+)">', style, source)
    hashes = ['\'sha256-' + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + '\''
              for s in re.findall(r'<script>([\s\S]*?)</script>', source)]
    policy = ("default-src 'self'; script-src 'self' " + ' '.join(hashes) +
              " https://cdn.jsdelivr.net blob: 'wasm-unsafe-eval'; script-src-attr 'none'; "
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
              "img-src 'self' data: blob: https: http:; media-src 'self' blob:; connect-src 'self' https: wss: http: ws:; "
              "frame-src https: http:; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    headers = {'Content-Security-Policy': policy, 'Referrer-Policy': 'no-referrer',
               'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store', 'Vary': 'Accept-Encoding'}
    if request is not None and any(part.strip().split(';')[0] == 'gzip' and not re.search(r';\s*q=0(?:\.0*)?\s*$', part)
                                   for part in request.headers.get('accept-encoding', '').split(',')):
        import gzip
        headers['Content-Encoding'] = 'gzip'
        source = gzip.compress(source.encode(), compresslevel=6)
    return Response(source, media_type='text/html', headers=headers)


@app.get("/static/{path:path}")
async def static_file(path: str):
    p = (STATIC / path).resolve()
    if not p.is_relative_to(STATIC.resolve()) or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


# --------------------------------------------------------------------------
# main: http + https in one loop, signals on the main thread
# --------------------------------------------------------------------------
def main():
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=NODE["port"], log_level="warning", ws_max_size=32 * 1024 * 1024))]
    cert, key = config.TLS / "cert.pem", config.TLS / "key.pem"
    if NODE.get('tls_port', 0) > 0 and cert.exists() and key.exists():
        servers.append(uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=NODE["tls_port"], log_level="warning",
                                                     ws_max_size=32 * 1024 * 1024, ssl_certfile=str(cert), ssl_keyfile=str(key))))
        log(f"https on :{NODE['tls_port']}")

    def stop(*_):
        for srv in servers:
            srv.should_exit = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    async def run_all():
        await asyncio.gather(*(srv.serve() for srv in servers))
    t = threading.Thread(target=lambda: asyncio.run(run_all()), daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.5)


if __name__ == "__main__":
    main()
