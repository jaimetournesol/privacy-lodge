"""Idempotent provisioning and read-only diagnostics for a node."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from . import config
from .backends import BACKENDS, binary_for, model_for


def provision(args):
    n = config.node()
    if getattr(args, 'lodge', False):
        if args.backend and args.backend != 'codex':
            raise ValueError('Lodge uses Codex only')
        n['lodge'] = True
        args.backend = 'codex'
    role = 'control' if args.control else args.role
    if role:
        n['control'] = role == 'control'
        n['host_tools'] = role != 'control'
    if n.get('lodge'):
        n['host_tools'] = False
    if args.name:
        n['name'] = args.name
    backend = args.backend or n.get('backend', 'claude')
    model = model_for(backend, n, {'backend': backend, 'model': args.model})
    n.update(backend=backend, model=model)
    if args.yolo and backend in ('codex', 'opencode'):
        n[backend + '_yolo'] = True
    binary = args.binary or binary_for(backend, n)
    n[backend + '_bin'] = shutil.which(binary) or str(Path(binary).expanduser().resolve())
    if not Path(n[backend + '_bin']).is_file():
        raise ValueError(f'{backend} CLI missing. Install/authenticate it, then pass --binary /absolute/path')
    if args.surface_dir:
        surface = Path(args.surface_dir).expanduser().resolve()
    elif n.get('surface_dir'):
        surface = Path(n['surface_dir']).expanduser().resolve()
    else:
        surface = (config.HOME / 'surface').resolve()
        if not surface.exists():
            shutil.copytree(config.REPO / 'vendor/surface', surface)
        elif not (surface / 'package.json').exists():
            raise ValueError(f'{surface} exists without a Surface package; choose --surface-dir')
    for path in ('package.json', 'package-lock.json', 'server/index.ts', 'server/mcp-stdio.mjs'):
        if not (surface / path).is_file():
            raise ValueError(f'Surface is incomplete: missing {path}')
    n['surface_dir'] = str(surface)
    config.save_node(n)
    if args.install_deps:
        if not shutil.which('npm'):
            raise ValueError('Node.js/npm required for Surface (Node 20.19+ or 22.12+)')
        subprocess.run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=surface, check=True)
        if n.get('control') and not n.get('lodge'):
            subprocess.run([sys.executable, '-m', 'pip', 'install', 'faster-whisper'], check=True)
        subprocess.run([sys.executable, str(config.REPO / 'scripts/install_surface_presentation.py'), str(surface), '--build'], check=True)
    from .cli import _ensure_tls
    _ensure_tls()
    # Preserve every existing project, named session, workspace and standing brief.
    items = config.projects()
    if n.get('control') and not any(p.get('conductor') for p in items):
        if any(p['id'] == 'conductor' for p in items):
            raise ValueError('A non-conductor project already owns id conductor; configure its role explicitly')
        folder = config.HOME / 'projects/conductor'
        folder.mkdir(parents=True, exist_ok=True)
        items.append({'id': 'conductor', 'name': 'Conductor', 'dir': str(folder), 'backend': backend, 'model': model,
                      'host_tools': False, 'conductor': True,
                      'instructions': 'Coordinate the connected fleet and help the human present and interact with results.'})
        config.save_projects(items)
    if args.services:
        install_services(n)
    print('Configured', n['name'], 'as', 'control' if n.get('control') else 'worker', 'using', backend)
    print('State:', config.HOME)
    print('Run: ./run-surface-hub.sh and ./run.sh (or use --services). Then: python -m agentnode doctor --live')
    print('CLI authentication is unchanged. Retrieve the connection token with: python -m agentnode token')


def install_services(node):
    """Render actual absolute paths, including custom checkout and data directories."""
    env = {'AGENTNODE_HOME': str(config.HOME), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
    for label, script in [('com.agentnode.surface', 'run-surface-hub.sh'), ('com.agentnode', 'run.sh')]:
        if config.is_mac():
            target = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': ['/bin/bash', str(config.REPO / script)],
                'WorkingDirectory': str(config.REPO), 'EnvironmentVariables': env, 'RunAtLoad': True, 'KeepAlive': True,
                'StandardOutPath': str(config.LOGS / (label + '.out.log')),
                'StandardErrorPath': str(config.LOGS / (label + '.err.log'))}))
            domain = f'gui/{os.getuid()}'
            subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
            subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=True)
        else:
            name = 'surface-hub' if label.endswith('surface') else 'agentnode'
            target = Path.home() / '.config/systemd/user' / (name + '.service')
            target.parent.mkdir(parents=True, exist_ok=True)
            def esc(value):
                return str(value).replace('%', '%%')
            def quote(value):
                return json.dumps(esc(value))
            # WorkingDirectory= takes a bare path: systemd unquotes command lines, not path settings.
            target.write_text('[Unit]\nDescription=AgentNode ' + name + '\nAfter=graphical-session.target network-online.target\n'
                '[Service]\nType=simple\nWorkingDirectory=' + esc(config.REPO) + '\nExecStart=/bin/bash ' + quote(config.REPO / script) +
                '\nEnvironment=' + quote('AGENTNODE_HOME=' + str(config.HOME)) + '\nEnvironment=' + quote('PATH=' + env['PATH']) +
                '\nRestart=on-failure\nRestartSec=5\n[Install]\nWantedBy=default.target\n')
    if not config.is_mac():
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'surface-hub', 'agentnode'], check=True)


def doctor(args):
    from urllib.request import urlopen
    # Deliberately never print credentials, provider configs or generated MCP env.
    n = config.node()
    checks = []
    def check(name, ok, detail):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
    backend = n.get('backend', 'claude')
    for project in config.projects() or [{}]:
        chosen = project.get('backend') or backend
        binary = binary_for(chosen, n)
        check((project.get('id') or 'node') + ' CLI', shutil.which(binary), chosen + ': ' + binary)
        try:
            model = model_for(chosen, n, project)
            check('model', True, model)
        except ValueError as exc:
            check('model', False, str(exc))
    for dep in ('fastapi', 'uvicorn', 'websockets', 'requests', 'PIL') + (('faster_whisper',) if n.get('control') else ()):
        check('Python dependency', importlib.util.find_spec(dep), dep)
    for name in ('cert.pem', 'key.pem'):
        check('TLS', (config.TLS / name).is_file(), name)
    surface = Path(n.get('surface_dir') or '/nonexistent-agentnode-surface')
    for name in ('server/mcp-stdio.mjs', 'node_modules/.bin/tsx', 'web/dist/index.html'):
        check('Surface', (surface / name).is_file(), name)
    check('Node.js', shutil.which('node'), 'node must be available to service launchers')
    if args.live:
        for name, url in [('AgentNode', f"http://127.0.0.1:{n['port']}/api/projects"),
                          ('Surface', f"http://127.0.0.1:{n['hub_port']}/api/workspaces")]:
            try:
                with urlopen(url, timeout=5) as response:
                    check(name, response.status == 200, 'HTTP ' + str(response.status))
            except Exception as exc:
                check(name, False, type(exc).__name__)
    print(json.dumps(checks, indent=2) if args.json else '\n'.join(('OK   ' if c['ok'] else 'FAIL ') + c['check'] + ': ' + c['detail'] for c in checks))
    return all(c['ok'] for c in checks)


def add_commands(sub):
    p = sub.add_parser('setup', help='Provision a node without replacing existing projects or sessions')
    p.add_argument('--role', choices=('control', 'worker'))
    p.add_argument('--control', action='store_true', help='Alias for --role control')
    p.add_argument('--name'); p.add_argument('--backend', choices=BACKENDS); p.add_argument('--model'); p.add_argument('--binary')
    p.add_argument('--lodge', action='store_true', help='Codex-only Privacy Lodge integration with private runtime sign-in')
    p.add_argument('--yolo', action='store_true', help='Enable unattended unrestricted execution for the selected CLI')
    p.add_argument('--surface-dir'); p.add_argument('--install-deps', action='store_true')
    p.add_argument('--services', action='store_true', help='Install and start user services')
    p.set_defaults(f=provision)
    p = sub.add_parser('doctor', help='Check setup without starting agents or sending provider requests')
    p.add_argument('--live', action='store_true'); p.add_argument('--json', action='store_true')
    p.set_defaults(f=lambda args: sys.exit(0 if doctor(args) else 1))
