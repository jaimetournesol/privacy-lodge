#!/usr/bin/env python3
"""`conductor` MCP: the control node's orchestration tools.

Everything goes through the local agentnode (control node) API, which proxies to the
other machines with their tokens. Zero dependencies; JSON-RPC over stdio.
"""
import base64
import json
import os
import sys
import time
import uuid
from urllib.parse import quote
import urllib.request
import urllib.error
from pathlib import Path

NODE = os.environ.get("AGENTNODE_URL", "http://127.0.0.1:8444")
ME = os.environ.get("AGENTNODE_PROJECT", "conductor")
SHOTS = Path.home() / ".agentnode" / "data" / ME / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)


def call(method, path, body=None, timeout=30, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(NODE + path, data=data, method=method, headers={"Content-Type": "application/json", "X-Agentnode-Token": os.environ.get("AGENTNODE_TOKEN", "")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            return b if raw else json.loads(b.decode() or "null")
    except urllib.error.HTTPError as exc:
        # Preserve the API's actionable selection/conflict receipt for the agent.
        raise RuntimeError(exc.read(65536).decode(errors='replace')) from exc



TOOLS = [
    {"name": "list_nodes", "description": "The machines in the fleet (name, reachable?, platform) and the projects (agents) on each. Start here.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "status", "description": "Status of one project's agent: idle/working/stopped, current tool, cost, last result.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}}, "required": ["node", "project"]}},
    {"name": "focus", "description": "Decide what the human sees on the stage: a machine's live screen, a project's Surface panels, or both. Select a destination with the human when several screens are online. Use explicit presentation calls before and during delegations, and whenever something worth watching happens (e.g. view='screen' while an agent operates a desktop, view='surface' when panels carry the result). The human's chat stays with you.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string", "description": "optional; omit for the machine's screen only"}, "view": {"type": "string", "enum": ["screen", "surface", "both"], "description": "what to show (default: screen if the machine has one)"}, "add": {"type": "boolean", "description": "true = add these tiles next to what is already on stage instead of replacing it"}, "pin": {"type": "boolean", "description": "true = pin the tile(s) so later focus calls add alongside instead of replacing them"}}, "required": ["node"]}},
    {"name": "stage", "description": "Compose the whole stage as a grid of up to 6 views (both uses two views), any mix of machine screens and project Surfaces — e.g. TVPC's screen next to zeus's screen next to the conductor's Fleet Overview. Replaces everything on stage. Use when the human is working on several things at once, or to show a delegate's screen beside its results.",
     "inputSchema": {"type": "object", "properties": {"tiles": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}, "view": {"type": "string", "enum": ["screen", "surface", "both"]}}, "required": ["node"]}}}, "required": ["tiles"]}},
    {"name": "pin", "description": "Pin (or unpin) what is on stage so later focus adds next to it instead of replacing it. Omit node to pin everything currently shown.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}, "pinned": {"type": "boolean", "description": "default true"}}}},
    {"name": "send", "description": "Delegate a task to another project's agent (its own persistent CLI session, memory and machine tools). NON-BLOCKING: returns at once and a watcher is armed — when that agent's turn ends you receive a new message `[delegate-result] node/project finished …` in this conversation. For a new voice request FIRST acknowledge with voice.say before any execution. Then send, tell the human it is underway in the current response channel (text for chat), END YOUR TURN, and keep serving them; relay [delegate-progress] after four quiet minutes and [delegate-result] on completion, in the original request channel. Presenter screens stay silent. Use `result` only for quick lookups (<30 s). Starts the agent if it is stopped.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}, "message": {"type": "string"}, "request_id": {"type":"string", "description":"Reuse this ID to safely retry an uncertain acceptance request"}, "wait_for_result": {"type": "boolean", "description": "true = old blocking behaviour, no watcher (use `result` afterwards)"}}, "required": ["node", "project", "message"]}},
    {"name": "result", "description": "Block until a project's current turn finishes (up to `wait` seconds, default 60) and return its final reply. For quick lookups only; long jobs use the non-blocking `send` + the automatic [delegate-result] message.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}, "wait": {"type": "integer"}, "turn_id": {"type":"string", "description":"Turn ID returned by send; use it to collect exactly that command"}}, "required": ["node", "project"]}},
    {"name": "progress", "description": "Check on a delegate that is still working: its status, current tool, and how long its turn has been running. Use when the human asks how something is going.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}}, "required": ["node", "project"]}},
    {"name": "screenshot", "description": "Look at a machine's screen right now. Saves a JPEG and returns its path — Read that path to see it.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}}, "required": ["node"]}},
    {"name": "computer", "description": "Act on a machine's screen directly (mouse/keyboard), same actions as the host computer tool; coordinates in the returned screenshot dimensions (at most 1280px per edge). Prefer delegating to that machine's project agent for multi-step work.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "action": {"type": "string", "enum": ["screenshot", "mouse_move", "left_click", "right_click", "middle_click", "double_click", "type", "key", "scroll", "cursor_position"]}, "coordinate": {"type": "array", "items": {"type": "integer"}}, "text": {"type": "string"}, "amount": {"type": "integer"}}, "required": ["node", "action"]}},
    {"name": "start_project", "description": "Create (if new) and start a project agent on any machine: a folder there gets its own Claude Code, Codex or OpenCode session, a Surface workspace, and the fleet tools. Folders are created if missing. Returns the project id to use in send/focus. On macOS nodes avoid ~/Desktop and ~/Documents (background services cannot read them).",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "dir": {"type": "string", "description": "absolute folder path on that machine (~ allowed)"}, "name": {"type": "string"}, "model": {"type": "string", "description": "Model supported by the selected backend; OpenCode requires provider/model. Inherits matching node defaults if omitted."}, "instructions": {"type": "string", "description": "optional standing project brief injected identically for every backend; existing project instruction files are preserved"}}, "required": ["node", "dir"]}},
    {"name": "stop_project", "description": "Stop a project agent (its session is kept and resumes on next start).",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}}, "required": ["node", "project"]}},
    {"name": "remove_project", "description": "Remove a project from a machine's registry (stops its agent; the folder and files stay).",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}}, "required": ["node", "project"]}},
    {"name": "agents", "description": "The named agents (sessions) of a project — each with its own history and panels — plus earlier Claude Code sessions found in that folder that could be resumed.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}}, "required": ["node", "project"]}},
    {"name": "new_agent", "description": "Start a NEW named agent (fresh session) in a project, or resume an earlier Claude Code session from that folder as a named agent. It becomes the project's active agent.",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}, "name": {"type": "string"}, "session_id": {"type": "string", "description": "optional: an earlier session id (from `agents`) to resume"}}, "required": ["node", "project", "name"]}},
    {"name": "switch_agent", "description": "Make another named agent of a project the active one (its session, history and panels).",
     "inputSchema": {"type": "object", "properties": {"node": {"type": "string"}, "project": {"type": "string"}, "agent": {"type": "string"}}, "required": ["node", "project", "agent"]}},
]


