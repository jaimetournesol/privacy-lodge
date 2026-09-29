#!/usr/bin/env python3
"""Two disposable fleets: setup, authenticated Surfaces, peer isolation and persistence.

No host directories or existing volumes are mounted. Cleanup addresses only the
unique Compose project created by this invocation. Never prints runtime secrets.
"""
import argparse
import base64
import json
import secrets
import subprocess
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--box-image', required=True)
    parser.add_argument('--agent-image', required=True)
    parser.add_argument('--federation', action='store_true', help='Provision synthetic accounts and verify messages across two onion boxes')
    args = parser.parse_args()
    project = 'pl-ci-' + secrets.token_hex(8)
    services, volumes = {}, {}
    for fleet in ('a', 'b'):
        for role in ('box', 'handoff', 'control', 'worker', 'peer'):
            volumes[f'{fleet}-{role}'] = {}
        services[f'{fleet}-box'] = {
            'image': args.box_image, 'environment': {'PL_BOX': 'ci-'+fleet,
            'PL_SECRETS_KEY': base64.b64encode(secrets.token_bytes(32)).decode()},
            'volumes': [f'{fleet}-box:/data', f'{fleet}-handoff:/handoff'],
            'stop_grace_period': '60s'}
        for role in ('control', 'worker'):
            services[f'{fleet}-{role}'] = {
                'image': args.agent_image, 'environment': {'LODGE_ROLE': role},
                'security_opt': ['no-new-privileges:true'], 'cap_drop': ['ALL'],
                'cap_add': ['CHOWN', 'SETUID', 'SETGID'], 'pids_limit': 256,
                'volumes': [f'{fleet}-{role}:/data', f'{fleet}-peer:/peer' + (':ro' if role == 'control' else '')]}
        services[f'{fleet}-worker']['networks'] = {fleet: {'aliases': ['agent-worker']}}
        services[f'{fleet}-box']['networks'] = [fleet]
        services[f'{fleet}-control'].update(network_mode=f'service:{fleet}-box',
            depends_on=[f'{fleet}-box', f'{fleet}-worker'])
        services[f'{fleet}-control']['volumes'].append(f'{fleet}-handoff:/handoff')
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        config = Path(directory)/'compose.json'
        config.write_text(json.dumps({'services': services, 'volumes': volumes, 'networks': {'a': {}, 'b': {}}}))
        config.chmod(0o600)
        command = ['docker', 'compose', '-p', project, '-f', str(config)]
        def compose(*tail, **kw):
            return subprocess.run(command+list(tail), check=True, capture_output=True, text=True, **kw)
        probe = r'''
import json,os,pathlib,urllib.request,urllib.error
n=json.loads((pathlib.Path(os.environ['AGENTNODE_HOME'])/'node.json').read_text())
def get(path, token=None):
 req=urllib.request.Request('http://127.0.0.1:'+str(n['port'])+path,headers={'X-Agentnode-Token':token} if token else {})
 try:
  with urllib.request.urlopen(req,timeout=5) as r:return r.status,json.load(r)
 except urllib.error.HTTPError as e:return e.code,{}
assert get('/api/surface/workspaces')[0] in (401,403)
assert get('/api/surface/workspaces', 'invalid-ci-token')[0] in (401,403)
assert get('/healthz',n['token'])[1]['ok']
assert get('/api/surface/workspaces',n['token'])[1]['ok']
if os.environ['LODGE_ROLE']=='control':
 nodes=get('/api/control/tree',n['token'])[1]['nodes']
 assert sum(x.get('name')=='worker' and x.get('reachable',False) for x in nodes)==1
 with urllib.request.urlopen('http://127.0.0.1:8470/status',timeout=5) as r:assert not json.load(r).get('onion')
# A hash verifies persistent, distinct credentials without exposing them.
import hashlib
print(hashlib.sha256(n['token'].encode()).hexdigest())
'''
        def ready():
            deadline = time.monotonic()+180
            while time.monotonic()<deadline:
                try:
                    fingerprints = [compose('exec','-T','--user','1000:1000',f'{f}-{r}',
                        'python3','-',input=probe).stdout.strip() for f in ('a','b') for r in ('control','worker')]
                    assert len(set(fingerprints)) == 4, 'Runtime credentials must be independent'
                    return fingerprints
                except (subprocess.CalledProcessError, AssertionError):
                    time.sleep(3)
            raise RuntimeError('Disposable runtime acceptance timed out; no private logs exported')
        try:
            compose('up','-d')
            before = ready()
            compose('restart')
            assert ready() == before, 'Runtime credentials changed across restart'
            if args.federation:
                federation(compose)
            print('PASS: two isolated fleets; four independent credentials; unauthorized Surface access rejected; authenticated APIs/Surfaces; worker connectivity; unprovisioned setup; restart persistence')
        finally:
            compose('down','--volumes','--remove-orphans')


