"""Private, atomic local state with explicit corruption errors and writer locks."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile

class StateError(ValueError):
    pass

def private_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path

def private_file(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    path.chmod(0o600)
    return path

def read_json(path: Path, default):
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise StateError(f'Cannot read {path.name}; restore a valid state file before restarting') from exc
    if not isinstance(value, type(default)):
        raise StateError(f'Invalid {path.name}: expected {type(default).__name__}')
    path.chmod(0o600)
    return value

@contextmanager
def locked(path: Path):
    private_dir(path.parent)
    fd = os.open(path.with_name(path.name+'.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def atomic_text(path: Path, text: str):
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def write_json(path: Path, value):
    with locked(path):
        atomic_text(path, json.dumps(value, indent=2)+'\n')
