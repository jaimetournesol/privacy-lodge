"""Privacy Lodge's Codex-only product contract. No host credentials are imported."""
import asyncio
import os
import re
import time
from fastapi import APIRouter, HTTPException
from . import config

router = APIRouter(prefix='/api/lodge')
_lock = asyncio.Lock()
_login = None
_reader = None
_state = {'state': 'signed_out'}


def environment():
    env = dict(os.environ)
    for name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN'):
        env.pop(name, None)
    env['CODEX_HOME'] = str(config.HOME / 'codex')
    return env


async def authenticated():
    process = await asyncio.create_subprocess_exec(config.node()['codex_bin'], 'login', 'status',
        env=environment(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        return await asyncio.wait_for(process.wait(), 10) == 0
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return False


@router.get('/auth')
async def auth_status():
    if _login is None or _login.returncode is not None:
        return {'state': 'ready' if await authenticated() else (_state.get('state') if _state.get('state') in ('failed', 'expired') else 'signed_out'),
                'provider': 'codex', 'node': config.node()['name']}
    return {**_state, 'provider': 'codex', 'node': config.node()['name']}


async def read_login(process):
    global _state
    # Whitelist only the official URL and short-lived device code. Never expose
    # raw CLI output: future versions may include credentials or diagnostics.
    text = ''
    try:
        async with asyncio.timeout(15 * 60):
            while chunk := await process.stdout.read(1024):
                text = (text + chunk.decode('utf-8', 'replace'))[-16384:]
                text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
                url = re.search(r'https://auth\.openai\.com/codex/device(?:\b|/)', text)
                code = re.search(r'\b[A-Z0-9]{4}-[A-Z0-9]{5}\b|\b[A-Z0-9]{4}-[A-Z0-9]{4}\b', text)
                if url and code:
                    _state = {'state': 'awaiting_authorization', 'url': 'https://auth.openai.com/codex/device',
                              'code': code.group(), 'expires_at': time.time() + 15 * 60}
            result = await process.wait()
            _state = {'state': 'ready' if result == 0 and await authenticated() else 'failed'}
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        _state = {'state': 'expired'}


@router.post('/auth')
async def start_login():
    global _login, _reader, _state
    async with _lock:
        if _login is not None and _login.returncode is None:
            return {**_state, 'provider': 'codex'}
        if await authenticated():
            return {'state': 'ready', 'provider': 'codex'}
        _state = {'state': 'starting'}
        _login = await asyncio.create_subprocess_exec(config.node()['codex_bin'], 'login', '--device-auth',
            env=environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        _reader = asyncio.create_task(read_login(_login))
        return dict(_state)


@router.delete('/auth')
async def cancel_login():
    global _state
    async with _lock:
        if _reader and not _reader.done():
            _reader.cancel()
            await asyncio.gather(_reader, return_exceptions=True)
        _state = {'state': 'signed_out'}
        return {'ok': True}
