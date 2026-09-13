import asyncio
import json
import time
import urllib.request

IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def http_json(method: str, url: str, body=None, headers=None, timeout: float = 6.0, max_bytes: int = 16*1024*1024):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    import os
    if os.environ.get('LODGE_MODE') == '1':
        from urllib.parse import urlsplit
        from . import config
        target = urlsplit(url)
        node = config.node()
        if target.hostname == '127.0.0.1' and target.port in (node['port'], node['hub_port']):
            h.setdefault('X-Agentnode-Token', node['token'])
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data=r.read(max_bytes+1)
        if len(data)>max_bytes:raise ValueError('Response exceeds the permitted size')
        return json.loads(data.decode() or "null")


async def hub_call(hub_url: str, method: str, path: str, body=None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: http_json(method, hub_url + path, body))
