"""Bounded calls to a configured speech worker; credentials never enter URLs."""
from urllib.parse import urlsplit
import requests
from . import config


def configured():
    return bool(config.node().get('speech_worker_url'))


def request(path, audio=None, language=None, prompt=None):
    n=config.node();url=n.get('speech_worker_url','').rstrip('/')
    u=urlsplit(url)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
        raise RuntimeError('Configure a trusted HTTPS speech worker origin.')
    token=config.secrets_().get('SPEECH_WORKER_TOKEN')
    if not token:raise RuntimeError('Speech worker credential is missing.')
    ca=n.get('fleet_ca') or str(config.HOME/'ca/ca.crt')
    params={}
    if language:params['language']=language.split('-')[0].lower()
    if prompt:params['prompt']=prompt
    try:
        with requests.Session() as session:
            session.trust_env=False
            r=session.request('GET' if audio is None else 'POST',url+path,data=audio,params=params,
                headers={'Authorization':'Bearer '+token,'Content-Type':'audio/wav'},verify=ca,
                timeout=(3,15),allow_redirects=False)
            if r.status_code!=200:raise RuntimeError('Speech worker is unavailable or busy. Please retry.')
            return r.json()
    except requests.RequestException:
        raise RuntimeError('Speech worker is unreachable. Chat and Stage remain available.') from None
