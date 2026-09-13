"""Persistent named Claude/Codex sessions with one delivery ledger per project."""
import asyncio
import base64
import json
import os
import subprocess
import signal
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from . import config
from .util import log, hub_call, IMAGE_MIME
from .storage import read_json, write_json, atomic_text, private_file
from .delivery import TurnLedger, DeliveryError
from .validation import command_parts
from .interaction import channels, instruction
from . import policy, history as transcripts
from .broadcast import send_all


class ProjectSession:
    HISTORY_EVENTS = 400
    HISTORY_BYTES = 8 * 1024 * 1024

    def __init__(self, loop: asyncio.AbstractEventLoop, project: dict, node: dict, hub_url: str | None):
        self.loop = loop
        self.project = project                # {id, name, dir, model?, mcps?}
        self.node = node
        self.hub_url = hub_url
        self.data = config.project_data(project["id"])
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self.history: list[dict] = []
        self.history_truncated = False
        self._history_sizes = deque()
        self._history_bytes = 0
        self.clients: set = set()
        self.viewers = {}
        self.status = "stopped"               # stopped | starting | idle | working
        self.current_tool: str | None = None
        self.total_cost = 0.0
        self.stop_requested = False
        self.desired_running = False
        self.restart_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.generation = 0
        self.restart_task = None
        self.drain_task = None
        self.current_turn = None
        self.write_timeout = 5
        self.ledger = TurnLedger(self.data / 'turns.json')
        self.history_file: Path | None = None
        self.surface_ws: str | None = None
        self.context: list[dict] = []
        self.hub_ok = False
        self.last_result: dict | None = None
        self.turn_done = asyncio.Event()
        self._fail_count = 0
        self.agents: list[dict] = []
        self.active_id = "main"
        self._load_state()
        self._write_ws_file()

    # ---- persistence: several named agents (sessions) per project, one active ----
    @property
    def agents_file(self):
        return self.data / "agents.json"

    def _load_state(self):
        st = read_json(self.agents_file, {})
        if not st:
            # migrate the single-session layout (state.json) into agents.json
            old = read_json(self.data / 'state.json', {})
            st = {"active": "main", "running": old.get("running", False),
                  "agents": [{"id": "main", "name": "main", "session_id": old.get("session_id"), "total_cost": old.get("total_cost", 0.0),
                              "history_file": old.get("history_file"), "surface_ws": old.get("surface_ws"),
                              "created": time.time(), "last_used": time.time()}]}
        self.agents = st.get("agents") or []
        self.active_id = st.get("active") or (self.agents[0]["id"] if self.agents else "main")
        self.desired_running = st.get("running", False)
        if not any(a["id"] == self.active_id for a in self.agents):
            self.agents.append({"id": self.active_id, "name": self.active_id, "session_id": None, "total_cost": 0.0,
                                "history_file": None, "surface_ws": None, "created": time.time(), "last_used": time.time()})
        from .backends import model_for
        for agent in self.agents:
            agent.setdefault('backend', self.project.get('backend') or self.node.get('backend', 'claude'))
            agent.setdefault('model', model_for(agent['backend'], self.node, self.project, agent))
        self._load_agent(self.agent)
        self._save_state()

    @property
    def agent(self) -> dict:
        return next(a for a in self.agents if a["id"] == self.active_id)

    def _load_agent(self, a: dict):
        self.session_id = a.get("session_id")
        self.total_cost = a.get("total_cost", 0.0)
        self.surface_ws = a.get("surface_ws")
        self.history = []
        self.history_truncated = False
        self._history_sizes.clear()
        self._history_bytes = 0
        self.last_result = None
        hf = a.get("history_file")
        if hf and Path(hf).exists():
            self.history_file = Path(hf)
            private_file(self.history_file)
            with self.history_file.open('rb') as f:
                f.seek(0,2);start=max(0,f.tell()-self.HISTORY_BYTES);f.seek(start)
                tail=f.read(self.HISTORY_BYTES)
            if start:
                tail=tail.partition(b'\n')[2]
                self.history_truncated=True
            lines=tail.splitlines()
            if len(lines)>self.HISTORY_EVENTS:self.history_truncated=True
            for line in lines[-self.HISTORY_EVENTS:]:
                try:
                    event=json.loads(line)
                    if isinstance(event,dict):self._remember(event,len(line))
                except Exception:
                    pass
        else:
            self._new_history_file()
            a["history_file"] = str(self.history_file)

    def _new_history_file(self):
        self.history_file = self.data / f"history-{self.active_id}-{uuid.uuid4().hex}.jsonl"
        private_file(self.history_file)

    def _save_state(self):
        a = self.agent
        a.update({"session_id": self.session_id, "total_cost": self.total_cost, "history_file": str(self.history_file),
                  "surface_ws": self.surface_ws, "last_used": time.time()})
        write_json(self.agents_file, {"active": self.active_id, "running": self.desired_running, "agents": self.agents})

    def _write_ws_file(self):
        atomic_text(self.data / "surface-ws.txt", self.surface_ws or "")

    # ---- agents (named sessions) ----
    @property
    def backend(self):
        return self.agent.get('backend') or self.project.get('backend') or self.node.get('backend', 'claude')

    @property
    def model(self):
        from .backends import model_for
        return model_for(self.backend, self.node, self.project, self.agent)

    def list_agents(self) -> list[dict]:
        return [{**a, "approval":policy.approval(a,self.project), "active": a["id"] == self.active_id} for a in self.agents]

    def claude_sessions(self) -> list[dict]:
        """Claude Code sessions that were run in this folder (by anyone): candidates to resume as an agent."""
        if self.backend != 'claude':
            return []  # A Claude archive cannot be resumed by Codex.
        import re
        enc = re.sub(r"[^A-Za-z0-9]", "-", self.project["dir"])
        d = Path.home() / ".claude" / "projects" / enc
        known = {a.get("session_id") for a in self.agents}
        out = []
        for f in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)[:40]:
            sid = f.stem
            first = ""
            try:
                with open(f) as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("type") == "user":
                            c = r.get("message", {}).get("content")
                            if isinstance(c, str):
                                first = c
                            elif isinstance(c, list):
                                first = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                            first = first.strip()
                            if first and not first.startswith("[") and not first.startswith("<"):
                                break
            except Exception:
                pass
            out.append({"session_id": sid, "mtime": f.stat().st_mtime, "size": f.stat().st_size, "first": first[:160], "known": sid in known})
        return out

    async def new_agent(self, name: str | None = None, session_id: str | None = None, backend=None, model=None):
        async with self.restart_lock:
            policy.check_mutation()
            aid = self._add_agent(name, session_id, backend, model)
            a=next(a for a in self.agents if a['id']==aid)
            a['approval']='pending'
            self._save_state()
            self.broadcast_status()
            return a

    def _add_agent(self, name=None, session_id=None, backend=None, model=None):
        from .backends import validate_backend, model_for
        chosen = validate_backend(backend or self.backend)
        chosen_model = model or (self.model if chosen == self.backend else model_for(chosen, self.node))
        model_for(chosen, self.node, agent={'backend': chosen, 'model': chosen_model})
        if session_id and (chosen != 'claude' or self.backend != 'claude'):
            raise ValueError('Switch to a saved Claude agent before importing a Claude session.')
        base = config.slug(name or time.strftime("agent-%Y%m%d-%H%M"))
        aid = base
        n = 2
        while any(a["id"] == aid for a in self.agents):
            aid = f"{base}-{n}"
            n += 1
        self.agents.append({"id": aid, "name": name or aid, "backend": chosen, "model": chosen_model,
                            "session_id": session_id, "total_cost": 0.0, "history_file": None,
                            "surface_ws": None, "created": time.time(), "last_used": time.time()})
        return aid

    async def switch_agent(self, aid: str, expected_agent=None):
        async with self.restart_lock:
            if expected_agent is not None and expected_agent != self.active_id:
                raise policy.PolicyError('Active agent changed; review before activating',409)
            await self._switch_agent(aid)

    async def _switch_agent(self, aid):
        policy.check_mutation()
        if not any(a['id'] == aid for a in self.agents):
            raise ValueError('unknown agent')
        target = next(a for a in self.agents if a['id'] == aid)
        was_running = self.desired_running or bool(self.proc and self.proc.poll() is None)
        if was_running: policy.check_execution(target, self.project)
        await self._stop()
        self.active_id = aid
        self._load_agent(self.agent)
        self._write_ws_file()
        self.context = []
        self._save_state()
        await self.broadcast({'type':'bridge','subtype':'agent_switched','agent':self.agent,'ts':time.time()})
        await self.ensure_workspace()
        if was_running: self._start()
        await self.broadcast(self.history_payload())
        self.broadcast_status()

    def rename_agent(self, aid: str, name: str):
        policy.check_mutation()
        for a in self.agents:
            if a["id"] == aid:
                a["name"] = name
        self._save_state()

    async def delete_agent(self, aid: str):
        policy.check_mutation()
        if aid == self.active_id:
            raise ValueError("stop or switch away from the active agent first")
        self.agents = [a for a in self.agents if a["id"] != aid]
        self._save_state()

    def _record(self, ev: dict):
        pos = self.history_file.stat().st_size if self.history_file.exists() else 0
        ev['event_id'] = transcripts.cursor(self.history_file,pos)
        ev['agent'] = self.active_id
        line=json.dumps(ev)
        with open(self.history_file, "a") as f:
            f.write(line + "\n")
        ev['cursor'] = transcripts.cursor(self.history_file,pos+len(line.encode())+1)
        self._remember(ev,len(line.encode()))

    def _remember(self,event,size):
        if size>self.HISTORY_BYTES:
            self.history_truncated=True
            return
        self.history.append(event);self._history_sizes.append(size);self._history_bytes+=size
        while len(self.history)>self.HISTORY_EVENTS or self._history_bytes>self.HISTORY_BYTES:
            self.history.pop(0);self._history_bytes-=self._history_sizes.popleft();self.history_truncated=True

    def history_payload(self):
        return {'type':'history','events':self.history,'truncated':self.history_truncated}

    # ---- MCP config per project ----
    def mcp_config_path(self) -> Path:
        mcps = {}
        import sys
        py = self.node.get("python") or sys.executable   # the node's own venv python runs the MCP scripts
        mcpdir = config.REPO / "mcp"
        env_common = {"AGENTNODE_TOKEN": (self.node.get("token") or ""), "AGENTNODE_URL": f"http://127.0.0.1:{self.node['port']}", "AGENTNODE_PROJECT": self.project["id"],
                      "AGENTNODE_HOST_BACKEND": config.host_backend(self.node)}
        # host computer use (this machine's screen/mouse/keyboard)
        if self.project.get("host_tools", True) and self.node.get("host_tools", True):
            mcps["host"] = {"command": py, "args": [str(mcpdir / "computer_mcp.py")], "env": env_common}
        # surface panels
        if self.hub_url and self.node.get("surface_dir"):
            mcps["surface"] = {"command": "bash", "args": [str(mcpdir / "surface-mcp-launch.sh")],
                               "env": {**env_common, "SURFACE_DIR": self.node["surface_dir"], "SURFACE_HUB_URL": self.hub_url,
                                       "SURFACE_STATE_DIR": self.node.get("surface_state_dir", self.node["surface_dir"]),
                                       "SURFACE_WS_FILE": str(self.data / "surface-ws.txt")}}
        # voice (say / ask)
        if os.environ.get('LODGE_MODE') != '1':
            mcps["voice"] = {"command": py, "args": [str(mcpdir / "voice_mcp.py")], "env": env_common}
        # conductor tools (control node's orchestrator only)
        if self.project.get("conductor") and self.node.get("control"):
            mcps["conductor"] = {"command": py, "args": [str(mcpdir / "conductor_mcp.py")], "env": env_common}
        from .mcp_config import validate_servers, RESERVED
        own_path = Path(self.project['dir']) / '.mcp.json'
        if own_path.exists():
            own = json.loads(own_path.read_text()).get('mcpServers', {})
            # Old project files may contain fleet entries. Managed roles always win,
            # including roles disabled on this machine; never resurrect them here.
            for name, spec in validate_servers(own).items():
                if name not in RESERVED:
                    mcps[name] = spec
        extra = validate_servers(self.project.get('mcps') or {})
        if RESERVED.intersection(extra):
            raise ValueError('host, surface, voice and conductor are reserved MCP roles')
        mcps.update(extra)
        if self.backend == 'claude' and self.node.get('claude_oauth_token_file'):
            # Locally launched MCPs do not need the parent Claude login token.
            # Preserve an explicit credential configured for a custom MCP.
            mcps = {name: ({**spec, 'env': {'CLAUDE_CODE_OAUTH_TOKEN': '', **spec.get('env', {})}}
                           if 'command' in spec else spec) for name, spec in mcps.items()}
        validate_servers(mcps)
        p = self.data / "mcp.json"
        write_json(p, {"mcpServers": mcps})
        return p

    def fleet_primer(self) -> str:
        from .agent_contract import render
        servers = json.loads(self.mcp_config_path().read_text())['mcpServers']
        return render(self, servers)

    def cmd(self) -> list[str]:
        from .backends import binary_for, validate_backend
        validate_backend(self.backend)
        if self.backend in ('codex', 'opencode'):
            import sys
            c = [self.node.get('python') or sys.executable, str(config.REPO / ('agentnode/' + self.backend + '_bridge.py')),
                 '--binary', binary_for(self.backend, self.node),
                 '--model', self.model, '--mcp-config', str(self.mcp_config_path()), '--primer', self.fleet_primer()]
            if self.backend == 'codex' and os.environ.get('LODGE_MODE') == '1' and self.node.get('lodge_container'):
                c.append('--container-sandbox')
            if self.project.get(self.backend + '_yolo', self.node.get(self.backend + '_yolo', False)):
                c.append('--yolo')
            if self.session_id:
                c += ['--resume', self.session_id]
            return c
        c = [self.node["claude_bin"], "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--include-partial-messages", "--model", self.model,
             "--dangerously-skip-permissions", "--mcp-config", str(self.mcp_config_path()), "--strict-mcp-config",
             "--append-system-prompt", self.fleet_primer()]
        if self.session_id:
            c += ["--resume", self.session_id]
        return c

    # ---- process ----
    def check_target(self, expected_agent=None):
        if expected_agent is not None and expected_agent != self.active_id:
            raise policy.PolicyError("Active agent changed; review the target",409)

    async def start(self, expected_agent=None):
        async with self.restart_lock:
            self.check_target(expected_agent)
            policy.check_execution(self.agent, self.project)
            await self.ensure_workspace()
            self._start()

    def _start(self):
        policy.check_execution(self.agent, self.project)
        if self.proc and self.proc.poll() is None:
            self._ensure_drain()
            return
        self.stop_requested = False
        self.desired_running = True
        self.status = 'starting'
        self.generation += 1
        generation = self.generation
        self._save_state()
        stderr = private_file(self.data / 'logs' / (self.backend + '-stderr.log')).open('ab')
        env = dict(os.environ)
        env.pop('CLAUDECODE', None)
        env['PWD'] = str(Path(self.project['dir']).resolve())
        from .backends import binary_for, process_env
        binary = binary_for(self.backend, self.node)
        env['PATH'] = str(Path(binary).parent) + os.pathsep + env.get('PATH', '')
        try:
            env = process_env(self.backend, self.node, env)
            self.proc = subprocess.Popen(self.cmd(), cwd=self.project['dir'], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=stderr, env=env, bufsize=0, start_new_session=True)
            os.set_blocking(self.proc.stdin.fileno(), False)
        except Exception as exc:
            self.proc = None
            self.status = 'stopped'
            self.desired_running = False
            self._save_state()
            self.broadcast_status()
            raise DeliveryError(f'Cannot start agent: {exc}') from exc
        finally:
            stderr.close()
        threading.Thread(target=self._reader, args=(self.proc,generation), daemon=True).start()
        self.status = 'idle'
        self.broadcast_status()
        self._ensure_drain()

    def _reader(self, proc, generation):
        try:
            for raw in proc.stdout:
                try: ev = json.loads(raw)
                except ValueError: continue
                # Bound queued callbacks and retain ordering from this process.
                try: asyncio.run_coroutine_threadsafe(self.handle_event(ev,generation),self.loop).result()
                except Exception: break
            rc = proc.wait()
            asyncio.run_coroutine_threadsafe(self.on_exit(rc,generation),self.loop)
        finally:
            proc.stdout.close()

    async def on_exit(self, rc, generation=None):
        async with self.restart_lock:
            await self._on_exit(rc, generation)

    async def _on_exit(self, rc, generation=None):
        if generation is not None and generation != self.generation: return
        if self.proc and self.proc.stdin: self.proc.stdin.close()
        self.proc = None
        self.status = 'stopped'
        self.current_tool = None
        if self.current_turn:
            self._finish_turn({'type':'result','subtype':'process_exit','is_error':True,
                              'result':f'Agent process exited ({rc}); inspect its work before retrying.'})
        self.turn_done.set()
        await self.broadcast({'type':'bridge','subtype':'process_exit','rc':rc,'ts':time.time()})
        self.broadcast_status()
        if self.stop_requested or not self.desired_running: return
        self._fail_count += 1
        if self._fail_count >= 5:
            self.desired_running = False
            self._save_state()
            await self.broadcast({'type':'bridge','subtype':'error','text':'Agent repeatedly exited; automatic restart paused. Existing session retained.'})
            return
        async def retry():
            await asyncio.sleep(min(30, 2 ** self._fail_count))
            async with self.restart_lock:
                if not self.stop_requested and self.desired_running and generation == self.generation:
                    try: self._start()
                    except DeliveryError as exc: log(exc)
        self.restart_task = asyncio.create_task(retry())

    async def stop(self, preserve_running=False, expected_agent=None):
        async with self.restart_lock:
            self.check_target(expected_agent)
            if not preserve_running: policy.check_mutation()
            await self._stop(preserve_running)

    async def _stop(self, preserve_running=False):
        wanted = self.desired_running
        self.stop_requested = True
        self.desired_running = False
        self.generation += 1
        if self.restart_task:
            self.restart_task.cancel()
            self.restart_task = None
        if self.drain_task and self.drain_task is not asyncio.current_task():
            self.drain_task.cancel()
            await asyncio.gather(self.drain_task, return_exceptions=True)
            self.drain_task = None
        proc, self.proc = self.proc, None
        if proc:
            try:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try: await asyncio.to_thread(proc.wait, 5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        await asyncio.to_thread(proc.wait, 5)
            except ProcessLookupError: pass
            finally:
                if proc.stdin: proc.stdin.close()
        if self.current_turn:
            self._finish_turn({'type':'result','subtype':'interrupted','is_error':True,'result':'Agent stopped before completing this turn.'})
        if not preserve_running:
            for item in self.ledger.items.values():
                if item['agent'] == self.active_id and item['status'] == 'queued':
                    self.ledger.update(item['id'],'cancelled',{'is_error':True,'subtype':'cancelled','result':'Queued command cancelled when the agent was stopped or switched.'})
        self.status = 'stopped'
        self.current_tool = None
        self.turn_done.set()
        self.desired_running = wanted if preserve_running else False
        self._save_state()
        self.broadcast_status()

    async def _write_message(self, proc, msg):
        data = memoryview((json.dumps(msg)+'\n').encode())
        if len(data) > 24 * 1024 * 1024: raise DeliveryError('command exceeds 24 MiB')
        async def write():
            nonlocal data
            while data:
                if self.proc is not proc or proc.poll() is not None: raise DeliveryError('agent process changed during delivery')
                try:
                    n = os.write(proc.stdin.fileno(), data)
                    if n == 0: raise DeliveryError('agent input closed')
                    data = data[n:]
                except BlockingIOError:
                    # A short cooperative wait works on both supported POSIX platforms.
                    await asyncio.sleep(.01)
        async with self.write_lock:
            # wait_for supports the fleet's Python 3.10 nodes as well as newer runtimes.
            await asyncio.wait_for(write(), self.write_timeout)

    def _ensure_drain(self):
        if self.drain_task is None or self.drain_task.done():
            self.drain_task = asyncio.create_task(self._drain())

    async def _drain(self):
        while not self.stop_requested:
            if self.current_turn:
                await self.turn_done.wait()
                continue
            item = self.ledger.pending(self.active_id)
            if not item: return
            async with self.restart_lock:
                if self.stop_requested or item['agent'] != self.active_id: return
                try:
                    self.current_turn = item['id']
                    self.turn_done.clear()
                    await self._deliver_user(**item['input'])
                except Exception as exc:
                    result={'type':'result','subtype':'delivery_failed','is_error':True,
                            'result':str(exc) or 'Agent delivery timed out; inspect its work before retrying.', 'ts':time.time()}
                    self._finish_turn(result)
                    # A partial JSON write leaves an unusable stream. Never send the next
                    # command into it or retry the possibly delivered command implicitly.
                    await self._stop(preserve_running=True)
                    self.stop_requested = False
                    await self.broadcast(result)
                    self.status = 'idle' if self.proc and self.proc.poll() is None else 'stopped'
                    self.broadcast_status()

    def _finish_turn(self, result):
        result['response_channel']=self.response_channel()
        result['voice_target']=self.voice_target()
        if self.current_turn:
            result['turn_id'] = self.current_turn
            self.ledger.update(self.current_turn,'failed' if result.get('is_error') else 'completed',result)
        self.current_turn = None
        self.last_result = result
        self.turn_done.set()

    async def handle_event(self, ev: dict, generation=None):
        async with self.restart_lock:
            await self._handle_event(ev, generation)

    async def _handle_event(self, ev: dict, generation=None):
        if generation is not None and generation != self.generation: return
        t = ev.get("type")
        ev["ts"] = time.time()
        if t == "system":
            if ev.get("subtype") == "init":
                self.session_id = ev.get("session_id")
                if self.status == "starting":
                    self.status = "idle"
                self._save_state()
                initial={"type": "system", "subtype": "init", "session_id": self.session_id, "model": ev.get("model"),
                              "mcp_servers": ev.get("mcp_servers"),
                              "tools": [x for x in ev.get("tools", []) if x.startswith("mcp__")], "ts": ev["ts"]}
                self._record(initial)
                await self.broadcast(initial)
                self.broadcast_status()
            return
        if t == "stream_event":
            e = ev.get("event", {})
            if e.get("type") == "content_block_delta":
                await self.broadcast({"type": "delta", "index": e.get("index"), "delta": e.get("delta")})
            elif e.get("type") == "content_block_start":
                await self.broadcast({"type": "block_start", "index": e.get("index"), "block": e.get("content_block")})
            elif e.get("type") == "message_start":
                self.status = "working"
                self.broadcast_status()
            return
        if t == "assistant":
            self.status = "working"
            for c in ev["message"].get("content", []):
                if c.get("type") == "tool_use":
                    self.current_tool = c.get("name", "").replace("mcp__", "").replace("__", ":")
            self._record(ev)
            await self.broadcast(ev)
            self.broadcast_status()
            return
        if t == "user":
            self.current_tool = None
            self._record(ev)
            await self.broadcast(ev)
            self.broadcast_status()
            return
        if t == "result":
            self.status = "idle"
            self.current_tool = None
            self.total_cost += ev.get("total_cost_usd") or 0
            self._save_state()
            slim = {k: ev.get(k) for k in ("type", "subtype", "duration_ms", "num_turns", "total_cost_usd", "is_error", "ts")}
            slim["result"] = ev.get("result") or ""
            slim['response_channel']=self.response_channel()
            slim['voice_target']=self.voice_target()
            self._fail_count = 0
            if self.current_turn: slim['turn_id'] = self.current_turn
            self._record(slim)
            self._finish_turn(slim)
            if ev.get("result"):
                asyncio.ensure_future(self.mirror_chat("assistant", ev.get("result") or ""))
            await self.broadcast(slim)
            self.broadcast_status()
            return
        if t == "control_response":
            return
        await self.broadcast({"type": "raw", "event": ev})

    # ---- surface ----
    async def ensure_workspace(self):
        if self.surface_ws or not self.hub_url or not self.node.get('surface_dir'):
            return
        try:
            result = await hub_call(self.hub_url, 'POST', '/api/workspaces',
                                    {'name': self.project['name'] + ' · ' + self.agent['name']})
            if not result.get('id'):
                raise ValueError('hub did not return a workspace ID')
            await self.select_workspace(result['id'])
        except Exception as exc:
            raise DeliveryError('Cannot provision this agent’s Surface workspace: ' + str(exc)) from exc

    async def select_workspace(self, ws_id: str | None, expected_agent=None):
        self.check_target(expected_agent)
        policy.check_mutation()
        self.surface_ws = ws_id or None
        self._write_ws_file()
        self._save_state()
        if self.surface_ws and self.hub_url:
            try:
                items = (await hub_call(self.hub_url, "GET", "/api/workspaces")).get("items", [])
                info = next((w for w in items if w.get("id") == self.surface_ws), None)
                if info:
                    self.push_context(f"the active Surface workspace is now “{info.get('name')}” (id={info.get('id')}); "
                                      f"its folder is {info.get('dir')} — put apps under {info.get('dir')}/apps/<name>/ and use ui_list to inspect this workspace; canonical Stage context identifies which workspaces are presented",
                                      key="surface:workspace")
            except Exception as e:
                log("workspace lookup failed:", e)
        await self.broadcast({"type": "bridge", "subtype": "surface_ws", "ws": self.surface_ws, "ts": time.time()})
        self.broadcast_status()
        await self.report_phase()

    def push_context(self, note: str, key: str | None = None):
        if key:
            self.context = [c for c in self.context if c.get("key") != key]
            self.context.append({"key": key, "note": note})
        elif not any(c["note"] == note for c in self.context):
            self.context.append({"note": note})

    def _phase(self) -> str:
        if not (self.proc and self.proc.poll() is None):
            return "stopped"
        if self.status == "working":
            return "tool" if self.current_tool else "thinking"
        return {"starting": "starting", "idle": "idle"}.get(self.status, "idle")

    async def report_phase(self):
        if not self.surface_ws or not self.hub_url:
            return
        try:
            await hub_call(self.hub_url, "POST", "/internal/agent", {
                "ws": self.surface_ws, "phase": self._phase(), "activeTool": self.current_tool or "",
                "model": self.model, "sessionId": self.session_id or "",
                "costUsd": round(self.total_cost, 4)})
            self.hub_ok = True
        except Exception as e:
            if self.hub_ok:
                log("hub unreachable:", e)
            self.hub_ok = False

    async def mirror_chat(self, role: str, text: str):
        if not self.surface_ws or not self.hub_url or not text.strip():
            return
        try:
            await hub_call(self.hub_url, "POST", "/internal/chat", {"ws": self.surface_ws, "role": role, "text": text})
        except Exception:
            pass

    # ---- commands ----
    async def send_user(self, text: str, files=None, annotations=None, kind="user_input", voice=None, origin=None, request_id=None, expected_agent=None, request_source=None, response_channel=None, presentation_display=None, presentation_reset_revision=None, require_running=False):
        if (config.HOME / "deployment-maintenance.json").exists():
            raise DeliveryError("Deployment in progress; retry after maintenance finishes")
        if not isinstance(text, str) or not text.strip() or len(text) > 100000:
            raise DeliveryError('message must contain 1–100000 characters')
        command_parts(files,annotations,voice)
        if presentation_display is not None:
            import re
            if not isinstance(presentation_display,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',presentation_display):
                raise DeliveryError('Invalid presentation display')
        if presentation_reset_revision is None and self.project.get('conductor') and self.node.get('control'):
            from .server import presentation_stage
            presentation_reset_revision = presentation_stage.last_reset_revision
        request_source,response_channel=channels(request_source,voice,response_channel)
        payload = dict(text=text, files=files, annotations=annotations, kind=kind, voice=voice, origin=origin,
                       request_source=request_source,response_channel=response_channel,presentation_display=presentation_display,
                       presentation_reset_revision=presentation_reset_revision)
        if require_running:payload['require_running']=True
        if presentation_display is None:payload.pop('presentation_display')
        if presentation_reset_revision is None:payload.pop('presentation_reset_revision')
        async with self.restart_lock:
            policy.check_execution(self.agent, self.project)
            existing = self.ledger.get(request_id) if request_id else None
            if existing:
                # Internal acceptance metadata must remain stable on idempotent retries.
                original = existing.get('input',{})
                if 'presentation_reset_revision' in original:
                    payload['presentation_reset_revision'] = original['presentation_reset_revision']
                else:payload.pop('presentation_reset_revision',None)
            if expected_agent is not None and expected_agent != self.active_id:
                raise DeliveryError('active agent changed; review the target before sending')
            if require_running and (not self.proc or self.proc.poll() is not None):
                raise DeliveryError('The operator must start this agent before it can receive messages')
            item = self.ledger.accept(self.active_id,payload,request_id)
            if item['status'] == 'queued':
                self.stop_requested = False
                self.desired_running = True
                self._save_state()
                self._ensure_drain()
        return {'ok':True,'turn_id':item['id'],'delivery':item['status'],'agent':item['agent']}

    def voice_target(self):
        item=self.ledger.get(self.current_turn) if self.current_turn else None
        return ((item or {}).get('input',{}).get('voice') or {}).get('client_id')

    def response_channel(self):
        item=self.ledger.get(self.current_turn) if self.current_turn else None
        payload=item.get('input',{}) if item else {}
        return channels(payload.get('request_source'),payload.get('voice'),payload.get('response_channel'))[1]

    async def _deliver_user(self, text, files=None, annotations=None, kind='user_input', voice=None, origin=None, request_source=None, response_channel=None, presentation_display=None, presentation_reset_revision=None, require_running=False):
        self.current_stage_reset_revision = presentation_reset_revision
        await self.ensure_workspace()
        if not self.proc or self.proc.poll() is not None:
            if require_running:raise DeliveryError('The agent stopped before this message could be delivered')
            self._start()
        request_source,response_channel=channels(request_source,voice,response_channel)
        ev = {"type": kind, "text": text, "ts": time.time(), 'request_source':request_source,'response_channel':response_channel,'turn_id':self.current_turn,'voice_target':(voice or {}).get('client_id')}
        if origin:
            ev["origin"] = origin
        if annotations:
            ev["annotations"] = [{"componentId": a.get("componentId"), "title": a.get("title"), "marks": len(a.get("marks") or [])}
                                 for a in annotations]
        content: list[dict] = []
        prefix = ""
        if self.context:
            prefix = "[surface-context]\n" + "\n".join("- " + c["note"] for c in self.context) + "\n\n"
        for a in annotations or []:
            marks = a.get("marks") or []
            prefix += (f"[surface-context] the human froze the {a.get('componentType', 'panel')} panel "
                       f"“{a.get('title') or a.get('componentId')}” (id={a.get('componentId')}) and circled "
                       f"{len(marks)} region(s) — normalized {{x,y,w,h}} boxes: {json.dumps(marks)}; their marked-up screenshot is attached.\n\n")
            url = a.get("imageDataUrl") or ""
            if url:
                media_type=url.split(';',1)[0][5:]
                content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": url.split(",", 1)[1]}})
        if files:
            prefix += "[attachments] Files supplied by the human on this machine (read them with your file tools): " + json.dumps(files,ensure_ascii=False) + "\n\n"
            ev["files"] = [{"name":Path(f).name} for f in files]
        for f in files or []:
            ext = Path(f).suffix.lower()
            if ext in IMAGE_MIME and Path(f).exists() and Path(f).stat().st_size < 8_000_000:
                content.append({"type": "image", "source": {"type": "base64", "media_type": IMAGE_MIME[ext],
                                                            "data": base64.b64encode(Path(f).read_bytes()).decode()}})
        if origin and kind != "delegate_result":
            prefix += f"[from {origin}] "
        if presentation_display:
            prefix += "[presentation-request] The human selected destination ID " + json.dumps(presentation_display) + ". Use it for this task; if offline ask before moving content elsewhere. [/presentation-request]\n\n"
        prefix += instruction(request_source,response_channel)+"\n\n"
        prefix += self.fleet_primer() + "\n\n"
        if self.project.get("conductor") and self.node.get("control"):
            try:
                from .server import fleet_roster, presentation_stage, presenter_displays
                prefix = presentation_stage.context() + "\n\n" + presenter_displays.context(presentation_stage) + "\n\n" + prefix
                roster = await asyncio.wait_for(fleet_roster(), timeout=3)
                if roster:
                    prefix = roster + "\n\n" + prefix
            except Exception:
                pass
        if prefix:content.append({"type":"text","text":prefix})
        content.append({"type": "text", "text": text})
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        self.turn_started = time.time()
        # Persist uncertainty before the first byte can reach the child. A crash
        # must not replay a command that may already have performed actions.
        self.ledger.update(self.current_turn, 'delivered')
        await self._write_message(self.proc, msg)
        self.context = []
        self.status = 'working'
        ev['turn_id'] = self.current_turn
        self._record(ev)
        await self.broadcast(ev)
        if kind == 'user_input': asyncio.create_task(self.mirror_chat('user', text))
        self.broadcast_status()

    async def interrupt(self, expected_agent=None):
        async with self.restart_lock:
            self.check_target(expected_agent)
            policy.check_mutation()
            if self.proc and self.proc.poll() is None:
                await self._write_message(self.proc, {'type':'control_request','request_id':uuid.uuid4().hex,'request':{'subtype':'interrupt'}})
        await self.broadcast({'type':'bridge','subtype':'interrupt','ts':time.time()})

    async def restart(self, fresh=False, expected_agent=None):
        async with self.restart_lock:
            self.check_target(expected_agent)
            policy.check_execution(self.agent, self.project)
            was_running = self.desired_running or bool(self.proc and self.proc.poll() is None)
            if fresh:
                aid = self._add_agent()
                await self._switch_agent(aid)
            else:
                await self._stop()
                await self.ensure_workspace()
            if was_running: self._start()

    # ---- websockets ----
    def status_payload(self):
        return {"type": "status", "project": self.project["id"], "name": self.project["name"], "status": self.status,
                "agent": self.active_id, "agent_name": self.agent.get("name"), "agents": len(self.agents),
                "turn_id": self.current_turn, "queued": sum(t['status']=='queued' for t in self.ledger.items.values()),
                "tool": self.current_tool, "session_id": self.session_id, "cost": round(self.total_cost, 4),
                "model": self.model, "approval": policy.approval(self.agent, self.project),
                "backend": self.backend, "alive": bool(self.proc and self.proc.poll() is None), "running": self.desired_running,
                "surface_ws": self.surface_ws, "hub_ok": self.hub_ok, "pending_context": len(self.context),
                "turn_started": getattr(self, "turn_started", None) if self.status == "working" else None,
                "ts": time.time()}

    on_status = None   # set by the server: forwards compact status to the control channel

    def broadcast_status(self):
        asyncio.ensure_future(self.broadcast(self.status_payload()))
        asyncio.ensure_future(self.report_phase())
        if ProjectSession.on_status:
            try:
                ProjectSession.on_status(self)
            except Exception:
                pass

    async def broadcast(self, ev: dict):
        ev = {**ev, 'agent':ev.get('agent',self.active_id)}
        await send_all(self.clients, ev)
        async def send_viewer(viewer):
            if viewer['agent'] != ev['agent']: return
            if viewer['initializing']:
                if len(viewer['pending'])>=1000:
                    for ws in viewer['clients']:await ws.close(code=1013)
                else:viewer['pending'].append(ev)
                return
            async with viewer['lock']:
                await send_all(viewer['clients'], transcripts.preview(ev,ev.get('event_id','')))
        await asyncio.gather(*(send_viewer(v) for v in list(self.viewers.values())))

    def viewed_status(self, aid):
        a = next((a for a in self.agents if a['id']==aid),None)
        if not a: raise ValueError('Unknown agent')
        if aid == self.active_id: return self.status_payload()
        return dict(type='status',project=self.project['id'],agent=aid,agent_name=a['name'],
                    status='stopped',alive=False,running=False,model=a.get('model'),backend=a.get('backend'),
                    session_id=a.get('session_id'),surface_ws=a.get('surface_ws'),approval=policy.approval(a,self.project),queued=0)

    def history_path(self, aid):
        a = next((a for a in self.agents if a['id']==aid),None)
        if not a: raise ValueError('Unknown agent')
        if not a.get('history_file'):
            path = self.data / ('history-'+aid+'-empty.jsonl')
            private_file(path)
            return path
        return Path(a['history_file'])
