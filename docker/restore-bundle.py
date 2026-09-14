#!/usr/bin/env python3
"""Validate completely, restore into fresh Docker volumes, then atomically select them."""
import base64
import io
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile

MEMBERS = {'box.tgz':'', 'agent-data.tgz':'-legacy-agents', 'agent-handoff.tgz':'-handoff',
           'conductor.tgz':'-conductor', 'worker.tgz':'-worker', 'agent-peer.tgz':'-agent-peer'}
MAX_TOTAL = 1024**4


def validate(archive):
    """Validate data and link resolution before extracting anything into a volume."""
    from collections import deque
    total = 0
    members = {}
    for member in archive:
        path = PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or '\\' in member.name:
            raise ValueError('Archive contains an unsafe path')
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError('Archive contains a special file')
        if member.size < 0 or member.size > MAX_TOTAL - total or len(members) >= 1_000_000:
            raise ValueError('Archive exceeds restore limits')
        key = str(path)
        if key in members and not (member.isdir() and members[key].isdir()):
            raise ValueError('Duplicate archive member')
        members[key] = member
        total += member.size
        if member.isfile():
            stream = archive.extractfile(member)
            left = member.size
            while left:
                data = stream.read(min(left, 1024*1024))
                if not data: raise ValueError('Archive is truncated')
                left -= len(data)
    links = {name:member for name,member in members.items() if member.issym() or member.islnk()}
    # Tar extraction must never write through a link created earlier in the archive.
    for name in members:
        if any(str(parent) in links for parent in PurePosixPath(name).parents):
            raise ValueError('Archive writes through a link')
    def resolve(name, link):
        todo = deque(PurePosixPath(link.linkname).parts)
        if link.linkname.startswith('/') or '\\' in link.linkname:
            raise ValueError('Archive link points outside its volume')
        stack = list(PurePosixPath(name).parent.parts) if link.issym() else []
        steps = 0
        while todo:
            item = todo.popleft()
            if item in ('', '.'): continue
            if item == '..':
                if not stack: raise ValueError('Archive link escapes its volume')
                stack.pop(); continue
            stack.append(item)
            key = '/'.join(stack)
            target = links.get(key)
            if target:
                steps += 1
                if steps > 40: raise ValueError('Archive contains a link cycle')
                if target.linkname.startswith('/') or '\\' in target.linkname:
                    raise ValueError('Archive link points outside its volume')
                stack = stack[:-1] if target.issym() else []
                todo.extendleft(reversed(PurePosixPath(target.linkname).parts))
        return '/'.join(stack)
    for name,link in links.items():
        target = resolve(name,link)
        if link.islnk() and (target not in members or not members[target].isfile()):
            raise ValueError('Archive hard link has no regular-file target')
    return set(members)


def docker(*args, capture=False):
    result = subprocess.run(['docker',*args], check=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    return result.stdout.decode().strip() if capture else ''


def prepare(path, stage):
    with tarfile.open(path, 'r:gz') as outer:
        names = validate(outer)
        if 'MANIFEST' not in names:
            regular = {str(PurePosixPath(m.name)) for m in outer.getmembers() if m.isfile()}
            if not {'box.json','secrets.json'}.issubset(regular): raise ValueError('Not a box backup')
            return {}, {'box.tgz':path}
        if any(not (m.isfile() or m.isdir()) for m in outer.getmembers()): raise ValueError('Bundle wrappers cannot contain links')
        if not names.issubset({'MANIFEST','.',*MEMBERS}): raise ValueError('Unexpected bundle member')
        manifest_member = next(m for m in outer.getmembers() if str(PurePosixPath(m.name))=='MANIFEST')
        if manifest_member.size > 16384: raise ValueError('Manifest is too large')
        manifest = dict(line.split('=',1) for line in outer.extractfile(manifest_member).read().decode().splitlines() if '=' in line)
        if manifest.get('pp_backup_format') not in ('2','3'): raise ValueError('Unsupported backup format')
        if len(base64.b64decode(manifest.get('secrets_key',''),validate=True))!=32: raise ValueError('Backup has no valid storage key')
        files = {}
        for member in outer.getmembers():
            name = str(PurePosixPath(member.name))
            if name not in MEMBERS: continue
            target = stage / name
            with target.open('xb') as out: shutil.copyfileobj(outer.extractfile(member),out,1024*1024)
            target.chmod(0o600)
            with tarfile.open(target,'r:gz') as inner:
                contents=validate(inner)
                if name=='box.tgz' and not {'box.json','secrets.json'}.issubset({str(PurePosixPath(m.name)) for m in inner.getmembers() if m.isfile()}): raise ValueError('Box identity is missing')
            files[name]=target
        if 'box.tgz' not in files: raise ValueError('Backup contains no box')
        return manifest, files


def restore(path, env_path):
    os.umask(0o077)
    previous=env_path.read_text()
    root='privacy-lodge-restored-'+secrets.token_hex(6)
    created=[]
    activated=False
    with tempfile.TemporaryDirectory(prefix='lodge-restore-') as temp:
        manifest, files=prepare(path,Path(temp))
        print('Validated backup. Restore uses fresh volumes; your current volumes will be kept.')
        if input('Select the restored box after extraction? [y/N] ').lower()!='y': return
        try:
            for member,file in files.items():
                volume=root+MEMBERS[member]
                docker('volume','create',volume);created.append(volume)
                # Exact argv; file names never become shell programs. All members were
                # validated before creating any volume or changing the selected identity.
                docker('run','--rm','-v',f'{volume}:/restore','-v',f'{file.resolve()}:/backup.tgz:ro',
                       'alpine','tar','xzf','/backup.tgz','-C','/restore')
            values={'PL_VOLUME':root,'PL_AGENT_HANDOFF_VOLUME':root+'-handoff',
                    'PL_AGENT_VOLUME':root+'-legacy-agents','PL_AGENTS':'1' if 'conductor.tgz' in files else '0'}
            if manifest.get('secrets_key'): values['PL_SECRETS_KEY']=manifest['secrets_key']
            lines=[]
            for line in previous.splitlines():
                key=line.split('=',1)[0]
                if key in values: lines.append(key+'='+values.pop(key))
                else: lines.append(line)
            lines += [key+'='+value for key,value in values.items()]
            backup=env_path.with_name('.env.before-restore-'+secrets.token_hex(4))
            backup.write_text(previous);backup.chmod(0o600)
            temporary=env_path.with_name('.env.restoring')
            temporary.write_text('\n'.join(lines)+'\n');temporary.chmod(0o600)
            with temporary.open('rb') as stream: os.fsync(stream.fileno())
            os.replace(temporary,env_path)
            activated=True
            print('Restored and selected. Start with ./pl-box up. Previous volumes and configuration are preserved.')
        finally:
            if not activated:
                for volume in created:
                    subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

if __name__=='__main__':
    try: restore(Path(sys.argv[1]),Path(sys.argv[2]))
    except (ValueError,OSError,tarfile.TarError,subprocess.CalledProcessError) as error:
        # Do not print subprocess args or manifest fields (may hold credentials).
        print('Restore failed; the selected box and its original volumes were preserved. '+
              (str(error) if not isinstance(error,subprocess.CalledProcessError) else 'Docker could not complete extraction.'),file=sys.stderr)
        sys.exit(1)