def federation(compose):
    """Exercise actual Tor federation, with generated accounts and synthetic text only."""
    import urllib.parse
    rpc = r'''
import json,sys,urllib.request
x=json.load(sys.stdin)
headers=x.get('headers',{})
body=x.get('body')
if isinstance(body,dict):
 body=json.dumps(body).encode();headers['Content-Type']='application/json'
elif body is not None:body=body.encode()
req=urllib.request.Request('http://127.0.0.1:'+str(x.get('port',8118))+x['path'],data=body,headers=headers,method=x.get('method','GET'))
with urllib.request.urlopen(req,timeout=90) as r:
 data=r.read().decode();print(json.dumps({'data':data}))
'''
    def request(fleet, path, body=None, method='GET', token=None, port=8118, headers=None):
        h=dict(headers or {})
        if token:h['Authorization']='Bearer '+token
        wire={'path':path,'body':body,'method':method,'port':port,'headers':h}
        out=compose('exec','-T',fleet+'-control','python3','-c',rpc,input=json.dumps(wire)).stdout
        value=json.loads(out)['data']
        try:return json.loads(value)
        except ValueError:return value
    def until(fn, seconds=480):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:return fn()
            except (subprocess.CalledProcessError, AssertionError, KeyError):time.sleep(5)
        raise RuntimeError('Synthetic Tor federation timed out')
    passwords={f:secrets.token_urlsafe(24) for f in ('a','b')}
    import re
    for f in passwords:
        page=request(f,'/',port=8470)
        nonce=re.search(r"'X-Lodge-Setup':'([a-f0-9]{64})'",page).group(1)
        request(f,'/provision',urllib.parse.urlencode({'username':'ci'+f,'password':passwords[f],'box_name':'CI '+f}),
            'POST',port=8470,headers={'X-Lodge-Setup':nonce,'Content-Type':'application/x-www-form-urlencoded'})
    sessions={f:until(lambda f=f:request(f,'/_matrix/client/v3/login',{'type':'m.login.password','user':'ci'+f,
        'password':passwords[f],'device_id':'ISOLATED_CI'},'POST')) for f in passwords}
    quote=lambda s:urllib.parse.quote(s,safe='')
    for f,peer in [('a','b'),('b','a')]:
        request(f,'/_matrix/client/v3/user/'+quote(sessions[f]['user_id'])+'/account_data/ai.tournesol.privacylodge.pairings',
            {'onions':[sessions[peer]['user_id'].split(':',1)[1]]},'PUT',sessions[f]['access_token'])
    # The ordinary box reconciler installs the consent recorded through Matrix account data.
    time.sleep(20)
    room=until(lambda:request('a','/_matrix/client/v3/createRoom',{'preset':'private_chat'},'POST',sessions['a']['access_token']))['room_id']
    until(lambda:request('a','/_matrix/client/v3/rooms/'+quote(room)+'/invite',{'user_id':sessions['b']['user_id']},'POST',sessions['a']['access_token']))
    until(lambda:request('b','/_matrix/client/v3/join/'+quote(room),{},'POST',sessions['b']['access_token']))
    for sender,receiver in [('a','b'),('b','a')]:
        marker='Synthetic CI delivery '+secrets.token_hex(8)
        request(sender,'/_matrix/client/v3/rooms/'+quote(room)+'/send/m.room.message/'+secrets.token_hex(8),
            {'msgtype':'m.text','body':marker},'PUT',sessions[sender]['access_token'])
        def delivered():
            data=request(receiver,'/_matrix/client/v3/rooms/'+quote(room)+'/messages?dir=b&limit=20',token=sessions[receiver]['access_token'])
            assert any(e.get('content',{}).get('body')==marker for e in data['chunk'])
        until(delivered,180)
    print('PASS: synthetic two-way messages across independently provisioned onion boxes (transport acceptance, not Android E2EE/call acceptance)')

if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('FAIL: isolated runtime test (details suppressed to protect generated credentials)') from None
