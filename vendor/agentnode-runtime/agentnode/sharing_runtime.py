"""Optional Tor and narrow listeners; no model process is started by sharing."""
import asyncio
import os
import shutil
from pathlib import Path
import uvicorn
from fastapi import HTTPException
from . import config
from .storage import read_json,write_json,private_dir,atomic_text

SERVERS={}
RELAYS={}
CONNECTIONS=set()
TOR=None
READY=False
LOCK=asyncio.Lock()
ROOT=config.HOME/'sharing-tor'
SETTINGS=config.HOME/'sharing-transport.json'

def state():
    global READY
    settings=read_json(SETTINGS,{})
    address=ROOT/'service'/'hostname'
    alive=TOR is not None and TOR.returncode is None
    log=ROOT/'tor.log'
    if not alive:READY=False
    if alive and not READY and log.is_file():
        with log.open('rb') as stream:
            stream.seek(max(0,log.stat().st_size-8192));READY=b'Bootstrapped 100%' in stream.read()
    url='http://'+address.read_text().strip() if READY and address.is_file() else None
    return {'enabled':settings.get('enabled',False),'installed':bool(shutil.which('tor')),'running':alive,'url':url}

async def listen(key,app,port,*,tls=False,host='127.0.0.1'):
    if key in SERVERS:return
    opts={}
    if tls:
        cert,keyfile=config.TLS/'cert.pem',config.TLS/'key.pem'
        if not cert.is_file() or not keyfile.is_file():raise HTTPException(503,'Conductor HTTPS certificates are required for shared Surface')
        opts.update(ssl_certfile=str(cert),ssl_keyfile=str(keyfile))
    server=uvicorn.Server(uvicorn.Config(app,host=host,port=port,log_level='warning',access_log=False,lifespan='off',ws_max_size=4*1024*1024,**opts))
    # Binding explicitly lets a failed optional listener report an error instead
    # of allowing uvicorn's startup SystemExit to terminate Conductor.
    import socket
    sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try:sock.bind((host,port));sock.listen(128);sock.setblocking(False)
    except OSError:
        sock.close();raise HTTPException(503,f'Sharing port {port} is already in use') from None
    task=asyncio.create_task(server.serve(sockets=[sock]))
    SERVERS[key]=(server,task,sock)

async def start():
    global TOR,READY
    from .sharing import gateway_app,REGISTRY
    settings=read_json(SETTINGS,{})
    if not settings.get('enabled'):return
    executable=shutil.which('tor')
    if not executable:raise HTTPException(503,'Install Tor on this Conductor, then enable sharing')
    await listen('gateway',gateway_app(),settings['gateway_port'])
    private_dir(ROOT);private_dir(ROOT/'data');private_dir(ROOT/'service')
    # All targets are local, fixed by the operator's node configuration.
    torrc=ROOT/'torrc'
    atomic_text(torrc,f'DataDirectory "{ROOT / "data"}"\nSocksPort 127.0.0.1:{settings["socks_port"]}\nHiddenServiceDir "{ROOT / "service"}"\nHiddenServicePort 80 127.0.0.1:{settings["gateway_port"]}\nSafeLogging 1\nLog notice file "{ROOT / "tor.log"}"\n')
    if TOR is None or TOR.returncode is not None:
        READY=False
        atomic_text(ROOT/'tor.log','')
        TOR=await asyncio.create_subprocess_exec(executable,'-f',str(torrc),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    for key,r in REGISTRY.read()['imports'].items():
        if not r.get('pending'):await ensure_import(key)

async def enable():
    async with LOCK:
        n=config.node()
        if not n.get('control'):raise HTTPException(403,'Enable sharing on a Conductor')
        if not shutil.which('tor'):raise HTTPException(503,'Tor is not installed on this Conductor. Install the tor package, then retry.')
        settings=read_json(SETTINGS,{})
        settings.update(enabled=True,gateway_port=n['port']+200,socks_port=n['port']+201)
        if settings['socks_port']>65535:raise HTTPException(422,'Configure lower Conductor ports before enabling sharing')
        write_json(SETTINGS,settings);await start();return state()

async def ensure_import(key):
    from .sharing import REGISTRY,surface_app
    n=config.node()
    def assign(d):
        r=d['imports'].get(key)
        if not r:raise HTTPException(404,'Unknown shared agent')
        # Keep allocated origins after removal so a stale tab never gains another agent.
        slots=d.setdefault('surface_slots',{})
        if key not in slots:
            slot=next((i for i in range(1,17) if i not in slots.values()),None)
            if slot is None:raise HTTPException(409,'All 16 shared Surface origins are allocated')
            slots[key]=slot
        return slots[key]
    slot=REGISTRY.update(assign)
    port=n['port']+300+2*slot;tls_port=n.get('tls_port',0)+300+2*slot if n.get('tls_port') else None
    app=surface_app(key)
    await listen('surface-'+key,app,port)
    if tls_port:await listen('surface-tls-'+key,app,tls_port,tls=True,host='0.0.0.0')
    if os.environ.get('LODGE_MODE')=='1' and slot not in RELAYS:
        async def accept(reader,writer):await relay(key,port,reader,writer)
        RELAYS[slot]=await asyncio.start_server(accept,'0.0.0.0' if n.get('lodge_container') else '127.0.0.1',8900+slot)
    return {'port':port,'tls_port':tls_port,'slot':slot}

async def relay(key,port,reader,writer):
    # Fixed local destination; authentication stays at the isolated Surface app.
    from .sharing import REGISTRY
    task=asyncio.current_task();upstream=None;pumps=[]
    if len(CONNECTIONS)>=128:writer.close();return
    CONNECTIONS.add(task)
    try:
        REGISTRY.imported(key)
        remote,upstream=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',port),10)
        async def pipe(source,destination):
            while chunk:=await asyncio.wait_for(source.read(65536),120):
                destination.write(chunk);await asyncio.wait_for(destination.drain(),30)
        pumps=[asyncio.create_task(pipe(reader,upstream)),asyncio.create_task(pipe(remote,writer))]
        await asyncio.wait(pumps,return_when=asyncio.FIRST_COMPLETED)
    except (OSError,TimeoutError,HTTPException):pass
    finally:
        for pump in pumps:pump.cancel()
        await asyncio.gather(*pumps,return_exceptions=True)
        writer.close()
        if upstream:upstream.close()
        CONNECTIONS.discard(task)

async def stop():
    global TOR
    for relay_server in RELAYS.values():relay_server.close()
    await asyncio.gather(*(s.wait_closed() for s in RELAYS.values()))
    RELAYS.clear()
    tasks=list(CONNECTIONS)
    for task in tasks:task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)
    from .sharing import PROBES
    for task in list(PROBES.values()):task.cancel()
    await asyncio.gather(*list(PROBES.values()),return_exceptions=True)
    PROBES.clear()
    if TOR and TOR.returncode is None:
        TOR.terminate()
        try:await asyncio.wait_for(TOR.wait(),10)
        except TimeoutError:TOR.kill();await TOR.wait()
    TOR=None
    for server,task,sock in SERVERS.values():server.should_exit=True
    if SERVERS:
        try:await asyncio.wait_for(asyncio.gather(*(t for s,t,k in SERVERS.values()),return_exceptions=True),10)
        except TimeoutError:pass
    for server,task,sock in SERVERS.values():
        task.cancel();sock.close()
    SERVERS.clear()
