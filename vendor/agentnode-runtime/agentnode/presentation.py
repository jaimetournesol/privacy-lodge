"""Small, durable stage composition; no chat text, audio, images or credentials."""
import copy
import json
import time
import uuid
from .storage import read_json, write_json


class StageConflict(ValueError):
    pass


def clean_tiles(tiles):
    if not isinstance(tiles, list) or len(tiles) > 6:
        raise ValueError('The presentation stage holds up to six views.')
    clean = []
    for tile in tiles:
        if not isinstance(tile, dict):
            raise ValueError('Invalid presentation view.')
        node, project, kind = tile.get('node'), tile.get('project'), tile.get('kind')
        if (not isinstance(node, str) or not node or len(node) > 100
                or (project is not None and (not isinstance(project, str) or not project or len(project) > 64))
                or kind not in ('screen', 'surface', 'fleet')
                or (kind in ('surface', 'fleet') and not project)
                or not isinstance(tile.get('pinned', False), bool)):
            raise ValueError('Invalid presentation view.')
        item = {'node': node, 'project': project if kind != 'screen' else None,
                'kind': kind, 'pinned': tile.get('pinned', False)}
        if any(key(t) == key(item) for t in clean):
            raise ValueError('Duplicate presentation view.')
        workspace = tile.get('workspace')
        if workspace is not None:
            if kind != 'surface' or not isinstance(workspace, str) or not 1 <= len(workspace) <= 200:
                raise ValueError('Invalid Surface workspace.')
            item['workspace'] = workspace
        clean.append(item)
    return clean


def key(tile):
    return (tile['node'], tile['project'] if tile['kind'] != 'screen' else None, tile['kind'])


