"""agentnode configuration: ~/.agentnode/{node.json, projects.json, nodes.json, secrets.json, tls/, data/}."""
import json
import os
import platform
import secrets
import socket
import re
from pathlib import Path
from .storage import private_dir, read_json, write_json

HOME = Path(os.environ.get("AGENTNODE_HOME", Path.home() / ".agentnode"))
DATA = HOME / "data"
TLS = HOME / "tls"
LOGS = HOME / "logs"
for d in (HOME, DATA, TLS, LOGS):
    private_dir(d)

REPO = Path(__file__).resolve().parent.parent  # the agentnode checkout (mcp/, static/)


def _read(path: Path, default):
    return read_json(path, default)


def _write(path: Path, value):
    write_json(path, value)


def is_mac() -> bool:
    return platform.system() == "Darwin"


def default_claude_bin() -> str:
    for p in (Path.home() / ".local/bin/claude", Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude")):
        if p.exists():
            return str(p)
    return "claude"


DEFAULT_NODE = {
    "name": socket.gethostname().split(".")[0].lower(),
    "port": 8444,
    "tls_port": 8445,
    "token": None,
    "model": "opus",
    "claude_bin": None,
    "claude_oauth_token_file": None,  # optional private setup-token file; never store its value here
    "hub_port": 4400,          # Surface hub (external-agent mode) on this node
    "hub_tls_port": 4443,
    "surface_dir": None,       # checkout of the Surface hub (universal); None = no surfaces
    "wake_word": "conductor",  # what the voice stage listens for (control node)
    "control": False,          # this node hosts the control UI + conductor
    "host_tools": True,        # expose this machine's screen + mouse/keyboard to agents (off on the control machine)
    "host_backend": "auto",    # auto | x11 | mac | wayland (GNOME portals + PipeWire)
    "fps": 15,
    "frame_width": 1280,
    "jpeg_quality": 65,
}


def node() -> dict:
    n = {**DEFAULT_NODE, **_read(HOME / "node.json", {})}
    changed = False
    if not n.get("token"):
        n["token"] = secrets.token_urlsafe(24)
        changed = True
    if not n.get("claude_bin"):
        n["claude_bin"] = default_claude_bin()
        changed = True
    if changed:
        _write(HOME / "node.json", n)
    # Release code and shared Surface data have separate lifetimes.
    if os.environ.get("AGENTNODE_SURFACE_DIR"):
        n["surface_dir"] = os.environ["AGENTNODE_SURFACE_DIR"]
    return n


def save_node(n: dict):
    _write(HOME / "node.json", n)


def projects() -> list[dict]:
    return _read(HOME / "projects.json", [])


def save_projects(items: list[dict]):
    _write(HOME / "projects.json", items)


def nodes() -> list[dict]:
    """Other machines (control node only): [{name, url, token, hub_url}]."""
    return _read(HOME / "nodes.json", [])


def save_nodes(items: list[dict]):
    _write(HOME / "nodes.json", items)


def secrets_() -> dict:
    s = _read(HOME / "secrets.json", {})
    for k in ("ELEVENLABS_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(k):
            s[k] = os.environ[k]
    return s


def project_data(pid: str) -> Path:
    validate_id(pid)
    d = (DATA / pid).resolve()
    if not d.is_relative_to(DATA.resolve()):
        raise ValueError('project data must stay inside the data directory')
    private_dir(d)
    private_dir(d / 'logs')
    return d


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise ValueError('id must be 1–64 lowercase letters, numbers, underscores or hyphens; start with a letter or number')
    return value


def slug(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:64].rstrip('-') or "project"


def host_backend(n: dict | None = None) -> str:
    n = n or node()
    b = n.get("host_backend") or "auto"
    if b != "auto":
        return b
    if is_mac():
        return "mac"
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or (os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY")):
        return "wayland"
    return "x11"
