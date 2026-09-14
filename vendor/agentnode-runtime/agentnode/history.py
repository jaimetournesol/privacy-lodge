"""Byte-cursor transcript pages. Archives remain authoritative and unmodified."""
import copy
import hashlib
import json
from pathlib import Path

PAGE_BYTES = 192 * 1024


def revision(path):
    return hashlib.sha256(str(path).encode()).hexdigest()[:24]


def cursor(path, offset):
    return f'{revision(path)}:{offset}'


def offset(path, value, size):
    try:
        rev, pos = value.split(':')
        pos = int(pos)
        if rev != revision(path) or not 0 <= pos <= size:
            raise ValueError()
        if pos:
            with path.open('rb') as f:
                f.seek(pos - 1)
                if f.read(1) != b'\n': raise ValueError()
        return pos
    except (ValueError, AttributeError):
        raise ValueError('History changed; reload the recent page') from None


def preview(event, key):
    """Heavy details are loaded explicitly, never silently deleted from storage."""
    raw = json.dumps(event)
    if len(raw.encode()) <= 12000:
        return event
    event = copy.deepcopy(event)
    def shrink(value):
        if isinstance(value, str): return value[:2500] + ('…' if len(value) > 2500 else '')
        if isinstance(value, list): return [shrink(x) for x in value[:30]]
        if isinstance(value, dict):
            if value.get('type') == 'image': return {'type':'text','text':'[Image available in full event]'}
            return {k:shrink(v) for k,v in value.items() if k not in ('image','image_url','imageDataUrl')}
        return value
    result = shrink(event)
    result['detail_cursor'] = key
    if len(json.dumps(result).encode())>12000:
        result={'type':'raw','event':{'summary':'Large event — open full details'},'detail_cursor':key}
    return result


def page(filename, before=None, after=None, limit=40, budget=PAGE_BYTES, through=None):
    path = Path(filename)
    size = path.stat().st_size if path.exists() else 0
    limit = max(1, min(int(limit), 100))
    # Do not acknowledge a record until its terminating newline is on disk.
    complete = size
    if size:
        with path.open('rb') as f:
            while complete:
                start_chunk=max(0,complete-65536);f.seek(start_chunk)
                chunk=f.read(complete-start_chunk);at=chunk.rfind(b'\n')
                if at>=0:complete=start_chunk+at+1;break
                complete=start_chunk
                if size-complete>32*1024*1024:raise ValueError('Incomplete history record exceeds the read limit')
    end = offset(path, before, size) if before else complete
    if through:end=min(end,offset(path,through,size))
    start = offset(path, after, size) if after else 0
    if before and after: raise ValueError('Use before or after, not both')
    entries = []
    used = 0
    with path.open('rb') if size else __import__('io').BytesIO() as f:
        if after:
            f.seek(start);next_offset=start
            while f.tell() < end and len(entries) < limit:
                pos = f.tell(); raw = f.readline(); nxt = f.tell()
                if not raw.endswith(b'\n'): break
                try: ev = json.loads(raw)
                except ValueError: next_offset=nxt;continue
                if not isinstance(ev,dict):next_offset=nxt;continue
                key = cursor(path,pos); ev = preview(ev,key)
                ev.update(event_id=key,cursor=cursor(path,nxt))
                n = len(json.dumps(ev).encode())
                if entries and used+n > budget: break
                entries.append(ev); used += n;next_offset=nxt
            return dict(events=entries,revision=revision(path),cursor=cursor(path,next_offset),more=next_offset<end,older=None,through=cursor(path,end))
        # Scan backwards by chunks; no full-file parse or offset index rebuild.
        pos = end; tail = b''; lines = []
        while pos > 0 and len(lines) <= limit:
            amount = min(65536,pos); pos -= amount; f.seek(pos)
            tail = f.read(amount)+tail
            lines = tail.splitlines(keepends=True)
            if len(tail) > 32*1024*1024: break
        if pos: lines = lines[1:]; pos = end-sum(map(len,lines))
        offsets=[]
        for raw in lines:
            offsets.append((pos,raw));pos+=len(raw)
        scan_start=offsets[0][0] if offsets else 0
        oldest=end
        for pos,raw in reversed(offsets):
            if not raw.endswith(b'\n'): continue
            try: ev=json.loads(raw)
            except ValueError: continue
            if not isinstance(ev,dict):continue
            key=cursor(path,pos);ev=preview(ev,key)
            ev.update(event_id=key,cursor=cursor(path,pos+len(raw)))
            n=len(json.dumps(ev).encode())
            if entries and (len(entries)>=limit or used+n>budget):break
            entries.append(ev);used+=n;oldest=pos
    entries.reverse()
    return dict(events=entries,revision=revision(path),cursor=cursor(path,end),older=cursor(path,oldest if entries else scan_start) if (oldest if entries else scan_start) else None,more=False)


def detail(filename, value):
    path=Path(filename);pos=offset(path,value,path.stat().st_size)
    with path.open('rb') as f:
        f.seek(pos);raw=f.readline(32*1024*1024)
    if not raw.endswith(b'\n'):raise ValueError('Event is too large or unavailable')
    ev=json.loads(raw);ev.update(event_id=value,cursor=cursor(path,pos+len(raw)))
    return ev
