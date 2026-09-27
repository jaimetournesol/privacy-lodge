"""Backend identity, model and reasoning-effort resolution; never reuse another CLI's defaults."""
import json
import os
import shutil
import subprocess
from pathlib import Path

BACKENDS = ('claude', 'codex', 'opencode')
# Effort levels each CLI accepts on its command line. Claude Code documents --effort;
# Codex's bundled model catalog narrows its list per model when it can be read.
EFFORT_LEVELS = {'claude': ('low', 'medium', 'high', 'xhigh', 'max'),
                 'codex': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
                 'opencode': ()}
ALL_EFFORTS = tuple(dict.fromkeys(level for levels in EFFORT_LEVELS.values() for level in levels))
_catalog_cache = {}


def validate_backend(value):
    if value not in BACKENDS:
        raise ValueError('backend must be claude, codex or opencode')
    return value


def model_for(backend, node, project=None, agent=None):
    validate_backend(backend)
    for level in (agent or {}, project or {}):
        if level.get('model') and (level.get('backend') or node.get('backend', 'claude')) == backend:
            model = level['model']
            if backend == 'opencode' and '/' not in model:
                raise ValueError('OpenCode model must be provider/model')
            return model
    model = node.get(backend + '_model') or (node.get('model') if node.get('backend', 'claude') == backend else None)
    if not model:
        model = {'claude': 'claude-opus-5-5', 'codex': 'gpt-6-astra'}.get(backend)
    if not model:
        raise ValueError(f'Set an explicit model for {backend}' + (' (provider/model)' if backend == 'opencode' else ''))
    if backend == 'opencode' and '/' not in model:
        raise ValueError('OpenCode model must be provider/model')
    return model


def effort_for(backend, node, project=None, agent=None):
    """Resolve saved overrides, then the backend default; OpenCode has no effort flag."""
    validate_backend(backend)
    for level in (agent or {}, project or {}):
        if level.get('reasoning_effort') and (level.get('backend') or node.get('backend', 'claude')) == backend:
            return level['reasoning_effort']
    return node.get(backend + '_reasoning_effort') or (
        node.get('reasoning_effort') if node.get('backend', 'claude') == backend else None) or (
        'high' if backend in ('claude', 'codex') else None)


def codex_supported_efforts(binary, model):
    """Effort levels the installed Codex catalog lists for a model; None when unknown."""
    try:
        stamp = os.stat(binary).st_mtime_ns
    except OSError:
        return None
    key = (str(binary), stamp)
    if key not in _catalog_cache:
        try:
            run = subprocess.run([str(binary), 'debug', 'models', '--bundled'], capture_output=True, timeout=8, check=True)
            catalog = json.loads(run.stdout.decode())['models']
            _catalog_cache[key] = {m['slug']: tuple(l['effort'] if isinstance(l, dict) else str(l) for l in m.get('supported_reasoning_levels') or ())
                                   for m in catalog if isinstance(m, dict) and m.get('slug')}
        except Exception:
            _catalog_cache[key] = {}
    levels = _catalog_cache[key].get(model)
    return levels if levels else None


def validate_effort(backend, value, model=None, node=None):
    """Reject efforts the CLI would ignore or misapply; the CLIs do not validate reliably themselves."""
    validate_backend(backend)
    if value is None:
        return None
    allowed = EFFORT_LEVELS[backend]
    if not allowed:
        raise ValueError(f'{backend} does not support reasoning_effort')
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f'{backend} reasoning_effort must be one of ' + ', '.join(allowed))
    if backend == 'codex' and model and node is not None:
        supported = codex_supported_efforts(binary_for('codex', node), model)
        if supported is not None and value not in supported:
            raise ValueError(f'Installed Codex lists {model} reasoning efforts ' + ', '.join(supported))
    return value


def binary_for(backend, node):
    validate_backend(backend)
    if node.get(backend + '_bin'):
        return node[backend + '_bin']
    return shutil.which(backend) or str(Path.home() / '.local/bin' / backend)


def process_env(backend, node, inherited):
    """Load an optional host-local subscription token only for Claude processes."""
    import os
    import stat
    env = dict(inherited)
    env.pop('CLAUDECODE', None)
    if backend != 'claude':
        env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
        return env
    location = node.get('claude_oauth_token_file')
    if not location:
        return env
    try:
        with Path(location).expanduser().open() as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('Claude token file must be private (0600)')
            token = f.read(4097).strip()
        if not token or len(token) > 4096 or any(c.isspace() for c in token):
            raise ValueError('Claude token file is empty or invalid')
    except OSError:
        raise ValueError('Cannot read configured Claude token file') from None
    # Explicit subscription-token mode must not accidentally select API billing
    # from inherited provider credentials. The daemon's environment is untouched.
    env.pop('ANTHROPIC_API_KEY', None)
    env.pop('ANTHROPIC_AUTH_TOKEN', None)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = token
    return env
