"""Container entry: provision command/worker roles and supervise Surface + Agentnode."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from . import config
from .storage import write_json, private_dir


def provision():
    role = os.environ.get('LODGE_ROLE', 'control')
    if role not in ('control', 'worker'):
        raise SystemExit('LODGE_ROLE must be control or worker')
    n = config.node()
    n.update(lodge=True, lodge_container=True, name=role, control=role == 'control', backend='codex', codex_bin='/usr/local/bin/codex',
             model=os.environ.get('LODGE_CODEX_MODEL', 'auto'), host_tools=False,
             port=8787 if role == 'control' else 8444, tls_port=0,
             hub_port=4400, hub_tls_port=0, surface_state_dir=str(config.HOME / 'surface'), surface_dir='/opt/agentnode/vendor/surface')
    config.save_node(n)
    private_dir(config.HOME / 'codex')
    private_dir(config.HOME / 'projects')
    if not config.projects():
        pid = 'conductor' if role == 'control' else 'workspace'
        directory = config.HOME / 'projects' / pid
        private_dir(directory)
        config.save_projects([{'id': pid, 'name': 'Conductor' if role == 'control' else 'Worker',
            'dir': str(directory), 'backend': 'codex', 'model': n['model'], 'host_tools': False,
            'conductor': role == 'control', 'approval': 'approved',
            'instructions': ('Coordinate the Docker worker and present results on Surface. Keep your presentation visible while the human chats. '
                             'Use text replies; do not request voice tools. Work only in the allocated workspace. '
                             'Ask before external publishing or destructive operations.') }])
    if role == 'worker':
        write_json(Path('/peer/worker.json'), {'name': 'worker', 'url': 'http://agent-worker:8444', 'token': n['token'], 'lodge_surface_slot': 1, 'lodge_surface_port': 4400})
    else:
        handoff = Path('/handoff/agentnode')
        private_dir(handoff)
        token_file = handoff / 'browser-token'
        token_file.write_text(n['token'])
        token_file.chmod(0o600)
        write_json(handoff / 'runtime.json', {'backend': 'agentnode', 'provider': 'codex', 'version': 1})
        deadline = time.monotonic() + 60
        while not Path('/peer/worker.json').exists():
            if time.monotonic() > deadline:
                raise SystemExit('Worker did not publish its connection; retry after checking the worker container.')
            time.sleep(.25)
        peer = json.loads(Path('/peer/worker.json').read_text())
        if peer.get('url') != 'http://agent-worker:8444' or peer.get('name') != 'worker':
            raise SystemExit('Unexpected worker connection')
        config.save_nodes([peer] + [node for node in config.nodes() if node['name'] != 'worker'])
    return n


async def main():
    n = provision()
    env = dict(os.environ, CODEX_HOME=str(config.HOME / 'codex'), AGENTNODE_TOKEN=n['token'])
    for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN'):
        env.pop(key, None)
    surface_env = dict(env, SURFACE_DIR=n['surface_dir'], HUB_PORT='4400', HUB_TLS_PORT='0',
        SURFACE_AGENT_URL=f"http://127.0.0.1:{n['port']}", SURFACE_MODEL=n['model'],
        SURFACE_AUTH_TOKEN=n['token'], SURFACE_STATE_DIR=str(config.HOME / 'surface'), SURFACE_AGENT='external', HUB_HOST='0.0.0.0', NODE_ENV='production')
    children = [await asyncio.create_subprocess_exec(sys.executable, '-m', 'agentnode', 'serve', env=env),
                await asyncio.create_subprocess_exec('/opt/agentnode/vendor/surface/node_modules/.bin/tsx', 'server/index.ts',
                    cwd=n['surface_dir'], env=surface_env)]
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    tasks = [asyncio.create_task(c.wait()) for c in children] + [asyncio.create_task(stopping.wait())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for child in children:
        if child.returncode is None: child.terminate()
    for child in children:
        try: await asyncio.wait_for(child.wait(), 10)
        except TimeoutError: child.kill(); await child.wait()
    for task in tasks: task.cancel()
    if not stopping.is_set(): raise SystemExit('An Agentnode service stopped; restarting the container.')


if __name__ == '__main__':
    asyncio.run(main())
