"""Install the versioned presentation extension into a configured Surface checkout."""
from pathlib import Path
import argparse, shutil, subprocess, time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'integrations/surface-presentation'
# One authoring convention for MCP descriptions and every CLI's runtime prompt.
GUIDE = '- PRESENTATIONS: register every deck. ' + (ROOT / 'prompts/surface.md').read_text().split('For every presentation, ', 1)[1].strip() + '\n'

def install(target, build=False):
    updates = {}
    def patch(name, marker, pairs):
        p = target/name; old = updates.get(p, p.read_text())
        if marker in old: return
        new = old
        for before, after in pairs:
            if new.count(before) != 1: raise RuntimeError(f'{name}: expected one integration anchor; no files changed')
            new = new.replace(before, after, 1)
        updates[p] = new
    patch('server/assets.ts', 'PRESENTATION_BRIDGE_JS', [
        ("import chokidar,", "import { PRESENTATION_BRIDGE_JS } from './presentation.ts';\nimport chokidar,"),
        ('res.end(BRIDGE_JS);', "res.end(BRIDGE_JS + '\\n' + PRESENTATION_BRIDGE_JS);")])
    patch('server/index.ts', 'presentationEvent(store', [
        ("/** ui-event actions that are context, not conversation. */", "import { presentationEvent } from './presentation.ts';\n\n/** ui-event actions that are context, not conversation. */"),
        ("      case 'ui-event': {", "      case 'ui-event': {\n        if (presentationEvent(store, msg.componentId, msg.action, msg.payload, (note, key) => agent.pushContext(note, key))) return;")])
    patch('server/tools.ts', '- PRESENTATIONS: register every deck', [('- KEYBOARD: the frame', GUIDE + '- KEYBOARD: the frame')])
    tools_path = target / 'server/tools.ts'
    tools_text = updates.get(tools_path, tools_path.read_text())
    import re
    current = re.sub(r'- PRESENTATIONS: register every deck[^\n]*\n', lambda match: GUIDE, tools_text)
    if current != tools_text:
        updates[tools_path] = current

    for panel in ['AppPanel','HtmlPanel']:
        patch(f'web/registry/{panel}.tsx', 'data-surface-id', [('className="panel-frame"', 'className="panel-frame"\n          data-surface-id={comp.id}')])
    patch('web/App.tsx', 'presentationView.current', [
        ('  // Events + theme handshake for iframe panels (apps & html).', '''  // The iframe receives only its own canonical presentation position.
  const presentationView = useRef({state, status});
  presentationView.current = {state, status};
  const sendPresentation = (frame: HTMLIFrameElement) => {
    const current = presentationView.current;
    const position = current.state?.components.find(c => c.id === frame.dataset.surfaceId)?.props.__presentation;
    frame.contentWindow?.postMessage({__surfacePresentation:{online:current.status === 'open',state:position}}, '*');
  };
  useEffect(() => {
    document.querySelectorAll<HTMLIFrameElement>('iframe[data-surface-id]').forEach(sendPresentation);
  }, [state?.v, status]);

  // Events + theme handshake for iframe panels (apps & html).'''),
        ("      if (!d || d.__surface !== true) return;", """      if (!d || d.__surface !== true) return;
      const frame = [...document.querySelectorAll<HTMLIFrameElement>('iframe[data-surface-id]')].find(f => f.contentWindow === e.source && f.dataset.surfaceId === d.componentId);
      if (!frame) return;
      if (d.action === '__presentation-ready') {sendPresentation(frame);return;}
      if (d.action.startsWith('__presentation-')) {
        sendPresentation(frame);
        if (presentationView.current.status !== 'open') return;
      }""")])
    patch('shared/protocol.ts', 'view?: { expandedId', [
        ('export interface SurfaceState {', 'export interface SurfaceState {\n  view?: { expandedId: string | null };')])
    patch('server/state.ts', 'setExpandedPanel(', [
        ('        components: saved.components ?? [],', '        components: saved.components ?? [],\n        view: { expandedId: saved.view?.expandedId ?? null },'),
        ('  // ---- components', """  /** Shared focus is durable workspace state, just like panel order. */
  setExpandedPanel(id: string | null): boolean {
    if (id !== null && (!this.getComponent(id) || this.getComponent(id)?.hidden)) return false;
    if ((this.state.view?.expandedId ?? null) === id) return false;
    this.commit([{ op: 'add', path: '/view', value: { expandedId: id } }]);
    return true;
  }

  // ---- components"""),
        ('    const prev = this.state.components[i];\n    this.commit([\n      { op: prev.hidden', '    const prev = this.state.components[i];\n    if (hidden && this.state.view?.expandedId === id) this.setExpandedPanel(null);\n    this.commit([\n      { op: prev.hidden'),
        ("    this.commit([{ op: 'remove', path: `/components/${i}` }]);", "    if (this.state.view?.expandedId === id) this.setExpandedPanel(null);\n    this.commit([{ op: 'remove', path: `/components/${i}` }]);")])
    patch('server/tools.ts', 'shared expanded panel:', [
        ("        return head + (items.length ?", "        return head + `shared expanded panel: ${store.state.view?.expandedId ?? 'none (panel layout)'}\\n` + (items.length ?"),
        ('region: comp.region, props: comp.props }, null, 2);', 'region: comp.region, expanded: store.state.view?.expandedId === comp.id, props: comp.props }, null, 2);')])
    patch('web/Workspace.tsx', "action: '__workspace-focus'", [
        ('  const [expandedId, setExpandedId] = useState<string | null>(null);', """  const focusedId = state?.view?.expandedId ?? null;
  const expandedId = state?.components.some(c => c.id === focusedId && !c.hidden) ? focusedId : null;
  const setExpandedId = (id: string | null) => {
    const componentId = id ?? expandedId;
    if (componentId) send({type: 'ui-event', componentId, action: '__workspace-focus', payload: {expanded: id !== null}});
  };"""),
        ('onToggleExpand={() => setExpandedId((cur) => (cur === c.id ? null : c.id))}', 'onToggleExpand={() => setExpandedId(expandedId === c.id ? null : c.id)}'),
        ("  // If the expanded panel disappears (agent removed it), drop the backdrop.\n  useEffect(() => {\n    if (expandedId && !components.some((c) => c.id === expandedId)) setExpandedId(null);\n  }, [expandedId, components.length]); // eslint-disable-line react-hooks/exhaustive-deps", "  // Hidden or removed panels are cleared by the hub, with no viewer feedback loop.")])
    for name in ['presentation.ts','presentation-runtime.js']:
        p=target/'server'/name; new=(SOURCE/name).read_text()
        if not p.exists() or p.read_text()!=new: updates[p]=new
    # Contract v2 owns these integration files. Keep upstream versions explicit.
    import json
    baseline=ROOT/'vendor/surface'
    if json.loads((target/'package.json').read_text()).get('version') != json.loads((baseline/'package.json').read_text()).get('version'):
        raise RuntimeError('Unsupported Surface version; review the integration before upgrading')
    for name in ('server/auth.ts','server/index.ts','server/state.ts','server/assets.ts','server/workspaces.ts','shared/protocol.ts','web/App.tsx','web/Workspace.tsx','web/useSurface.ts','web/registry/HtmlPanel.tsx'):
        p=target/name;content=(baseline/name).read_text()
        if not p.exists() or p.read_text()!=content:updates[p]=content
    if updates:
        backup=target/'.agentnode-presentation-backups'/str(time.time_ns())
        for p in updates:
            if p.exists():
                dest=backup/p.relative_to(target);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
        for p,new in updates.items():p.write_text(new)
    marker=target/'.agentnode-presentation-built'
    import hashlib
    sources=sorted(p for folder in ('server','shared','web') for p in (target/folder).rglob('*') if p.is_file() and 'dist' not in p.relative_to(target).parts)
    digest=hashlib.sha256(b''.join(str(p.relative_to(target)).encode()+b'\0'+p.read_bytes() for p in sources)).hexdigest()
    if build and (not marker.exists() or marker.read_text()!=digest):
        subprocess.run(['npm','run','check'],cwd=target,check=True)
        subprocess.run(['npm','run','build'],cwd=target,check=True)
        marker.write_text(digest)
    print(f'Surface presentation extension: {len(updates)} files updated')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('surface_dir',type=Path);parser.add_argument('--build',action='store_true')
    args=parser.parse_args();install(args.surface_dir.resolve(),args.build)
