"""Fleet connections share certificate trust and never follow redirects with credentials."""
import ssl
import hashlib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import requests
from websockets import connect
from . import config


def origin(node):
    value=node['url'].rstrip('/')
    parsed=urlsplit(value)
    if (parsed.scheme not in ('http','https') or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path or parsed.query or parsed.fragment
        or any(c.isspace() for c in value) or (parsed.port is not None and not 0 < parsed.port < 65536)):
        raise ValueError('Use an HTTP or HTTPS machine origin without a path or credentials.')
    return urlunsplit((parsed.scheme,parsed.netloc,'','',''))


def ca_file(node):
    if node.get('ca'):
        pem = node['ca']
        if not isinstance(pem, str) or len(pem) > 16384:
            raise ValueError('Invalid machine certificate')
        ssl.create_default_context(cadata=pem)  # Validate before persisting.
        directory = config.HOME / 'trusted-machine-certificates'
        from .storage import private_dir
        private_dir(directory)
        target = directory / (hashlib.sha256(pem.encode()).hexdigest() + '.pem')
        if not target.exists():
            target.write_text(pem)
            target.chmod(0o600)
        return str(target)
    configured=node.get('ca_file') or config.node().get('fleet_ca')
    if configured: return str(Path(configured).expanduser())
    fleet_ca=config.HOME/'ca'/'ca.crt'
    return str(fleet_ca) if fleet_ca.is_file() else None


def request(node, method, path, body=None, timeout=8):
    url=origin(node)+path
    with requests.Session() as session:
        session.trust_env=False
        response=session.request(method,url,json=body,headers={'X-Agentnode-Token':node.get('token','')},
                                 timeout=timeout,verify=ca_file(node) or True,allow_redirects=False)
        if 300 <= response.status_code < 400:
            response.close()
            raise ValueError('Fleet redirects are not allowed. Configure the final machine address.')
        return response


class DirectConnect(connect):
    def process_redirect(self, exc):
        return exc


def websocket(node, path, **kwargs):
    base=origin(node)
    if base.startswith('https:'):
        certificate=ca_file(node)
        context=ssl.create_default_context(cafile=certificate)
        kwargs['ssl']=context
    return DirectConnect('ws'+base[4:]+path,additional_headers={'X-Agentnode-Token':node.get('token','')},
                         proxy=None,**kwargs)
