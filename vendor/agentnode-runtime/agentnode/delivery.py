"""Durable command receipts. A receipt identifies one agent and one turn."""
import time
import uuid
import hashlib
import json
from .storage import read_json, write_json, private_dir

class DeliveryError(ValueError):
    pass

class TurnLedger:
    MAX_RECENT = 256
    MAX_RECENT_BYTES = 8 * 1024 * 1024

    def __init__(self, path):
        self.path = path
        self.archive = path.parent / 'turn-archive'
        self.items = read_json(path, {})
        changed = False
        for item in self.items.values():
            if item['status'] == 'delivered':
                item.update(status='interrupted', result={'is_error':True, 'subtype':'interrupted',
                    'result':'Service restarted before this turn completed. Inspect the agent before retrying; it may have performed actions.'})
                changed = True
        self.save()

    def _archive_path(self, rid):
        return self.archive / (hashlib.sha256(rid.encode()).hexdigest()+'.json')

    def get(self, rid):
        if not isinstance(rid,str) or not 1 <= len(rid) <= 100:return None
        return self.items.get(rid) or read_json(self._archive_path(rid),{}) or None

    def save(self):
        recent=dict(self.items)
        sizes={key:len(json.dumps(item).encode()) for key,item in recent.items()}
        total=sum(sizes.values())
        if len(recent)>self.MAX_RECENT or total>self.MAX_RECENT_BYTES:
            terminal=sorted((t for t in self.items.values() if t['status'] not in ('queued','delivered')),
                            key=lambda t:t.get('updated',t['created']))
            if terminal:private_dir(self.archive)
            for item in terminal:
                if len(recent)<=self.MAX_RECENT and total<=self.MAX_RECENT_BYTES:break
                # Archive first: an interrupted checkpoint must never erase a receipt.
                write_json(self._archive_path(item['id']),item)
                del recent[item['id']]
                total-=sizes[item['id']]
        write_json(self.path, recent)
        self.items=recent

    def accept(self, agent, payload, request_id=None):
        rid = request_id or uuid.uuid4().hex
        if not isinstance(rid, str) or not 1 <= len(rid) <= 100:
            raise DeliveryError('invalid message id')
        existing = self.get(rid)
        if existing:
            if existing['agent'] != agent or existing.get('input') != payload:
                raise DeliveryError('message id was already used for different input or agent')
            return existing
        if sum(t['status'] in ('queued','delivered') for t in self.items.values()) >= 16:
            raise DeliveryError('project command queue is full; retry after a turn finishes')
        item = {'id':rid, 'agent':agent, 'status':'queued', 'created':time.time(), 'input':payload}
        self.items[rid] = item
        try:self.save()
        except Exception:
            self.items.pop(rid,None)
            raise
        return item

    def update(self, rid, status, result=None):
        item = self.items[rid]
        item.update(status=status, updated=time.time())
        if result is not None: item['result'] = result
        self.save()
        return item

    def pending(self, agent):
        return next((t for t in self.items.values() if t['agent'] == agent and t['status'] == 'queued'), None)

    def result(self, rid):
        item = self.get(rid)
        if not item: return None
        return {'turn_id':rid, 'agent':item['agent'], 'status':item['status'],
                'pending':item['status'] in ('queued','delivered'), **item.get('result',{})}
