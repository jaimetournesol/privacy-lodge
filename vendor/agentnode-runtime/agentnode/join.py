"""Pairing codes and the worker side of joining a control node.

A code carries the control node's HTTPS origin, the SHA-256 fingerprint of the
certificate it serves and a single-use secret. The worker pins that fingerprint
for the join exchange, sends a certificate request plus its own node token,
receives a fleet-CA-signed certificate, installs it and asks the control node to
confirm it can now reach the worker over verified TLS. Only then is the machine
recorded in nodes.json and the code consumed.
"""
import base64
import hashlib
import hmac
import http.client
import json
import secrets
import socket
import ssl
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from . import config
from .storage import read_json, write_json

PREFIX = 'AGN1.'
DEFAULT_TTL = 15 * 60
MAX_TTL = 24 * 3600


def invites_file() -> Path:
    return config.HOME / 'invites.json'


def load_invites() -> dict:
    now = time.time()
    return {k: v for k, v in read_json(invites_file(), {}).items() if isinstance(v, dict) and v.get('exp', 0) > now}


def save_invites(items: dict):
    write_json(invites_file(), items)


def guess_local_ip(target_host='8.8.8.8', target_port=80) -> str:
    """The interface address used to reach a host; the best default for 'reach me here'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_host, target_port))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def control_url(node, override=None) -> str:
    if override:
        return validate_origin(override)
    if node.get('control_url_self'):
        return validate_origin(node['control_url_self'])
    return f"https://{guess_local_ip()}:{node['tls_port']}"


def validate_origin(value) -> str:
    from .transport import origin
    url = origin({'url': str(value)})
    if not url.startswith('https://'):
        raise ValueError('Fleet joins require an https:// origin')
    return url


def create_invite(node, url=None, ttl=DEFAULT_TTL, note='') -> dict:
    """Mint a single-use code for one machine; the secret is stored only as a hash."""
    from .fleet_ca import fingerprint
    if not node.get('control'):
        raise ValueError('Only a control node can invite machines')
    ttl = int(ttl)
    if not 60 <= ttl <= MAX_TTL:
        raise ValueError('Invite lifetime must be between 60 seconds and 24 hours')
    cert = config.TLS / 'cert.pem'
    if not cert.is_file():
        raise ValueError('Generate this node\'s TLS certificate first (python -m agentnode tls)')
    target = control_url(node, url)
    invite_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    record = {'id': invite_id, 'hash': hashlib.sha256(secret.encode()).hexdigest(), 'exp': int(time.time()) + ttl,
              'created': int(time.time()), 'note': str(note or '')[:120], 'url': target}
    items = load_invites()
    items[invite_id] = record
    save_invites(items)
    payload = json.dumps({'u': target, 'f': fingerprint(cert), 'i': invite_id, 's': secret}, separators=(',', ':')).encode()
    code = PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip('=')
    return {'code': code, 'id': invite_id, 'expires': record['exp'], 'url': target}


def decode_code(code) -> dict:
    if not isinstance(code, str) or not code.startswith(PREFIX) or len(code) > 2048:
        raise ValueError('That is not an AgentNode pairing code')
    body = code[len(PREFIX):].strip()
    try:
        data = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        assert isinstance(data, dict)
        url, fp, invite_id, secret = data['u'], data['f'], data['i'], data['s']
        assert all(isinstance(v, str) and v for v in (url, fp, invite_id, secret)) and len(fp) == 64
    except (ValueError, AssertionError, KeyError, TypeError):
        raise ValueError('Pairing code is malformed') from None
    return {'url': validate_origin(url), 'fingerprint': fp.lower(), 'id': invite_id, 'secret': secret}


def take_invite(invite_id, secret, consume=False) -> dict:
    items = load_invites()
    record = items.get(invite_id) if isinstance(invite_id, str) else None
    if not record or not isinstance(secret, str) or not hmac.compare_digest(record['hash'], hashlib.sha256(secret.encode()).hexdigest()):
        raise ValueError('Invalid or expired pairing code')
    if consume:
        items.pop(invite_id)
    save_invites(items)
    return record


def update_invite(record: dict):
    items = load_invites()
    if record['id'] in items:
        items[record['id']] = record
        save_invites(items)


class PinnedControl:
    """HTTPS to the control node trusting exactly the certificate fingerprint from the code."""

    def __init__(self, url, fingerprint, timeout=15):
        parsed = urlsplit(url)
        self.host, self.port, self.fingerprint, self.timeout = parsed.hostname, parsed.port or 443, fingerprint, timeout

    def request(self, method, path, body=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)
        try:
            conn.connect()
            seen = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(seen, self.fingerprint):
                raise ValueError('The control node presented a different certificate than the pairing code expects; ask for a new code')
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
            response = conn.getresponse()
            raw = response.read(1024 * 1024)
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {'error': raw.decode('utf-8', 'replace')[:200]}
            if response.status >= 400:
                raise ValueError(str(payload.get('error') or payload.get('detail') or f'HTTP {response.status}'))
            return payload
        finally:
            conn.close()


def advertised_url(node, control_host, override=None) -> str:
    if override:
        return validate_origin(override)
    parsed = urlsplit(control_host)
    return f"https://{guess_local_ip(parsed.hostname, parsed.port or 443)}:{node['tls_port']}"


def services_installed() -> bool:
    if config.is_mac():
        return (Path.home() / 'Library/LaunchAgents/com.agentnode.plist').is_file()
    return (Path.home() / '.config/systemd/user/agentnode.service').is_file()


def restart_services():
    from .setup import restart_services as restart
    restart(config.node())


def wait_local_https(node, timeout=25) -> bool:
    """The restarted service must serve the new certificate before the control node can verify it."""
    deadline = time.time() + timeout
    context = ssl.create_default_context(cafile=str(config.TLS / 'cert.pem'))
    context.check_hostname = False
    while time.time() < deadline:
        try:
            conn = http.client.HTTPSConnection('127.0.0.1', node['tls_port'], timeout=3, context=context)
            conn.request('GET', '/healthz')
            if conn.getresponse().status == 200:
                return True
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(1)
    return False


def worker_join(code, name=None, advertise=None, confirm=True, restart=True, out=print) -> dict:
    """Issue and install a fleet certificate, then let the control node verify the connection."""
    from . import fleet_ca
    from .backends import BACKENDS, binary_for
    import shutil
    data = decode_code(code)
    n = config.node()
    if n.get('control'):
        raise ValueError('This node is a control node; join is for workers')
    name = fleet_ca.validate_name(name or n['name'])
    control = PinnedControl(data['url'], data['fingerprint'])
    record = None
    if not (confirm and n.get('join_pending') == data['id'] and (config.TLS / 'cert.pem').is_file()):
        url = advertised_url(n, data['url'], advertise)
        host = urlsplit(url).hostname
        sans = [host, name]
        with tempfile.TemporaryDirectory(prefix='agentnode-join-') as temp:
            key, csr = fleet_ca.new_key_and_csr(name, sans, temp)
            info = {'backend': n.get('backend', 'claude'),
                    'clis': {b: bool(shutil.which(binary_for(b, n))) for b in BACKENDS},
                    'surface': bool(n.get('surface_dir')), 'host_tools': bool(n.get('host_tools', True)),
                    'hub_tls_port': n['hub_tls_port'] if n.get('surface_dir') else None}
            record = control.request('POST', '/api/control/join', {'id': data['id'], 'secret': data['secret'], 'name': name,
                                                                    'url': url, 'token': n['token'], 'csr': csr, 'sans': sans, 'info': info})
            backup = fleet_ca.install_tls(record['cert'], key)
        fleet = config.HOME / 'fleet-ca.crt'
        fleet.write_text(record['ca'])
        fleet.chmod(0o600)
        n.update(name=name, fleet_ca=str(fleet), control_url=data['url'], control_name=record.get('control', {}).get('name'),
                 advertise_url=url, join_pending=data['id'])
        config.save_node(n)
        out(f"Issued a fleet certificate for {name} ({', '.join(sans)}); previous TLS pair " + (f'backed up in {backup}' if backup else 'not present'))
        if restart and services_installed():
            out('Restarting services so the new certificate is served')
            restart_services()
    if not confirm:
        out('Certificate installed. Start the service, then run the same join command with --confirm')
        return {'ok': True, 'pending': True}
    if not wait_local_https(n, timeout=25 if restart else 5):
        raise ValueError(f"AgentNode is not serving https on port {n['tls_port']} yet; start it (./run.sh or --services) and rerun with --confirm")
    result = control.request('POST', '/api/control/join/confirm', {'id': data['id'], 'secret': data['secret']})
    n = config.node()
    n.pop('join_pending', None)
    n['joined'] = int(time.time())
    config.save_node(n)
    out(f"Joined {result.get('control', {}).get('name') or data['url']} as {name}; the control node reaches this machine at {result.get('node', {}).get('url')}")
    return result
