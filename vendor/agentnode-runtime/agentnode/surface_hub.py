"""Launch Surface using an environment dictionary, never shell-evaluated settings."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
from . import config


def main():
    n = config.node()
    if not n.get('surface_dir'):
        raise SystemExit('No surface_dir configured; run python -m agentnode setup --install-deps')
    surface = Path(n['surface_dir']).expanduser().resolve()
    subprocess.run([sys.executable, str(config.REPO / 'scripts/install_surface_presentation.py'), str(surface), '--build'], check=True)
    env = dict(os.environ, SURFACE_DIR=str(surface), HUB_PORT=str(n['hub_port']), HUB_TLS_PORT=str(n['hub_tls_port']),
               SURFACE_AGENT_URL=f"http://127.0.0.1:{n['port']}", SURFACE_MODEL=str(n['model']),
               SURFACE_AUTH_TOKEN=n['token'], SURFACE_AGENT='external', HUB_HOST='0.0.0.0', NODE_ENV='production')
    if n.get('lodge'):
        env.update(LODGE_MODE='1', CODEX_HOME=str(config.HOME / 'codex'), AGENTNODE_TOKEN=n['token'])
    if (config.TLS / 'cert.pem').exists() and (config.TLS / 'key.pem').exists():
        env.update(HUB_TLS_CERT=str(config.TLS / 'cert.pem'), HUB_TLS_KEY=str(config.TLS / 'key.pem'))
    os.chdir(surface)
    executable = surface / 'node_modules/.bin/tsx'
    if not executable.exists():
        raise SystemExit('Surface dependencies missing; run setup --install-deps')
    os.execve(str(executable), [str(executable), 'server/index.ts'], env)


if __name__ == '__main__':
    main()
