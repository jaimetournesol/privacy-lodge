"""Codex exec JSONL adapter for the existing persistent session transport.

One resumable Codex turn at a time; never retries a potentially executed turn.
The outer process stays idle between turns and owns its child process group.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time


def toml(value):
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k) + ' = ' + toml(v) for k, v in value.items() if v is not None) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(toml(v) for v in value) + ']'
    return json.dumps(value)


def command(binary, model, mcp_path, primer, session_id=None, images=(), output=None, yolo=False, container_sandbox=False):
    if container_sandbox and os.environ.get('LODGE_MODE') != '1':
        raise ValueError('Container execution is only available in a Lodge runtime')
    mcps = json.loads(Path(mcp_path).read_text())['mcpServers']
    for name, spec in mcps.items():
        if 'type' in spec:
            kind = spec.pop('type')
            if kind not in ('stdio', 'http'):
                raise ValueError('Codex adapter supports stdio and HTTP MCP servers only')
        if 'headers' in spec:
            spec['http_headers'] = spec.pop('headers')
        if os.environ.get('LODGE_MODE') == '1' and name in ('conductor', 'surface'):
            # These are reserved, product-owned MCP roles. Agentnode checks the
            # agent's approval before launching this bridge; Codex exec cannot
            # display a second interactive approval for each delegated tool call.
            # Custom servers retain Codex's own approval behavior.
            spec['default_tools_approval_mode'] = 'approve'
    cmd = [binary, 'exec']
    if session_id:
        cmd += ['resume']
    cmd += ['--json', '--skip-git-repo-check', '--ignore-user-config',
            '-c', 'mcp_servers=' + toml(mcps), '-c', 'developer_instructions=' + toml(primer)]
    # Let the authenticated CLI select its currently recommended model. A pinned
    # historical default may disappear from ChatGPT accounts after a rollout.
    if model != 'auto':
        cmd += ['-m', model]
    if yolo:
        cmd += ['--dangerously-bypass-approvals-and-sandbox']
    else:
        # Docker is the execution boundary in the shipped Lodge containers. Their
        # restricted capabilities intentionally prevent a nested bwrap namespace.
        # Native Agentnode keeps Codex's workspace sandbox.
        sandbox = 'danger-full-access' if container_sandbox else 'workspace-write'
        cmd += ['-c', 'approval_policy="never"', '-c', 'sandbox_mode=' + toml(sandbox)]
    if output:
        cmd += ['-o', str(output)]
    for path in images:
        cmd += ['-i', str(path)]
    if session_id:
        cmd += [session_id]
    return cmd + ['-']


def error_text(value):
    """Unwrap structured CLI/provider failures without rendering Python/JSON wrappers."""
    for _ in range(8):
        if isinstance(value, dict):
            value = value.get('message') or value.get('error')
        elif isinstance(value, str):
            try:
                nested = json.loads(value)
            except ValueError:
                return value[:2000]
            if isinstance(nested, (dict, str)):
                value = nested
            else:
                break
        else:
            break
    return 'Codex could not complete this request. Check the machine connection and try again.'


def item_events(item, completed):
    """Translate tool lifecycle without copying base64 tool images into UI history."""
    kind, iid = item.get('type'), item.get('id', 'codex-tool')
    if kind == 'agent_message' and completed:
        return [{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': item.get('text', '')}]}}]
    if kind not in ('mcp_tool_call', 'command_execution', 'file_change', 'web_search'):
        return []
    name = ('mcp__' + item.get('server', '') + '__' + item.get('tool', '')) if kind == 'mcp_tool_call' else 'codex__' + kind
    if not completed:
        args = {'command': item.get('command')} if kind == 'command_execution' else item.get('arguments', {})
        return [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': iid, 'name': name, 'input': args}]}}]
    result = item.get('result') or {}
    content = result.get('content', []) if isinstance(result, dict) else []
    texts = [str(c.get('text', '')) for c in content if c.get('type') == 'text']
    summary = '\n'.join(texts) or item.get('aggregated_output') or item.get('status', 'completed')
    if item.get('error'):
        summary = str(item['error'])
    return [{'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': iid,
            'content': summary[:24000], 'is_error': bool(item.get('error') or item.get('status') == 'failed')}]}}]


class Bridge:
    def __init__(self, args):
        self.args = args
        self.session_id = args.resume
        self.child = None
        self.thread = None
        self.lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.cancelled = threading.Event()

    def emit(self, value):
        with self.output_lock:
            print(json.dumps(value), flush=True)

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            child = self.child
        if child and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            except ProcessLookupError:
                pass

    def run_turn(self, msg):
        started = time.monotonic()
        final, failure, completed = '', None, False
        try:
            with tempfile.TemporaryDirectory(prefix='agentnode-codex-') as temp:
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
                output = Path(temp) / 'final.txt'
                cmd = command(self.args.binary, self.args.model, self.args.mcp_config, self.args.primer,
                              self.session_id, images, output, self.args.yolo, self.args.container_sandbox)
                with self.lock:
                    if self.cancelled.is_set():
                        raise RuntimeError('Turn interrupted before launch')
                    child = self.child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=sys.stderr, start_new_session=True)
                try:
                    child.stdin.write('\n\n'.join(texts).encode())
                    child.stdin.close()
                    for raw in child.stdout:
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        typ = event.get('type')
                        if typ == 'thread.started':
                            self.session_id = event['thread_id']
                            self.emit({'type': 'system', 'subtype': 'init', 'session_id': self.session_id,
                                       'model': self.args.model, 'mcp_servers': [], 'tools': []})
                        elif typ in ('item.started', 'item.completed'):
                            for ev in item_events(event.get('item', {}), typ == 'item.completed'):
                                self.emit(ev)
                        elif typ == 'turn.completed':
                            completed = True
                        elif typ in ('turn.failed', 'error'):
                            failure = error_text(event.get('error') or event.get('message') or 'Codex turn failed')
                    rc = child.wait()
                    if output.exists():
                        final = output.read_text()
                    if rc != 0 or not completed:
                        failure = failure or f'Codex exited ({rc}) without completing the turn; inspect before retrying.'
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
            failure = 'Codex turn interrupted; inspect any partial work before retrying.'
        self.emit({'type': 'result', 'subtype': 'error' if failure else 'success', 'is_error': bool(failure),
                   'result': failure or final, 'duration_ms': int((time.monotonic() - started) * 1000),
                   'num_turns': 1, 'total_cost_usd': None})

    def main(self):
        def shutdown(signum, frame):
            self.cancel()
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        try:
            for raw in sys.stdin:
                msg = json.loads(raw)
                if msg.get('type') == 'control_request':
                    if msg.get('request', {}).get('subtype') == 'interrupt':
                        self.cancel()
                    continue
                if msg.get('type') != 'user':
                    continue
                if self.thread:
                    self.thread.join()
                self.cancelled.clear()
                self.thread = threading.Thread(target=self.run_turn, args=(msg,), daemon=True)
                self.thread.start()
        finally:
            self.cancel()
            if self.thread:
                self.thread.join(timeout=6)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--binary', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--mcp-config', required=True)
    p.add_argument('--primer', required=True)
    p.add_argument('--resume')
    p.add_argument('--yolo', action='store_true')
    p.add_argument('--container-sandbox', action='store_true')
    Bridge(p.parse_args()).main()


if __name__ == '__main__':
    main()