class StageState:
    def __init__(self, path, data=None):
        self.path = path
        data = read_json(path, {}) if path else (data or {})
        self.tiles = clean_tiles(data.get('tiles', []))
        self.revision = data.get('revision', 0)
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError('Invalid presentation revision.')
        self.updated_at = data.get('updated_at', 0)
        self.last_reset_revision = data.get('last_reset_revision', 0)
        self.schema = data.get('schema', 1)
        self.session_id = data.get('session_id') or str(uuid.uuid4())
        self.outputs = {k: clean_tiles(v) for k,v in data.get('outputs', {}).items()}
        self.active_display = data.get('active_display')

    def snapshot(self, display=None):
        selected = display if display is not None else self.active_display
        return {'revision': self.revision, 'tiles': copy.deepcopy(self.outputs.get(selected, self.tiles)),
                'updated_at': self.updated_at, 'last_reset_revision': self.last_reset_revision,
                'session_id': self.session_id, 'active_display': self.active_display}

    def document(self):
        return {'schema': 2, 'session_id': self.session_id, 'revision': self.revision,
                'tiles': copy.deepcopy(self.tiles), 'outputs': copy.deepcopy(self.outputs),
                'active_display': self.active_display, 'updated_at': self.updated_at,
                'last_reset_revision': self.last_reset_revision}

    def check(self, expected):
        if expected is not None and (type(expected) is not int or expected != self.revision):
            raise StageConflict('Stage changed. Read its current revision before changing content.')

    def replace(self, tiles, expected=None, *, reset=False, display=None):
        self.check(expected)
        clean = clean_tiles(tiles)
        data = self.document()
        if reset:
            data.update(tiles=clean, outputs={}, active_display=None)
        elif display is not None:
            data['outputs'][display] = clean
            data['active_display'] = display
        else:
            data.update(tiles=clean, active_display=None)
        if data == self.document() and not reset:
            return self.snapshot(display)
        data.update(revision=self.revision + 1, updated_at=time.time())
        if reset:data['last_reset_revision'] = data['revision']
        self._commit(data)
        return self.snapshot(display)

    def _commit(self, data):
        if self.path:write_json(self.path, data)
        self.schema = 2
        self.tiles, self.outputs, self.active_display = data['tiles'], data['outputs'], data['active_display']
        self.revision, self.updated_at = data['revision'], data['updated_at']
        self.last_reset_revision = data['last_reset_revision']

    def migrate_displays(self, records):
        if self.schema >= 2:return
        data = self.document()
        for identity, record in records.items():
            if record.get('stage') is not None:
                data['outputs'][identity] = clean_tiles(record['stage']['tiles'])
        if self.path and self.path.exists():
            backup = self.path.with_name(self.path.name + '.before-unified-stage')
            if not backup.exists():
                from .storage import atomic_text
                atomic_text(backup, self.path.read_text())
        self._commit(data)
        self.schema = 2

    def reset(self, conductor, expected):
        node, project = conductor
        tiles = [{'node': node, 'project': project, 'kind': 'fleet', 'pinned': False}] if project else []
        return self.replace(tiles, expected, reset=True)

    def context(self):
        note = (f'The human used Reset Stage at revision {self.last_reset_revision}, '
                'clearing all screen/Surface views and pins and returning to Fleet. '
                'Respect that reset; do not restore removed content unless requested. '
                'The current composition below includes any changes since that reset.\n') if self.last_reset_revision else ''
        return (f'[stage-context]\n{note}Current shared stage, revision {self.revision}: '
                + json.dumps(self.snapshot(), ensure_ascii=False))

    def apply(self, event, conductor):
        # Build the new output in memory, then commit the whole session once.
        display = event.get('display')
        if display is not None or self.active_display is not None:
            target = StageState(None, data={'tiles': self.snapshot(display)['tiles']})
            target._apply(event, conductor)
            return self.replace(target.tiles, event.get('base_revision'), display=display)
        self.check(event.get('base_revision'))
        return self._apply(event, conductor)

    def _apply(self, event, conductor):
        def expand(node, project, view, pinned):
            if view not in (None, 'screen', 'surface', 'both'):
                raise ValueError('Invalid stage view.')
            view = view or ('both' if project else 'screen')
            out = []
            if view in ('screen', 'both'):
                kind = 'fleet' if project and (node, project) == conductor else 'screen'
                out.append({'node': node, 'project': project, 'kind': kind, 'pinned': pinned})
            if view in ('surface', 'both') and project:
                out.append({'node': node, 'project': project, 'kind': 'surface', 'pinned': pinned})
            return clean_tiles(out)

        kind = event.get('type')
        if kind == 'stage':
            wanted = []
            for item in event['tiles']:
                for tile in expand(item['node'], item.get('project'), item.get('view'), True):
                    if not any(key(t) == key(tile) for t in wanted): wanted.append(tile)
            return self.replace(wanted)
        if kind == 'focus':
            wanted = expand(event.get('node'), event.get('project'), event.get('view'), bool(event.get('pin')))
            kept = [dict(t) for t in self.tiles if event.get('mode') == 'add' or t['pinned'] or any(key(w) == key(t) for w in wanted)]
            for tile in wanted:
                existing = next((t for t in kept if key(t) == key(tile)), None)
                if existing is None: kept.append(tile)
                elif tile['pinned']: existing['pinned'] = True
            return self.replace(kept)
        if kind == 'pin':
            tiles = copy.deepcopy(self.tiles)
            for tile in tiles:
                if (not event.get('node') or tile['node'] == event['node']) and (not event.get('project') or tile['project'] == event['project'] or tile['kind'] == 'screen'):
                    tile['pinned'] = event.get('pinned') is not False
            return self.replace(tiles)
        return None


