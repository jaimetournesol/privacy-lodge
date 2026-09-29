#!/usr/bin/env python3
"""Promote a verified tagged CI candidate, signing locally. Never stores keys in CI.

Default is read-only preflight. --publish explicitly promotes the draft release.
Run from a clean checkout of the release tag. Credentials use gh/docker's existing
local stores; signing keys use sign-release.sh's private local configuration.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REPO = 'jaimetournesol/privacy-lodge'


def run(*args, **kw):
    return subprocess.check_output(args, cwd=ROOT, text=True, **kw).strip()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('version');p.add_argument('--notes',type=Path,required=True)
    p.add_argument('--publish',action='store_true')
    a=p.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+',a.version):p.error('Expected stable major.minor.patch')
    notes=a.notes.resolve();assert notes.is_file(), 'Release notes missing'
    assert not run('git','status','--porcelain'), 'Use a clean release checkout'
    tag='v'+a.version;sha=run('git','rev-parse',tag+'^{commit}')
    assert sha==run('git','rev-parse','HEAD'), 'Checkout must match the release tag'
    release=json.loads(run('gh','api',f'repos/{REPO}/releases/tags/{tag}'))
    assert release['draft'], 'Refusing to overwrite an already published release'
    runs=json.loads(run('gh','api',f'repos/{REPO}/actions/workflows/release.yml/runs?head_sha={sha}&per_page=20'))['workflow_runs']
    assert any(r['head_sha']==sha and r['event']=='push' and r['conclusion']=='success' for r in runs), 'Tagged release workflow must pass'
    assert json.loads((ROOT/'package.json').read_text())['version']==a.version
    for role in ('box','agent'):
        source=f'ghcr.io/jaimetournesol/privacy-lodge-{role}:sha-{sha}'
        run('docker','manifest','inspect',source)
    print('PASS: clean matching tag, draft release, successful release CI, both immutable image candidates')
    if not a.publish:return
    with tempfile.TemporaryDirectory(prefix='lodge-release-') as d:
        out=Path(d)
        run('gh','release','download',tag,'--repo',REPO,'--dir',d,'--pattern','*.deb','--pattern','*.AppImage')
        assert len(list(out.glob('*.deb')))==1 and len(list(out.glob('*.AppImage')))==1
        # Signing happens before publication or image promotion; failure keeps the draft closed.
        import os
        run('bash','scripts/sign-release.sh',a.version,'--installer-only',str(notes),env={**os.environ,'PL_OUT':d})
        for role in ('box','agent'):
            source=f'ghcr.io/jaimetournesol/privacy-lodge-{role}:sha-{sha}'
            target=f'jaimemelon/privacy-lodge-{role}:{a.version}'
            run('docker','pull',source);run('docker','tag',source,target);run('docker','push',target)
            # Published manifest must identify the same image as the candidate.
            remote=json.loads(run('docker','manifest','inspect',target))
            original=json.loads(run('docker','manifest','inspect',source))
            assert remote==original, 'Registry manifests differ; retain draft'
        archive=out/f'privacy-lodge-{a.version}-docker.tar.gz'
        with tarfile.open(archive,'w:gz') as t:
            for name in ['docker-compose.yml','install.sh','pl-box','pl-box.ps1','agent-ui.py','restore-bundle.py','README.md']:
                t.add(ROOT/'docker'/name,arcname=f'privacy-lodge-{a.version}-docker/{name}')
        sums=out/'privacy-lodge-SHA256SUMS.txt'
        sums.write_text(''.join(hashlib.sha256(f.read_bytes()).hexdigest()+'  '+f.name+'\n' for f in sorted(out.iterdir()) if f.is_file()))
        assets=[str(f) for f in out.iterdir() if f.suffix not in ('.deb','.AppImage')]
        run('gh','release','upload',tag,'--repo',REPO,'--clobber',*assets)
        remote=json.loads(run('gh','api',f'repos/{REPO}/releases/tags/{tag}'))
        for asset in remote['assets']:
            local=out/asset['name'];assert local.is_file(), 'Unexpected draft asset'
            assert asset['digest']=='sha256:'+hashlib.sha256(local.read_bytes()).hexdigest(), 'Uploaded checksum mismatch'
        run('gh','release','edit',tag,'--repo',REPO,'--draft=false','--latest','--notes-file',str(notes))
        for role in ('box','agent'):
            run('docker','tag',f'jaimemelon/privacy-lodge-{role}:{a.version}',f'jaimemelon/privacy-lodge-{role}:latest')
            run('docker','push',f'jaimemelon/privacy-lodge-{role}:latest')
        print('Published verified installers, matching Docker images, helper archive, checksums and dual-signed manifest')

if __name__=='__main__':
    try:main()
    except Exception:
        raise SystemExit('Release promotion failed; inspect draft/image state before retrying. Private details suppressed.') from None
