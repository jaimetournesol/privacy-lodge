"""OpenCode JSON run adapter; retains sessions and owns cancellable process groups."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentnode.codex_bridge import Bridge
from agentnode.mcp_config import opencode_servers


def working_directory(value=None):
    try:
        path = Path(value or Path.cwd()).resolve(strict=True)
        if not path.is_dir():
            raise ValueError('OpenCode working directory is not a directory: ' + str(path))
        return str(path)
    except OSError as exc:
        raise ValueError('OpenCode project working directory is unavailable. Restore the folder and restart this project.') from exc


def configuration(mcp_path, primer, workdir=None):
    directory = Path(working_directory(workdir))
    # Project config isolation also disables OpenCode's automatic instruction
    # discovery. Keep the project's standing brief without importing its MCPs.
    instructions = next(([str(directory / name)] for name in ('AGENTS.md', 'CLAUDE.md')
                         if (directory / name).is_file()), [])
    return {'$schema': 'https://opencode.ai/config.json',
            'mcp': opencode_servers(json.loads(Path(mcp_path).read_text())['mcpServers']),
            'agent': {'agentnode': {'description': 'AgentNode project agent', 'mode': 'primary', 'prompt': primer}},
            'autoupdate': False, 'share': 'disabled', 'instructions': instructions}


def provider_configuration(binary, env, workdir, diagnostic_path=None):
    """Resolve native JSON/JSONC/env/file references; import provider settings only.

    Never print resolved config: it may contain API keys. --pure prevents external
    plugins from loading during discovery. Native auth/session stores stay intact.
    """
    try:
        result = subprocess.run([binary, 'debug', 'config', '--pure'], env=env, cwd=workdir,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('OpenCode provider configuration lookup timed out; check opencode debug config --pure') from exc
    if result.returncode:
        if diagnostic_path:
            from agentnode.storage import atomic_text
            atomic_text(Path(diagnostic_path), result.stderr)
        raise ValueError(f'OpenCode provider configuration lookup failed (exit {result.returncode}); check opencode debug config --pure locally'
                         + (f'. Private diagnostics: {diagnostic_path}' if diagnostic_path else ''))
    try:
        resolved = json.loads(result.stdout)
        if not isinstance(resolved, dict):
            raise ValueError('expected object')
    except ValueError as exc:
        raise ValueError('OpenCode returned invalid configuration JSON') from exc
    return {key: resolved[key] for key in ('provider', 'enabled_providers', 'disabled_providers') if key in resolved}


def environment(mcp_path, primer, binary=None, workdir=None):
    workdir = working_directory(workdir)
    env = dict(os.environ, PWD=workdir)
    providers = provider_configuration(binary, env, workdir, Path(mcp_path).parent / 'logs/opencode-config-stderr.log') if binary else {}
    managed = {**configuration(mcp_path, primer, workdir), **providers}
    # Isolate user/project configuration, not CLI authentication or persisted sessions.
    # Provider credentials remain in OpenCode's ordinary XDG_DATA_HOME/auth store.
    root = Path(mcp_path).parent / 'opencode-config'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for key in ('OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR', 'OPENCODE_PERMISSION'):
        env.pop(key, None)
    env.update(XDG_CONFIG_HOME=str(root), OPENCODE_DISABLE_PROJECT_CONFIG='true',
               OPENCODE_CONFIG_CONTENT=json.dumps(managed))
    return env


def ensure_session_directory(binary, session_id, workdir, env, backup_dir):
    """Relocate old mis-rooted sessions with native export/import, retaining history.

    Import updates existing session directory metadata without replacing messages.
    Verify that guarantee against the installed CLI before delivering a new turn.
    """
    if not session_id:
        return
    workdir = working_directory(workdir)
    env = dict(env, PWD=workdir)
    def invoke(args):
        # Some CLI builds exit before a large piped export finishes flushing.
        # A regular private file preserves the complete history for verification.
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as output:
            result = subprocess.run([binary, *args, '--pure'], cwd=workdir, env=env,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.PIPE,
                                    text=True, timeout=30)
            if result.returncode:
                raise ValueError('OpenCode session directory check/repair failed; no new turn was delivered')
            output.seek(0)
            return output.read()
    original = json.loads(invoke(['export', session_id]))
    info = original.get('info', {})
    if info.get('id') != session_id or not info.get('directory'):
        raise ValueError('OpenCode export did not identify the saved session directory')
    if Path(info['directory']).resolve() == Path(workdir):
        return
    import hashlib
    from agentnode.storage import atomic_text, private_dir
    private_dir(Path(backup_dir))
    backup = Path(backup_dir) / (hashlib.sha256(session_id.encode()).hexdigest()[:20] + '-' + str(time.time_ns()) + '.json')
    atomic_text(backup, json.dumps(original))
    invoke(['import', str(backup)])
    repaired = json.loads(invoke(['export', session_id]))
    if (repaired.get('info', {}).get('id') != session_id or
        Path(repaired.get('info', {}).get('directory', '')).resolve() != Path(workdir) or
        repaired.get('messages') != original.get('messages')):
        raise ValueError('OpenCode session directory repair could not be verified; no new turn delivered. Backup: ' + str(backup))


def command(binary, model, session_id=None, images=(), yolo=False, workdir=None):
    workdir = working_directory(workdir)
    cmd = [binary, 'run', '--format', 'json', '--model', model, '--agent', 'agentnode', '--pure', '--dir', workdir]
    if session_id:
        cmd += ['--session', session_id]
    if yolo:
        cmd += ['--auto']
    for path in images:
        cmd += ['--file', str(path)]
    return cmd


def tool_events(part):
    state = part.get('state', {})
    iid = part.get('callID') or part.get('id', 'opencode-tool')
    name = part.get('tool', 'opencode_tool')
    # CLI emits completed tools, not start events. Show both without claiming
    # real-time running-tool visibility. Deliberately omit attachment payloads.
    return [
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': iid, 'name': name,
                                                      'input': state.get('input', {})}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': iid,
            'content': str(state.get('error') or state.get('output') or state.get('status', 'completed'))[:24000],
            'is_error': state.get('status') == 'error'}]}}]


class OpenCodeBridge(Bridge):
    def run_turn(self, msg):
        started = time.monotonic()
        texts_out, failure, completed, cost = [], None, False, 0.0
        try:
            with tempfile.TemporaryDirectory(prefix='agentnode-opencode-') as temp:
                images, texts = [], []
                content = msg.get('message', {}).get('content', [])
                if isinstance(content, str):
                    content = [{'type': 'text', 'text': content}]
                for part in content:
                    if part.get('type') == 'text':
                        texts.append(part['text'])
                    elif part.get('type') == 'image':
                        source = part['source']
                        ext = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif'}[source['media_type']]
                        path = Path(temp) / (str(len(images)) + ext)
                        path.write_bytes(base64.b64decode(source['data'], validate=True))
                        images.append(path)
                # Popen(cwd=...) does not update inherited PWD. OpenCode consults
                # PWD for its root, so pin the environment AND --dir on every turn.
                workdir = working_directory()
                cmd = command(self.args.binary, self.args.model, self.session_id, images, self.args.yolo, workdir)
                env = environment(self.args.mcp_config, self.args.primer, self.args.binary, workdir)
                session_key = (self.session_id, workdir)
                if getattr(self, '_directory_checked', None) != session_key:
                    ensure_session_directory(self.args.binary, self.session_id, workdir, env,
                                             Path(self.args.mcp_config).parent / 'opencode-session-backups')
                    self._directory_checked = session_key
                with self.lock:
                    if self.cancelled.is_set():
                        raise RuntimeError('Turn interrupted before launch')
                    child = self.child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=sys.stderr, env=env, cwd=workdir, start_new_session=True)
                try:
                    child.stdin.write('\n\n'.join(texts).encode())
                    child.stdin.close()
                    seen = set()
                    for raw in child.stdout:
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        sid = event.get('sessionID')
                        if sid and sid != self.session_id:
                            self.session_id = sid
                            self.emit({'type': 'system', 'subtype': 'init', 'session_id': sid,
                                       'model': self.args.model, 'mcp_servers': [], 'tools': []})
                        typ, part = event.get('type'), event.get('part', {})
                        key = (typ, part.get('id'))
                        if part.get('id') and key in seen:
                            continue
                        seen.add(key)
                        if typ == 'step_start':
                            completed = False
                            texts_out = []  # final response is the last assistant step
                        elif typ == 'text':
                            text = part.get('text', '')
                            texts_out.append(text)
                            self.emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}})
                        elif typ == 'tool_use':
                            for ev in tool_events(part):
                                self.emit(ev)
                        elif typ == 'step_finish':
                            completed = part.get('reason') in ('stop', 'end-turn')
                            cost += float(part.get('cost') or 0)
                        elif typ == 'error':
                            failure = str(event.get('error') or 'OpenCode turn failed')
                    rc = child.wait()
                    if rc != 0 or not completed:
                        failure = failure or f'OpenCode exited ({rc}) without completing the turn; inspect before retrying.'
                finally:
                    child.stdout.close()
                    if not child.stdin.closed:
                        child.stdin.close()
                    if child.poll() is None:
                        self.cancel()
        except Exception as exc:
            failure = str(exc)
        finally:
            with self.lock:
                self.child = None
        if self.cancelled.is_set():
            failure = 'OpenCode turn interrupted; inspect any partial work before retrying.'
        self.emit({'type': 'result', 'subtype': 'error' if failure else 'success', 'is_error': bool(failure),
                   'result': failure or '\n\n'.join(texts_out), 'duration_ms': int((time.monotonic() - started) * 1000),
                   'num_turns': 1, 'total_cost_usd': cost})


def main():
    p = argparse.ArgumentParser()
    for field in ('binary', 'model', 'mcp-config', 'primer'):
        p.add_argument('--' + field, required=True)
    p.add_argument('--resume')
    p.add_argument('--yolo', action='store_true')
    OpenCodeBridge(p.parse_args()).main()


if __name__ == '__main__':
    main()