TOOLS.append({'name': 'list_displays', 'description': 'Read the unified Stage session, global revision, connected outputs and online named presenter screens with their assignments and applied revisions. The phone is a controller and is never listed. Use returned IDs with focus/stage/pin display. Offline screens cannot be targeted.', 'inputSchema': {'type': 'object', 'properties': {}}})
for tool in TOOLS:
    if tool['name'] in ('focus', 'stage', 'pin'):
        tool['inputSchema']['properties']['base_revision'] = {'type':'integer','minimum':0,'description':'Current global Stage revision from list_displays. Read again after conflicts; do not restore content removed by a reset.'}
        tool['inputSchema'].setdefault('required',[]).append('base_revision')
        tool['inputSchema']['properties']['display'] = {'type': 'string', 'description': 'Optional active presenter display ID from list_displays. Omit to auto-select the sole online output or the default composition when none are online. An output assignment in the single persistent Stage. Required when multiple screens are online; ask the human unless they explicitly chose a destination for this task.'}

TOOLS.append({'name':'reset_stage','description':'Reset the entire conductor Stage across ALL screens to Fleet, clearing assignments and pins. Does not delete Surface content or stop tasks. Only use when the human requests a reset.', 'inputSchema':{'type':'object','properties':{'base_revision':{'type':'integer','minimum':0}},'required':['base_revision']}})

# Same creation options for all CLI backends; schemas are the discoverable contract.
for tool in TOOLS:
    if tool['name'] in ('start_project', 'new_agent'):
        props = tool['inputSchema']['properties']
        props['backend'] = {'type': 'string', 'enum': ['claude', 'codex', 'opencode'],
                            'description': 'CLI backend for this new agent. Saved sessions remain tied to their original backend.'}
        props.setdefault('model', {'type': 'string', 'description': 'Backend model; OpenCode requires provider/model.'})


