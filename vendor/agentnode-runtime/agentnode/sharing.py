"""Revocable agent references. Fleet credentials never cross the sharing boundary."""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

import requests
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from . import auth, config, transport
from .storage import read_json, locked, atomic_text
from .validation import body_bytes

ROLES = ('observer', 'collaborator', 'manager')
STATUS_KEYS = {'status','alive','running','model','backend','agent_name','surface_ws','queued','approval','tool','turn_id','turn_started'}
SURFACE_PATHS = ('shell/', 'assets/', 'apps/', 'local-apps/')

def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def identifier(value): return isinstance(value,str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value))
def onion_origin(value):
    p=urlsplit(value)
    if p.scheme!='http' or not p.hostname or not re.fullmatch(r'[a-z2-7]{56}\.onion',p.hostname) or p.port not in (None,80) or p.username or p.password or p.path or p.query or p.fragment:
        raise ValueError('Use a Tor v3 onion origin, without a path or credentials')
    return 'http://'+p.hostname

def public(record):
    return {k:record[k] for k in ('id','name','recipient','role','created','expires','revoked','redeemed','node','project','agent','url') if k in record}

class Registry:
    def __init__(self, home=None): self.path=Path(home or config.HOME)/'sharing.json'
    def read(self): return read_json(self.path,{'exports':{},'imports':{}})
    def update(self, function):
        with locked(self.path):
            data=self.read();result=function(data)
            atomic_text(self.path,json.dumps(data)+'\n')
            return result
    def grant(self, credential):
        if not isinstance(credential,str) or len(credential)>200: raise HTTPException(401,'Invalid shared-agent credential')
        key=digest(credential)
        record=next((r for r in self.read()['exports'].values() if hmac.compare_digest(r.get('credential',''),key)),None)
        if not record or record.get('revoked') or record['expires']<=time.time(): raise HTTPException(401,'Shared-agent access expired or was revoked')
        return record
    def imported(self, key):
        record=self.read()['imports'].get(key)
        if not record: raise HTTPException(404,'Shared agent was removed')
        return record
    def invite(self, target, *, url, name, recipient, role, days):
        if role not in ROLES or type(days)!=int or not 1<=days<=90: raise HTTPException(422,'Choose a role and expiry of 1–90 days')
        if not isinstance(recipient,str) or not 1<=len(recipient.strip())<=80: raise HTTPException(422,'Enter a recipient name')
        secret=secrets.token_urlsafe(32);key=secrets.token_hex(12);now=time.time()
        record=dict(target,id=key,name=name[:100],recipient=recipient.strip(),role=role,created=now,expires=now+days*86400,
                    invitation=digest(secret),invite_expires=now+900,redeemed=False,revoked=False)
        self.update(lambda d:d['exports'].__setitem__(key,record))
        code='agentnode-share-v1.'+base64.urlsafe_b64encode(json.dumps({'url':onion_origin(url),'id':key,'secret':secret}).encode()).decode().rstrip('=')
        return dict(public(record),code=code,invite_expires=record['invite_expires'])
    def redeem(self, key, secret, credential):
        if not identifier(key) or not isinstance(secret,str) or not isinstance(credential,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',credential): raise HTTPException(422,'Invalid invitation exchange')
        def change(d):
            r=d['exports'].get(key)
            if not r or r['revoked'] or r['expires']<=time.time() or r['invite_expires']<=time.time() or not hmac.compare_digest(r['invitation'],digest(secret)):
                raise HTTPException(401,'Invitation expired or was revoked')
            # An uncertain response can be retried by the same importing Conductor.
            if r['redeemed'] and not hmac.compare_digest(r['credential'],digest(credential)): raise HTTPException(409,'Invitation has already been used')
            r.update(redeemed=True,credential=digest(credential))
            return public(r)
        return self.update(change)
    def revoke(self,key):
        def change(d):
            if key not in d['exports']:raise HTTPException(404,'Unknown share')
            d['exports'][key]['revoked']=True
        self.update(change)

REGISTRY=Registry()

def parse_invite(code):
    try:
        prefix,encoded=code.strip().split('.',1)
        if prefix!='agentnode-share-v1' or len(encoded)>2048:raise ValueError()
        data=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        if set(data)!={'url','id','secret'} or not identifier(data['id']) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',data['secret']):raise ValueError()
        data['url']=onion_origin(data['url']);return data
    except (ValueError,TypeError,AttributeError,KeyError):raise HTTPException(422,'Invalid shared-agent invitation') from None

def socks_url():
    settings=read_json(config.HOME/'sharing-transport.json',{})
    if not settings.get('enabled'):raise HTTPException(503,'Enable Tor sharing in Settings first')
    return f"socks5h://127.0.0.1:{settings['socks_port']}"

def remote(record, method, path, body=None, timeout=45):
    url=onion_origin(record['url'])+path
    with requests.Session() as client:
        client.trust_env=False
        response=client.request(method,url,json=body,headers={'Authorization':'Bearer '+record.get('credential','')},
            proxies={'http':socks_url(),'https':socks_url()},timeout=timeout,allow_redirects=False)
        if 300<=response.status_code<400:raise HTTPException(502,'Shared-agent redirects are not allowed')
        return response

async def remote_json(record,method,path,body=None):
    try:
        r=await asyncio.to_thread(remote,record,method,path,body)
        data=r.json()
        if r.status_code>=400:raise HTTPException(r.status_code,data.get('error','Shared agent unavailable'))
        return data
    except HTTPException:raise
    except Exception:raise HTTPException(502,'Shared agent is unavailable over Tor; retry when its owner is online') from None

async def import_invite(code):
    invite=parse_invite(code)
    # Persist before sending so retrying an uncertain one-use redemption is safe.
    key=digest(invite['url']+'\0'+invite['id'])[:24]
    def prepare(d):
        if key not in d['imports']:
            d['imports'][key]=dict(id=key,url=invite['url'],credential=secrets.token_urlsafe(32),pending=True)
        return d['imports'][key]
    record=REGISTRY.update(prepare)
    result=await remote_json(record,'POST','/v1/redeem',{'id':invite['id'],'secret':invite['secret'],'credential':record['credential']})
    status=await remote_json(record,'GET','/v1/status')
    def finish(d):
        r=d['imports'][key];r.update(status=status_view(status),reachable=True,checked=time.time());r.update({k:result[k] for k in ('name','recipient','role','expires')});r['pending']=False
        return public(r)
    return REGISTRY.update(finish)

def peer_entries():
    return [dict(name='shared-'+r['id'],shared_id=r['id']) for r in REGISTRY.read()['imports'].values() if not r.get('pending')]

def peer(name):return next((n for n in peer_entries() if n['name']==name),None)

def status_view(data):
    return {**{k:v for k,v in data.items() if k in STATUS_KEYS},'type':'status','project':'shared','agent':'shared','agents':1}

def mapped_path(method,path,body=None):
    p=urlsplit(path);q=parse_qs(p.query)
    base='/api/projects/shared'
    if method=='GET' and p.path in (base,base+'/agents/shared/status'):return '/v1/status',None
    if method=='GET' and p.path==base+'/agents/shared/history':
        if set(q)-{'before','after','limit','event'}:raise HTTPException(403,'Unsupported shared history query')
        return '/v1/history'+('?' + p.query if p.query else ''),None
    if method=='GET' and p.path==base+'/result':return '/v1/result'+('?' + p.query if p.query else ''),None
    if method=='POST' and p.path in tuple(base+'/'+a for a in ('send','start','stop','restart','interrupt')):
        action=p.path.rsplit('/',1)[1];b=body or {}
        if b.get('expected_agent','shared')!='shared':raise HTTPException(409,'Shared agent selection changed')
        if action=='send':
            if b.get('files') or b.get('annotations'):raise HTTPException(422,'Shared-agent attachments are not supported yet')
            return '/v1/send',{k:b[k] for k in ('text','request_id') if k in b}
        return '/v1/'+action,{}
    raise HTTPException(403,'This connection only permits its assigned shared agent')

def peer_request(node,method,path,body=None,timeout=45):
    record=REGISTRY.imported(node['shared_id']);p=urlsplit(path).path
    if method=='GET' and p=='/api/projects/shared/agents':
        r=remote(record,'GET','/v1/status',timeout=timeout)
        if r.status_code==200:
            s=status_view(r.json());r._content=json.dumps({'active':'shared','items':[dict(s,id='shared',name=record['name'])]}).encode()
        return r
    target,payload=mapped_path(method,path,body)
    r=remote(record,method,target,payload,timeout=max(timeout,45))
    if r.status_code==200 and target=='/v1/status':r._content=json.dumps(status_view(r.json())).encode()
    return r

PROBES={}
async def tree_nodes():
    # An offline onion must never hold up the local fleet or its chat history.
    async def refresh(key):
        try:
            r=REGISTRY.imported(key)
            status=status_view(await remote_json(r,'GET','/v1/status'))
            REGISTRY.update(lambda d:d['imports'].get(key,{}).update(status=status,reachable=True,error=None,checked=time.time()))
        except HTTPException as exc:
            REGISTRY.update(lambda d:d['imports'].get(key,{}).update(reachable=False,error=exc.detail,checked=time.time()))
        finally:PROBES.pop(key,None)
    result=[]
    for n in peer_entries():
        r=REGISTRY.imported(n['shared_id']);cached=r.get('status',{});reachable=r.get('reachable',False)
        if time.time()-r.get('checked',0)>15 and r['id'] not in PROBES:
            PROBES[r['id']]=asyncio.create_task(refresh(r['id']))
        result.append(dict(n,shared=True,role=r['role'],recipient=r['recipient'],reachable=reachable,error=r.get('error'),info={'host_tools':False},
            hub_port=True,projects=[dict(cached,id='shared',name=r['name'],dir='Shared by its owner',alive=cached.get('alive',False) and reachable)]))
    return result

async def body(req, allowed):
    try: value=json.loads(await body_bytes(req,128*1024))
    except (ValueError,UnicodeError):raise HTTPException(422,'Expected a JSON object') from None
    if not isinstance(value,dict) or set(value)-set(allowed):raise HTTPException(422,'Unsupported request fields')
    return value

class Gateway:
    def __init__(self, registry=None):self.registry=registry or REGISTRY
    def target(self,r):
        n=config.node()
        node=({'name':n['name'],'url':f"http://127.0.0.1:{n['port']}",'token':n['token']} if r['node']==n['name'] else next((x for x in config.nodes() if x['name']==r['node']),None))
        if not node or digest(node['url']+'\0'+node['token'])!=r['binding']:raise HTTPException(409,'The owner must renew this share after the machine identity changes')
        return node
    async def request(self,r,method,path,payload=None):
        try:
            response=await asyncio.to_thread(transport.request,self.target(r),method,path,payload,30)
            data=response.json()
            if response.status_code>=400:raise HTTPException(response.status_code,data.get('error','Agent unavailable'))
            return data
        except HTTPException:raise
        except Exception:raise HTTPException(502,'The owner’s agent is currently unavailable') from None
    async def status(self,r):
        # Pin the named agent's creation identity as well as its worker credential.
        agents=await self.request(r,'GET',f"/api/projects/{r['project']}/agents")
        agent=next((a for a in agents['items'] if a['id']==r['agent']),None)
        if not agent or agent.get('created')!=r['agent_created']:raise HTTPException(409,'The shared agent was replaced; ask its owner for a new invitation')
        s=await self.request(r,'GET',f"/api/projects/{r['project']}/agents/{r['agent']}/status")
        return s
    async def surface(self,r):
        s=await self.status(r);workspace=s.get('surface_ws')
        if not workspace:raise HTTPException(404,'This agent has no Surface yet')
        n=self.target(r)
        info=await self.request(r,'GET','/api/node/info')
        parsed=urlsplit(n['url']);port=info.get('hub_tls_port' if parsed.scheme=='https' else 'hub_port')
        if type(port)!=int or not 1<=port<=65535:raise HTTPException(503,'The agent’s Surface is unavailable')
        host='['+parsed.hostname+']' if ':' in parsed.hostname else parsed.hostname
        return dict(n,url=f'{parsed.scheme}://{host}:{port}',token=auth.capability(n['token'],'surface','presenter',ws=[workspace])),workspace

GATEWAY=Gateway()

def credential(headers):
    value=headers.get('authorization','')
    return value[7:] if value.startswith('Bearer ') else ''

def check_surface_path(path):
    if path and not path.startswith(SURFACE_PATHS) and path not in ('__surface/bridge.js','api/workspaces','api/snapshot','favicon.ico'):
        raise HTTPException(403,'This resource is outside the shared Surface')
    if '\\' in path or any(x in ('.','..') for x in path.split('/')):raise HTTPException(403,'Invalid Surface path')

async def until_revoked(ws,record,valid,upstream,*,surface=False,identity=None):
    async def down():
        async for message in upstream:
            valid()
            if isinstance(message,bytes):await ws.send_bytes(message)
            else:
                if not surface:
                    event=json.loads(message)
                    if event.get('type')=='status':message=json.dumps(status_view(event))
                await ws.send_text(message)
    async def up():
        while True:
            message=await ws.receive_json();valid()
            if surface and message.get('type')=='resync':await upstream.send('{"type":"resync"}')
            elif message.get('type')=='ping':await ws.send_json({'type':'pong'})
    async def expiry():
        while True:
            await asyncio.sleep(1);valid()
    async def identities():
        while True:
            await identity();await asyncio.sleep(10)
    tasks=[asyncio.create_task(f()) for f in (down,up,expiry)]
    if identity:tasks.append(asyncio.create_task(identities()))
    try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

# The public onion has only these explicitly scoped routes, never the owner API.
def gateway_app(gateway=None):
    g=gateway or GATEWAY;app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.exception_handler(HTTPException)
    async def error(req,exc):return JSONResponse({'error':exc.detail},status_code=exc.status_code,headers={'Cache-Control':'no-store'})
    @app.middleware('http')
    async def boundary(req,call_next):
        if req.headers.get('origin') or req.headers.get('sec-fetch-site'):
            return JSONResponse({'error':'Use your Conductor to access shared agents'},status_code=403)
        result=await call_next(req);result.headers['Cache-Control']='no-store';return result
    def grant(req):return g.registry.grant(credential(req.headers))
    @app.post('/v1/redeem')
    async def redeem(req:Request):
        b=await body(req,('id','secret','credential'))
        return g.registry.redeem(b.get('id'),b.get('secret'),b.get('credential'))
    @app.get('/v1/status')
    async def status(req:Request):
        r=grant(req);s=await g.status(r);grant(req)
        return dict(status_view(s),name=r['name'],role=r['role'],recipient=r['recipient'],expires=r['expires'])
    @app.get('/v1/history')
    async def history(req:Request):
        r=grant(req);await g.status(r)
        if set(req.query_params)-{'before','after','limit','event'}:raise HTTPException(422,'Unsupported history query')
        result=await g.request(r,'GET',f"/api/projects/{r['project']}/agents/{r['agent']}/history?"+str(req.query_params))
        grant(req)
        return result
    @app.get('/v1/result')
    async def result(req:Request):
        r=grant(req);await g.status(r)
        turn=req.query_params.get('turn_id','')
        if not turn.startswith('share-'+r['id']+'-') or not identifier(turn):raise HTTPException(403,'This receipt belongs to another participant')
        # Bounded polls also recheck expiry/revocation on every retry.
        try:wait=min(20,max(0,int(req.query_params.get('wait','0'))))
        except ValueError:raise HTTPException(422,'Invalid result wait') from None
        result=await g.request(r,'GET',f"/api/projects/{r['project']}/result?"+urlencode({'turn_id':turn,'wait':wait}))
        grant(req)
        return result
    @app.post('/v1/{action}')
    async def action(action:str,req:Request):
        r=grant(req)
        if action not in ('send','start','stop','restart','interrupt'):raise HTTPException(404,'Unknown shared action')
        if r['role']=='observer' or (action!='send' and r['role']!='manager'):raise HTTPException(403,'The owner has not granted this action')
        b=await body(req,('text','request_id') if action=='send' else ())
        s=await g.status(r)
        payload={'expected_agent':r['agent']}
        if action=='send':
            if not s.get('alive'):raise HTTPException(409,'The owner must start this agent before it can receive messages')
            if not isinstance(b.get('text'),str) or not 1<=len(b['text'].strip())<=100000 or not identifier(b.get('request_id')):raise HTTPException(422,'A message and stable request ID are required')
            payload.update(text=b['text'],request_id='share-'+r['id']+'-'+digest(b['request_id'])[:40],origin=r['recipient'],request_source='chat',require_running=True)
        grant(req) # Recheck after network I/O, immediately before the mutation.
        return await g.request(r,'POST',f"/api/projects/{r['project']}/{action}",payload)
    @app.get('/v1/surface/{path:path}')
    async def surface(path:str,req:Request):
        r=grant(req);check_surface_path(path);node,workspace=await g.surface(r)
        q={k:v for k,v in req.query_params.items() if k not in ('access','token','ws')};q['ws']=workspace
        response=await asyncio.to_thread(transport.request,node,'GET','/'+path+'?'+urlencode(q),None,30)
        grant(req)
        return Response(response.content,status_code=response.status_code,headers={k:v for k,v in response.headers.items() if k.lower() in ('content-type','content-disposition')})
    @app.websocket('/v1/chat')
    async def chat(ws:WebSocket):
        try:
            r=grant(ws);await g.status(r)
            if ws.headers.get('origin'):raise HTTPException(403,'Conductor connection required')
            await ws.accept()
            query={k:v for k,v in ws.query_params.items() if k=='after'}
            path=f"/ws/projects/{r['project']}/agents/{r['agent']}/chat?"+urlencode(query)
            async with transport.websocket(g.target(r),path,max_size=4*1024*1024) as upstream:
                await until_revoked(ws,r,lambda:grant(ws),upstream,identity=lambda:g.status(r))
        except Exception:pass
        finally:
            try:await ws.close(code=4401)
            except Exception:pass
    @app.websocket('/v1/surface-ws')
    async def surface_ws(ws:WebSocket):
        try:
            r=grant(ws);node,workspace=await g.surface(r)
            if ws.headers.get('origin'):raise HTTPException(403,'Conductor connection required')
            await ws.accept()
            async with transport.websocket(node,'/ws?'+urlencode({'ws':workspace,'view':'presenter','presentation':'shared'}),max_size=4*1024*1024) as upstream:
                async def identity():
                    if (await g.status(r)).get('surface_ws')!=workspace:raise HTTPException(409,'Shared workspace changed')
                await until_revoked(ws,r,lambda:grant(ws),upstream,surface=True,identity=identity)
        except Exception:pass
        finally:
            try:await ws.close(code=4401)
            except Exception:pass
    return app

async def shared_chat(ws,node,path,authorized=None):
    record=REGISTRY.imported(node['shared_id'])
    parsed=urlsplit(path)
    if parsed.path!='/ws/projects/shared/agents/shared/chat':
        await ws.close(code=4403);return
    from websockets import connect
    query={k:v[0] for k,v in parse_qs(parsed.query).items() if k=='after'}
    try:
        async with transport.DirectConnect('ws'+onion_origin(record['url'])[4:]+'/v1/chat?'+urlencode(query),
                additional_headers={'Authorization':'Bearer '+record['credential']},proxy=socks_url(),max_size=4*1024*1024) as upstream:
            async def identity():
                if authorized and not await authorized(ws):raise HTTPException(401,'Conductor access expired')
            await until_revoked(ws,record,lambda:REGISTRY.imported(record['id']),upstream,identity=identity)
    except Exception:
        try:await ws.close(code=1013,reason='Shared agent unavailable or access revoked')
        except Exception:pass


def surface_app(key):
    """One browser origin per import. Never relay the owner's credentials to JS."""
    from http.cookies import SimpleCookie
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    cookie_name='shared_surface_'+key
    def access(headers,query):
        value=headers.get('x-agentnode-token') or query.get('access')
        if not value:
            try:jar=SimpleCookie();jar.load(headers.get('cookie',''));value=jar[cookie_name].value
            except Exception:value=''
        claim=auth.claims(value,config.node()['token'],'shared-surface')
        if not claim or claim.get('import_id')!=key:raise HTTPException(401,'Reopen this shared Surface from Conductor')
        r=REGISTRY.imported(key)
        if r['expires']<=time.time():raise HTTPException(401,'Shared agent access expired')
        return r
    def trusted(headers,scheme):
        if headers.get('sec-fetch-site')=='cross-site':return False
        origin=headers.get('origin')
        scheme={'ws':'http','wss':'https'}.get(scheme,scheme)
        return origin is None or origin==scheme+'://'+headers.get('host','')
    @app.exception_handler(HTTPException)
    async def error(req,exc):return JSONResponse({'error':exc.detail},status_code=exc.status_code)
    @app.middleware('http')
    async def boundary(req,call_next):
        if not trusted(req.headers,req.url.scheme):return JSONResponse({'error':'Untrusted browser origin'},status_code=403)
        response=await call_next(req)
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self' data: blob:; object-src 'none'; base-uri 'self'; form-action 'none'"})
        return response
    @app.post('/api/auth/session')
    async def login(req:Request):
        r=access(req.headers,req.query_params)
        await remote_json(r,'GET','/v1/status')
        response=JSONResponse({'ok':True})
        response.set_cookie(cookie_name,req.headers.get('x-agentnode-token',''),max_age=21600,secure=req.url.scheme=='https',httponly=True,samesite='strict')
        return response
    @app.get('/{path:path}')
    async def read(path:str,req:Request):
        try:r=access(req.headers,req.query_params)
        except HTTPException:
            if path:raise
            return Response('''<!doctype html><meta name="referrer" content="no-referrer"><p id="status">Open this Surface from Conductor.</p><script>
const token=new URLSearchParams(location.hash.slice(1)).get('access');
if(token)fetch('/api/auth/session',{method:'POST',headers:{'X-AgentNode-Token':token}}).then(r=>{if(!r.ok)throw Error('Access expired. Reopen from Conductor.');location.reload();}).catch(e=>document.querySelector('#status').textContent=e.message);
</script>''',media_type='text/html')
        check_surface_path(path)
        query={k:v for k,v in req.query_params.items() if k not in ('access','token','ws')}
        query.update(view='presenter',embed='1',presentation='shared')
        try:response=await asyncio.to_thread(remote,r,'GET','/v1/surface/'+path+'?'+urlencode(query))
        except Exception:raise HTTPException(502,'Shared Surface is unavailable over Tor') from None
        return Response(response.content,status_code=response.status_code,headers={k:v for k,v in response.headers.items() if k.lower() in ('content-type','content-disposition')})
    @app.websocket('/ws')
    async def stream(ws:WebSocket):
        try:
            if not trusted(ws.headers,ws.url.scheme):raise HTTPException(403,'Untrusted browser origin')
            r=access(ws.headers,ws.query_params);await ws.accept()
            async with transport.DirectConnect('ws'+onion_origin(r['url'])[4:]+'/v1/surface-ws',
                    additional_headers={'Authorization':'Bearer '+r['credential']},proxy=socks_url(),max_size=4*1024*1024) as upstream:
                await until_revoked(ws,r,lambda:access(ws.headers,ws.query_params),upstream,surface=True)
        except Exception:pass
        finally:
            try:await ws.close(code=4401)
            except Exception:pass
    return app


def install_owner_routes(app,server):
    @app.get('/api/sharing')
    async def listing():
        from .sharing_runtime import state
        d=REGISTRY.read()
        return {'exports':[public(r) for r in d['exports'].values()], 'imports':[public(r) for r in d['imports'].values() if not r.get('pending')], 'transport':state()}
    @app.post('/api/sharing/enable')
    async def enable(req:Request):
        from .sharing_runtime import enable
        await body(req,())
        return await enable()
    @app.post('/api/sharing/exports')
    async def export(req:Request):
        from .sharing_runtime import state
        b=await body(req,('node','project','agent','recipient','role','days','include_history'))
        if b.get('include_history') is not True:raise HTTPException(422,'Confirm that the recipient may read this agent’s existing history and Surface')
        if not all(identifier(b.get(k)) for k in ('node','project','agent')):raise HTTPException(422,'Select a machine, project and named agent')
        n=server._node_entry(b['node'])
        if not n or n.get('shared_id'):raise HTTPException(403,'Only owned agents can be shared')
        url=state().get('url')
        if not url:raise HTTPException(503,'Tor is still connecting; wait until sharing is ready')
        agents=await asyncio.to_thread(server._remote,n,'GET',f"/api/projects/{b['project']}/agents")
        a=next((a for a in agents['items'] if a['id']==b['agent']),None)
        if not a or not a.get('created'):raise HTTPException(409,'This agent needs a compatible worker before it can be shared')
        target=dict(node=n['name'],project=b['project'],agent=b['agent'],agent_created=a['created'],binding=digest(n['url']+'\0'+n['token']))
        return REGISTRY.invite(target,url=url,name=a['name'],recipient=b.get('recipient'),role=b.get('role'),days=b.get('days'))
    @app.delete('/api/sharing/exports/{key}')
    async def revoke(key:str):REGISTRY.revoke(key);return {'ok':True}
    @app.post('/api/sharing/imports')
    async def importing(req:Request):
        from .sharing_runtime import ensure_import
        b=await body(req,('code',));r=await import_invite(b.get('code'))
        await ensure_import(r['id']);await server.notify_control({'type':'nodes_changed'})
        return r
    @app.delete('/api/sharing/imports/{key}')
    async def remove(key:str):
        REGISTRY.update(lambda d:d['imports'].pop(key,None));await server.notify_control({'type':'nodes_changed'});return {'ok':True}
    @app.post('/api/sharing/imports/{key}/surface')
    async def surface_access(key:str,req:Request):
        await body(req,())
        return await imported_surface_access(key)


async def imported_surface_access(key, workspace=None):
    from .sharing_runtime import ensure_import
    r=REGISTRY.imported(key)
    status=await remote_json(r,'GET','/v1/status')
    assigned=status.get('surface_ws')
    if not assigned:raise HTTPException(404,'The shared agent has no Surface yet')
    if workspace is not None and workspace!=assigned:raise HTTPException(403,'The shared workspace changed')
    ports=await ensure_import(key)
    return dict(ports,workspace=assigned,access=auth.capability(config.node()['token'],'shared-surface','presenter',import_id=key))