class DisplayRegistry:
    """Connected output names and browser leases; persistent content lives in StageState."""
    TTL = 75
    LIMIT = 32

    def __init__(self, path, clock=time.monotonic):
        self.path, self.clock = path, clock
        self.records = read_json(path, {})
        self.leases = {}
        self.acks = {}

    def active(self, display):
        return isinstance(display, str) and any(k[0] == display and expiry > self.clock() for k,expiry in self.leases.items())

    def prune(self):
        now = self.clock()
        leases = {k:v for k,v in self.leases.items() if v > now}
        records = {k:v for k,v in self.records.items() if any(pair[0] == k for pair in leases)}
        if records != self.records:
            write_json(self.path,records)
            self.records = records
        self.leases = leases
        self.acks = {k:v for k,v in self.acks.items() if k in leases}

    def register(self, display, name=None, *, mobile=False, connection='legacy', previous_connection=None):
        import re
        if mobile:raise ValueError('Phones are controllers, not presenter displays.')
        for value in (display, connection, *([previous_connection] if previous_connection is not None else [])):
            if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', value):
                raise ValueError('Invalid display or connection ID.')
        previous_name = self.records.get(display, {}).get('name')
        self.prune()
        name = name or previous_name or 'Screen ' + display[:6]
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 60 or any(ord(c)<32 for c in name):
            raise ValueError('Use a display name of 1–60 characters.')
        name = name.strip()
        if name != self.records.get(display,{}).get('name') and any(k != display and v['name'].casefold() == name.casefold() for k,v in self.records.items()):
            raise ValueError('That screen name is in use. Join the connected screen instead.')
        if display not in self.records and len(self.records) >= self.LIMIT:
            raise ValueError('Too many connected presenter screens. Join an existing screen.')
        records = copy.deepcopy(self.records)
        records[display] = {'name': name}
        if records != self.records:
            write_json(self.path, records)
            self.records = records
        self.leases = {k:v for k,v in self.leases.items() if v > self.clock()}
        if previous_connection is not None:
            self.leases.pop((display,previous_connection),None)
            self.acks.pop((display,previous_connection),None)
        self.leases[(display,connection)] = self.clock() + self.TTL
        return self.describe(display)

    def leave(self, display, connection='legacy'):
        self.leases.pop((display,connection),None)
        self.acks.pop((display,connection),None)
        self.prune()

    def describe(self, display):
        connections = [k for k,v in self.leases.items() if k[0] == display and v > self.clock()]
        revisions = [self.acks.get(k,-1) for k in connections]
        name = self.records[display]['name']
        duplicate = any(k != display and v['name'].casefold() == name.casefold() for k,v in self.records.items())
        label = name + ' · ' + display[-6:] if duplicate else name
        return {'id':display, 'name':name, 'label':label, 'online':bool(connections),
                'connections':len(connections), 'applied_revision':min(revisions) if revisions else None}

    def listing(self, shared):
        self.prune()
        return {'items':[{**self.describe(k),'presentation':shared.snapshot(k)} for k in self.records if self.active(k)],
                'known':[self.describe(k) for k in self.records], 'session':shared.document(),
                'revision':shared.revision, 'active_display':shared.active_display}

    def acknowledge(self, display, connection, revision, shared):
        if self.leases.get((display,connection),0) <= self.clock():
            raise ValueError('Display connection is offline.')
        if type(revision) is not int or not 0 <= revision <= shared.revision:
            raise ValueError('Invalid applied revision.')
        self.acks[(display,connection)] = max(revision,self.acks.get((display,connection),-1))

    def resolve(self, display=None):
        if display is not None:
            if not self.active(display):raise ValueError('Presenter display is offline or unknown. List displays again.')
            return display
        online = [k for k in self.records if self.active(k)]
        if len(online)>1:raise DisplayChoice([self.describe(k) for k in online])
        return online[0] if online else None

    def context(self, shared):
        return ('[presenter-displays] One persistent Stage. Phones are controllers, never outputs. '
                'With multiple online screens and no explicit destination, ASK the human which screen before presenting. '
                'Do not infer permission from active_display. Keep an explicitly chosen destination for that same task only. '
                'Use the current revision from list_displays for every presentation change. Reset Stage clears ALL outputs. '
                'Only items are connected destinations; retained session output content is not a list of available screens.\n'
                + json.dumps(self.listing(shared),ensure_ascii=False))


class DisplayChoice(ValueError):
    def __init__(self, displays):
        self.displays = displays
        super().__init__('Choose a presenter screen: ' + ', '.join(d['name'] for d in displays))
