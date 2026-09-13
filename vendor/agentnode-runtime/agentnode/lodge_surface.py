"""Separate browser origins for every machine's Surface, through the box's onion.

Slot zero is the local Surface. Remote slots 1..16 are stable, never reassigned
when disconnecting a node (old browser origins must not silently become another
machine). No browser token is injected by these byte relays: Surface verifies its
own short-lived capability. HTTPS peers retain certificate verification.
"""
import asyncio
import ssl
from urllib.parse import urlsplit
from . import config, transport
from .storage import read_json, write_json

MAX_REMOTE = 16
SERVERS = []
CONNECTIONS = {}


def assign(node, info):
    secure = urlsplit(transport.origin(node)).scheme == 'https'
    port = info.get('hub_tls_port' if secure else 'hub_port')
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError('Machine returned an invalid Surface port')
    records = read_json(config.HOME / 'surface-slots.json', {})
    # An identity is its origin + token, not its user-editable display name.
    import hashlib
    identity = hashlib.sha256((transport.origin(node) + '\0' + node['token']).encode()).hexdigest()
    if identity not in records:
        used = set(records.values()) | {1}  # bundled Docker worker
        free = next((slot for slot in range(2, MAX_REMOTE + 1) if slot not in used), None)
        if free is None:
            raise ValueError('All 15 additional machine stage slots have been used. Create a new Conductor profile to connect more machines safely.')
        records[identity] = free
        write_json(config.HOME / 'surface-slots.json', records)
    node['lodge_surface_slot'] = records[identity]
    node['lodge_surface_port'] = port


async def relay(slot, reader, writer):
    task = asyncio.current_task()
    if len(CONNECTIONS) >= 128:
        writer.close()
        return
    CONNECTIONS[task] = slot
    upstream = None
    pumps = []
    try:
        node = next((n for n in config.nodes() if n.get('lodge_surface_slot') == slot), None)
        if not node or not node.get('lodge_surface_port'):
            return
        parsed = urlsplit(transport.origin(node))
        kwargs = {}
        if parsed.scheme == 'https':
            kwargs = {'ssl': ssl.create_default_context(cafile=transport.ca_file(node)), 'server_hostname': parsed.hostname}
        remote, upstream = await asyncio.wait_for(asyncio.open_connection(parsed.hostname, node['lodge_surface_port'], **kwargs), 12)
        async def pipe(source, destination):
            while chunk := await asyncio.wait_for(source.read(65536), 120):
                destination.write(chunk)
                await asyncio.wait_for(destination.drain(), 30)
        pumps = [asyncio.create_task(pipe(reader, upstream)), asyncio.create_task(pipe(remote, writer))]
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, TimeoutError, ValueError):
        pass
    finally:
        for pump in pumps: pump.cancel()
        if pumps: await asyncio.gather(*pumps, return_exceptions=True)
        writer.close()
        if upstream: upstream.close()
        CONNECTIONS.pop(task, None)


async def disconnect(slot):
    """Revoke existing streams as well as future registry lookups on disconnect."""
    tasks = [task for task, owner in CONNECTIONS.items() if owner == slot]
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def start():
    if SERVERS: return
    try:
        for slot in range(1, MAX_REMOTE + 1):
            async def accept(reader, writer, slot=slot):
                await relay(slot, reader, writer)
            # Docker's private network and the native loopback/onion entry share
            # the same authenticated Surface. No host port publishing is needed.
            host = '0.0.0.0' if config.node().get('lodge_container') else '127.0.0.1'
            SERVERS.append(await asyncio.start_server(accept, host, 4400 + slot))
    except BaseException:
        await stop()
        raise


async def stop():
    for server in SERVERS: server.close()
    await asyncio.gather(*(server.wait_closed() for server in SERVERS))
    SERVERS.clear()
    tasks = list(CONNECTIONS)
    for task in tasks: task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
