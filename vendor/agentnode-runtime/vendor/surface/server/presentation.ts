import fs from 'node:fs';
import type { SurfaceStore } from './state.ts';

export const PRESENTATION_BRIDGE_JS = fs.readFileSync(new URL('./presentation-runtime.js', import.meta.url), 'utf8');
type Position = { deck: string; count: number; index: number; revision: number; requests: string[] };

/** Serialized workspace input. No model turn, host keyboard input, or cross-panel control. */
export function presentationEvent(store: SurfaceStore, id: string, action: string, input: unknown,
  context: (note: string, key: string) => void, scope='default'): boolean {
  if (action === '__workspace-focus') {
    const comp = store.getComponent(id);
    if (!comp || comp.hidden || !input || typeof input !== 'object') return true;
    const expanded = (input as Record<string, unknown>).expanded;
    if (typeof expanded !== 'boolean') return true;
    const views=store.state.view?.presentations??{};
    if(!expanded&&views[scope]!==id)return true;
    store.setPresentationFocus(scope,expanded?id:null);
    return true;
  }
  if (!action.startsWith('__presentation-')) return false;
  const comp = store.getComponent(id);
  if (!comp || comp.hidden || !['html', 'app'].includes(comp.type) || !input || typeof input !== 'object') return true;
  const p = input as Record<string, unknown>;
  const positions=(comp.props.__presentations??{}) as Record<string,Position>;
  const current=positions[scope]??(scope==='default'?comp.props.__presentation as Position|undefined:undefined);
  const save=(position:Position)=>store.patchComponentProps(id,{__presentations:{...positions,[scope]:position},...(scope==='default'?{__presentation:position}:{})});
  if (action === '__presentation-register') {
    if (typeof p.deck !== 'string' || !p.deck || p.deck.length > 120 || !Number.isSafeInteger(p.count) || Number(p.count) < 1 || Number(p.count) > 10000) return true;
    if (current?.deck === p.deck && current.count === p.count) return true;
    const count = Number(p.count);
    save({ deck: p.deck, count,
      index: current?.deck === p.deck ? Math.min(current.index, count - 1) : 0,
      revision: (current?.revision || 0) + 1, requests: [] });
  } else if (action === '__presentation-command') {
    if (!current || p.deck !== current.deck || typeof p.request !== 'string' || p.request.length > 100 || !p.request || current.requests.includes(p.request)) return true;
    let index: number;
    if (p.command === 'next') index = current.index + 1;
    else if (p.command === 'previous') index = current.index - 1;
    else if (p.command === 'go' && Number.isSafeInteger(p.index)) index = Number(p.index);
    else return true;
    index = Math.max(0, Math.min(current.count - 1, index));
    save({ ...current, index, revision: current.revision + 1,
      requests: [...current.requests.slice(-63), p.request] });
    if (index !== current.index) context(`Presentation “${comp.title || id}”: the human is at step ${index + 1} of ${current.count}. Read ui_get for its shared position.`, `${id}:presentation`);
  }
  return true;
}