def run(name, a):
    n = a.get("node", "")
    p = a.get("project", "")
    if name == "list_displays":
        return json.dumps(call("GET", "/api/control/displays"), indent=1)
    if name == 'reset_stage':
        return json.dumps(call('POST','/api/control/presentation',{'action':'reset','base_revision':a['base_revision'],'source':ME}),indent=1)
    if name == "list_nodes":
        return json.dumps(call("GET", "/api/control/tree", timeout=20), indent=1)
    if name == "status":
        st = call("GET", f"/n/{n}/api/projects/{p}")
        call("POST", "/api/control/watch/news", {"node":n,"project":p,"turn_id":st.get("turn_id")})
        return json.dumps(st, indent=1)
    if name == "focus":
        view = a.get("view") or None
        receipt = call("POST", "/api/control/focus", {"node": n, "project": p or None, "by": ME, "display": a.get("display"), "base_revision":a.get("base_revision"), "view": view, "mode": "add" if a.get("add") else "replace", "pin": bool(a.get("pin"))})
        return json.dumps(receipt,indent=1)
    if name == "pin":
        receipt = call("POST", "/api/control/pin", {"node": a.get("node"), "project": a.get("project"), "pinned": a.get("pinned", True), "by": ME, "display": a.get("display"), "base_revision":a.get("base_revision")})
        return json.dumps(receipt,indent=1)
    if name == "stage":
        tiles = a.get("tiles") or []
        receipt = call("POST", "/api/control/stage", {"tiles": tiles, "by": ME, "display": a.get("display"), "base_revision":a.get("base_revision")})
        return json.dumps(receipt,indent=1)
    if name == "send":
        receipt = call("POST", f"/n/{n}/api/projects/{p}/send", {"text": a["message"], "origin": "conductor", "request_source":"automation", "request_id":a.get("request_id") or uuid.uuid4().hex})
        turn = receipt["turn_id"]
        # Delegation does not implicitly change presentation or bypass destination choice.
        if a.get("wait_for_result"):
            return f"Accepted by {n}/{p}, turn_id={turn}, delivery={receipt['delivery']}. Call result with this turn_id to collect the reply."
        call("POST", "/api/control/watch", {"node": n, "project": p, "turn_id":turn, "brief": a["message"][:200]})
        return f"Accepted by {n}/{p}; turn_id={turn}, delivery={receipt['delivery']}. You will get a [delegate-result] message here when it finishes — tell the human it is underway and end your turn."
    if name == "progress":
        st = call("GET", f"/n/{n}/api/projects/{p}")
        call("POST", "/api/control/watch/news", {"node":n,"project":p,"turn_id":st.get("turn_id")})
        since = st.get("turn_started")
        return json.dumps({"status": st.get("status"), "tool": st.get("tool"), "running_for_s": int(time.time() - since) if since else None,
                           "last_result": (st.get("last_result") or {}).get("result", "")[:300]})
    if name == "result":
        wait = max(0,min(600,int(a.get("wait",60))))
        turn_query = "&turn_id="+quote(a["turn_id"],safe="") if a.get("turn_id") else ""
        r = call("GET", f"/n/{n}/api/projects/{p}/result?wait={wait}{turn_query}", timeout=wait + 15)
        if r.get("pending"):
            return f"{n}/{p} is still working after {wait}s (status {r.get('status')}). Call result again to keep waiting."
        return (r.get("result") or "(no text)") + (f"\n\n[{n}/{p}: {r.get('subtype')}, ${(r.get('total_cost_usd') or 0):.3f}]")
    if name == "screenshot":
        data = call("GET", f"/n/{n}/screenshot.jpg", timeout=20, raw=True)
        path = SHOTS / f"{n}-{int(time.time())}.jpg"
        path.write_bytes(data)
        return f"Screenshot of {n} saved to {path} — Read it to look."
    if name == "computer":
        r = call("POST", f"/n/{n}/api/host/computer", {k: a.get(k) for k in ("action", "coordinate", "text", "amount")}, timeout=30)
        if r.get("image"):
            path = SHOTS / f"{n}-{int(time.time())}.jpg"
            path.write_bytes(base64.b64decode(r.pop("image")))
            r["screenshot"] = str(path)
        return json.dumps(r)
    if name == "start_project":
        r = call("POST", f"/n/{n}/api/projects", {"dir": a["dir"], "name": a.get("name"), "model": a.get("model"), "backend": a.get("backend"), "start": True, "instructions": a.get("instructions")})
        if r.get("error"):
            return "Error: " + r["error"]
        pr = r.get("project", {})
        return f"Project '{pr.get('id')}' ({pr.get('name')}) running on {n} in {pr.get('dir')}; surface workspace {r.get('surface_ws') or 'none'}. Use project='{pr.get('id')}' with send/focus."
    if name == "stop_project":
        return json.dumps(call("POST", f"/n/{n}/api/projects/{p}/stop"))
    if name == "remove_project":
        return json.dumps(call("DELETE", f"/n/{n}/api/projects/{p}"))
    if name == "agents":
        ag = call("GET", f"/n/{n}/api/projects/{p}/agents"); se = call("GET", f"/n/{n}/api/projects/{p}/sessions", timeout=60)
        lines = [f"active: {ag.get('active')}"] + [f"- agent {x['id']} \u201c{x['name']}\u201d session={x.get('session_id') or 'new'}" for x in ag.get("items", [])]
        prev = [x for x in se.get("items", []) if not x.get("known")][:12]
        if prev:
            lines.append("earlier sessions in this folder (resumable with new_agent session_id):")
            lines += [f"- {x['session_id']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(x['mtime']))}  \u201c{x.get('first','')[:80]}\u201d" for x in prev]
        return "\n".join(lines)
    if name == "new_agent":
        r = call("POST", f"/n/{n}/api/projects/{p}/agents", {"name": a["name"], "session_id": a.get("session_id"), "backend": a.get("backend"), "model": a.get("model"), "start": True})
        return json.dumps(r)
    if name == "switch_agent":
        return json.dumps(call("POST", f"/n/{n}/api/projects/{p}/agents/{a['agent']}/switch"))
    raise ValueError("unknown tool")


def _write(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


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
                                                          "capabilities": {"tools": {}}, "serverInfo": {"name": "conductor", "version": "0.1.0"}}})
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            text = run(params.get("name"), params.get("arguments") or {})
            _write({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
        elif method == "ping":
            _write({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            _write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
    except Exception as e:
        _write({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}})
