#!/usr/bin/env python3
"""`host` MCP: screenshot / mouse / keyboard on THIS machine (macOS or X11 Linux).

Zero-dependency JSON-RPC over stdio; the actual work uses mss + pyautogui from the
agentnode venv (this script is run with that venv's python). Screenshots are scaled to
FRAME_W wide; coordinates the agent gives are in that scaled space.
Also importable: `perform(action, **kw)` is used by the node's HTTP API.
"""
import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentnode.screenshots import dimensions, encode

FRAME_W = int(os.environ.get("AGENTNODE_FRAME_W", "1280"))
if sys.platform != "darwin":
    os.environ.setdefault("DISPLAY", ":0")
    if "XAUTHORITY" not in os.environ:
        xa = f"/run/user/{os.getuid()}/gdm/Xauthority"
        if os.path.exists(xa):
            os.environ["XAUTHORITY"] = xa

_pg = None


def pg():
    global _pg
    if _pg is None:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
        _pg = pyautogui
    return _pg


def screen_size():
    import mss
    with mss.mss() as sct:
        m = sct.monitors[1]
        return m["width"], m["height"]


def scale():
    w, h = screen_size()
    fw, fh = dimensions(w, h)
    return w / fw, h / fh, fw, fh


def screenshot() -> dict:
    import mss
    from PIL import Image
    with mss.mss() as sct:
        m = sct.monitors[1]
        shot = sct.grab(m)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return encode(img)


def to_real(coord):
    sx, sy, _, _ = scale()
    return int(coord[0] * sx), int(coord[1] * sy)


def perform_via_node(action, coordinate, text, amount) -> dict:
    """Wayland (and any backend the node owns): the node performs the action; we just relay."""
    import urllib.request
    url = os.environ.get("AGENTNODE_URL", "http://127.0.0.1:8444") + "/api/host/computer"
    body = json.dumps({"action": action, "coordinate": coordinate, "text": text, "amount": amount}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Agentnode-Token": os.environ.get("AGENTNODE_TOKEN", "")}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read().decode())
    if res.get("image"):
        res["_image"] = base64.b64decode(res.pop("image"))
    return res


def perform(action: str, coordinate=None, text: str | None = None, amount: int | None = None, *, _via_node=True) -> dict:
    if _via_node and (os.environ.get("AGENTNODE_URL") or os.environ.get("AGENTNODE_HOST_BACKEND") == "wayland"):
        return perform_via_node(action, coordinate, text, amount)
    p = pg()
    if action == "screenshot":
        s = screenshot()
        return {"status": "success", "action": "screenshot", "width": s["width"], "height": s["height"], "_image": s["jpeg"]}
    if coordinate and action in ("mouse_move", "left_click", "right_click", "middle_click", "double_click", "scroll"):
        x, y = to_real(coordinate)
        p.moveTo(x, y, duration=0.1)
    if action == "mouse_move":
        pass
    elif action == "left_click":
        p.click()
    elif action == "right_click":
        p.click(button="right")
    elif action == "middle_click":
        p.click(button="middle")
    elif action == "double_click":
        p.doubleClick()
    elif action == "type":
        if sys.platform == "darwin":
            # pyautogui.typewrite drops non-ASCII on macOS; paste via clipboard instead
            subprocess.run("pbcopy", input=(text or "").encode(), check=False)
            p.hotkey("command", "v")
        elif subprocess.run(["which", "xdotool"], capture_output=True).returncode == 0:
            subprocess.run(["xdotool", "type", "--delay", "12", text or ""], check=False)
        else:
            p.typewrite(text or "", interval=0.02)
    elif action == "key":
        keys = [k.strip() for k in (text or "").replace("+", " ").split() if k.strip()]
        alias = {"cmd": "command", "super": "win", "return": "enter", "esc": "escape", "ctl": "ctrl"}
        keys = [alias.get(k.lower(), k.lower()) for k in keys]
        if sys.platform != "darwin" and subprocess.run(["which", "xdotool"], capture_output=True).returncode == 0:
            subprocess.run(["xdotool", "key", "+".join(keys)], check=False)
        elif len(keys) > 1:
            p.hotkey(*keys)
        elif keys:
            p.press(keys[0])
    elif action == "scroll":
        p.scroll(int(amount or 0))
    elif action == "cursor_position":
        x, y = p.position()
        sx, sy, _, _ = scale()
        return {"status": "success", "x": int(x / sx), "y": int(y / sy)}
    else:
        return {"status": "error", "message": f"unknown action: {action}"}
    time.sleep(0.15)
    return {"status": "success", "action": action}


TOOLS = [{
    "name": "computer",
    "description": ("Control THIS computer's screen: take a screenshot (at most 1280 px on either edge and 256 KiB JPEG; give coordinates in its returned width/height), "
                    "move/click the mouse, type text, press keys (e.g. 'ctrl+l', 'enter', 'cmd+space'), scroll. "
                    "Take a screenshot before acting and after any action that changes the screen."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["screenshot", "mouse_move", "left_click", "right_click", "middle_click",
                                                  "double_click", "type", "key", "cursor_position", "scroll"]},
            "coordinate": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
            "text": {"type": "string", "description": "Text to type, or key combo for 'key'"},
            "amount": {"type": "integer", "description": "Scroll amount (positive = up, negative = down)"},
        },
        "required": ["action"],
    },
}]


def _write(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue
        try:
            if method == "initialize":
                _write({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                                              "capabilities": {"tools": {}}, "serverInfo": {"name": "host", "version": "0.1.0"}}})
            elif method == "tools/list":
                _write({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                a = params.get("arguments") or {}
                res = perform(a.get("action", ""), a.get("coordinate"), a.get("text"), a.get("amount"))
                img = res.pop("_image", None)
                content = [{"type": "text", "text": json.dumps(res)}]
                if img:
                    content.append({"type": "image", "data": base64.b64encode(img).decode(), "mimeType": "image/jpeg"})
                _write({"jsonrpc": "2.0", "id": mid, "result": {"content": content, "isError": res.get("status") == "error"}})
            elif method == "ping":
                _write({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                _write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
        except Exception as e:
            _write({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}})


if __name__ == "__main__":
    main()
