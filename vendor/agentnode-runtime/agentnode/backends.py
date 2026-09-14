"""Backend identity and model resolution; never reuse another CLI's model default."""
import shutil
from pathlib import Path

BACKENDS = ('claude', 'codex', 'opencode')


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
    if not model and backend == 'claude':
        model = 'opus'
    if not model:
        raise ValueError(f'Set an explicit model for {backend}' + (' (provider/model)' if backend == 'opencode' else ''))
    if backend == 'opencode' and '/' not in model:
        raise ValueError('OpenCode model must be provider/model')
    return model


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
