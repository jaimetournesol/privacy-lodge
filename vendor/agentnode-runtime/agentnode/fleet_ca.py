"""Fleet certificate authority in Python, sharing the ~/.agentnode/ca layout with ca.sh.

The control node signs one server certificate per joining machine so browsers and
the control node trust every worker after trusting a single CA. OpenSSL does the
cryptography; this module only owns file layout, validation and backups.
"""
import hashlib
import ipaddress
import re
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from . import config
from .storage import private_dir

NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
HOST_RE = re.compile(r'[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*')


def _openssl(*args, cwd=None):
    try:
        return subprocess.run(['openssl', *args], check=True, capture_output=True, text=True, cwd=cwd).stdout
    except FileNotFoundError:
        raise ValueError('openssl is required for fleet certificates') from None
    except subprocess.CalledProcessError as exc:
        raise ValueError('openssl failed: ' + (exc.stderr or '').strip()[-300:]) from None


def ca_dir() -> Path:
    d = config.HOME / 'ca'
    private_dir(d)
    return d


def ca_paths():
    d = ca_dir()
    return d / 'ca.key', d / 'ca.crt'


def has_ca() -> bool:
    key, crt = ca_paths()
    return key.is_file() and crt.is_file()


def ensure_ca() -> Path:
    """Create the fleet CA once; never overwrite or repair half a CA silently."""
    key, crt = ca_paths()
    if key.is_file() and crt.is_file():
        return crt
    if key.exists() or crt.exists():
        raise ValueError(f'Incomplete fleet CA in {key.parent}; restore ca.key and ca.crt together')
    _openssl('genrsa', '-out', str(key), '4096')
    key.chmod(0o600)
    _openssl('req', '-x509', '-new', '-key', str(key), '-sha256', '-days', '3650',
             '-subj', '/CN=Conductor Fleet CA/O=agentnode',
             '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
             '-out', str(crt))
    return crt


def first_pem_block(text: str) -> str:
    match = re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', text, re.S)
    if not match:
        raise ValueError('No certificate found')
    return match.group(0) + '\n'


def fingerprint(cert_path) -> str:
    """SHA-256 of the leaf certificate's DER encoding, lower-case hex."""
    der = ssl.PEM_cert_to_DER_cert(first_pem_block(Path(cert_path).read_text()))
    return hashlib.sha256(der).hexdigest()


def validate_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError('Machine names use 1-64 letters, numbers, dots, underscores or dashes')
    return name


def validate_sans(values) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ValueError('Supply between one and sixteen certificate names or addresses')
    out = []
    for value in values:
        if not isinstance(value, str) or len(value) > 253:
            raise ValueError('Invalid certificate name')
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not HOST_RE.fullmatch(value):
                raise ValueError(f'Invalid certificate name: {value!r}') from None
        if value not in out:
            out.append(value)
    return out


def san_extension(sans) -> str:
    entries = []
    for value in [*validate_sans(list(sans)), 'localhost', '127.0.0.1']:
        try:
            ipaddress.ip_address(value)
            entry = 'IP:' + value
        except ValueError:
            entry = 'DNS:' + value
        if entry not in entries:
            entries.append(entry)
    return 'subjectAltName=' + ','.join(entries)


def new_key_and_csr(name, sans, directory) -> tuple[Path, str]:
    """A fresh private key and signing request for this machine; the key never leaves the host."""
    validate_name(name)
    key = Path(directory) / 'key.pem'
    csr = Path(directory) / 'request.csr'
    _openssl('genrsa', '-out', str(key), '2048')
    key.chmod(0o600)
    _openssl('req', '-new', '-key', str(key), '-subj', f'/CN={name}/O=agentnode', '-addext', san_extension(sans), '-out', str(csr))
    return key, csr.read_text()


def sign_csr(csr_pem, name, sans, days=825) -> str:
    """Sign a machine's request with the fleet CA; the CA decides the names, not the request."""
    validate_name(name)
    if not isinstance(csr_pem, str) or len(csr_pem) > 8192 or '-----BEGIN CERTIFICATE REQUEST-----' not in csr_pem:
        raise ValueError('Invalid certificate request')
    key, crt = ca_paths()
    ensure_ca()
    with tempfile.TemporaryDirectory(prefix='agentnode-ca-') as temp:
        work = Path(temp)
        (work / 'request.csr').write_text(csr_pem)
        (work / 'ext.cnf').write_text(san_extension(sans) + '\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\nbasicConstraints=CA:FALSE\n')
        _openssl('x509', '-req', '-in', str(work / 'request.csr'), '-CA', str(crt), '-CAkey', str(key), '-CAcreateserial',
                 '-days', str(int(days)), '-sha256', '-extfile', str(work / 'ext.cnf'), '-out', str(work / 'leaf.pem'))
        leaf = (work / 'leaf.pem').read_text()
    return leaf + crt.read_text()


def install_tls(cert_pem: str, key_path: Path) -> Path | None:
    """Replace this machine's serving certificate, keeping the previous pair in a dated backup."""
    cert, key = config.TLS / 'cert.pem', config.TLS / 'key.pem'
    backup = None
    if cert.exists() or key.exists():
        backup = config.TLS / ('backup-' + time.strftime('%Y%m%d-%H%M%S'))
        private_dir(backup)
        for path in (cert, key):
            if path.exists():
                shutil.move(str(path), str(backup / path.name))
    cert.write_text(cert_pem)
    shutil.copyfile(key_path, key)
    key.chmod(0o600)
    return backup


def issue_local(name, sans) -> Path | None:
    """Give this control node a fleet-CA-signed certificate (the Python form of `ca.sh local`)."""
    ensure_ca()
    with tempfile.TemporaryDirectory(prefix='agentnode-tls-') as temp:
        key, csr = new_key_and_csr(name, sans, temp)
        chain = sign_csr(csr, name, sans)
        return install_tls(chain, key)


def cert_sans(cert_path) -> list[str]:
    text = _openssl('x509', '-in', str(cert_path), '-noout', '-ext', 'subjectAltName')
    return [part.split(':', 1)[1].strip() for part in text.replace('\n', ',').split(',') if ':' in part and part.strip().split(':')[0].strip() in ('DNS', 'IP Address', 'IP')]


def verified_by(cert_path, ca_path) -> bool:
    try:
        _openssl('verify', '-CAfile', str(ca_path), str(cert_path))
        return True
    except ValueError:
        return False
