"""Short-lived browser sessions, signed with the node credential."""
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import secrets
import time
import base64
import json

COOKIE = 'agentnode_session'

def capability(secret, audience, role, **scope):
    payload=base64.urlsafe_b64encode(json.dumps(dict(aud=audience,role=role,exp=int(time.time())+6*3600,**scope),separators=(',',':')).encode()).decode().rstrip('=')
    return payload+'.'+hmac.new(secret.encode(),payload.encode(),hashlib.sha256).hexdigest()


def claims(value, secret, audience):
    try:
        payload,sig=value.split('.')
        expected=hmac.new(secret.encode(),payload.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected):return None
        data=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        if data.get('aud')!=audience or not time.time()<data.get('exp',0)<=time.time()+6*3600+60:return None
        return data
    except (ValueError,TypeError,AttributeError):return None

def valid_token(supplied, secret):
    return isinstance(supplied,str) and bool(supplied) and hmac.compare_digest(supplied,secret)

def issue(secret, authority):
    payload = f'{int(time.time())+12*3600}.{secrets.token_hex(16)}'
    sig = hmac.new(secret.encode(),(authority+'|'+payload).encode(),hashlib.sha256).hexdigest()
    return payload+'.'+sig

def valid_cookie(headers, secret):
    try:
        cookies=SimpleCookie(); cookies.load(headers.get('cookie',''))
        value=cookies[COOKIE].value
        expiry,nonce,sig=value.split('.')
        if not time.time() < int(expiry) <= time.time()+12*3600+60: return False
        expected=hmac.new(secret.encode(),(headers.get('host','')+'|'+expiry+'.'+nonce).encode(),hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig,expected)
    except (KeyError,ValueError,CookieError): return False
