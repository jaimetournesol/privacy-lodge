"""Bound each subscriber's latency without reordering events per broadcast."""
import asyncio
import json


async def send_all(clients, event, timeout=2):
    data = json.dumps(event)
    async def send(ws):
        try:
            await asyncio.wait_for(ws.send_text(data), timeout)
        except Exception:
            clients.discard(ws)
            try:
                await asyncio.wait_for(ws.close(code=1013), .2)
            except Exception:
                pass
    await asyncio.gather(*(send(ws) for ws in list(clients)))
