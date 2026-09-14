import jsonpatch from 'fast-json-patch';
const { applyPatch } = jsonpatch;
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import type {
  AgentStatus,
  ChatMessage,
  ComponentType,
  LogEntry,
  LogLevel,
  PatchOp,
  Region,
  SurfaceComponent,
  SurfaceState,
} from '../shared/protocol.ts';

const MAX_LOG_ENTRIES = 800;
const MAX_CHAT_ENTRIES = 500;

export type StateListener = (v: number, ops: PatchOp[]) => void;

function shortId(prefix: string): string {
  return `${prefix}-${randomUUID().slice(0, 8)}`;
}

/**
 * Canonical Surface document. All mutations go through mutators that emit
 * RFC 6902 ops, apply them locally, bump the version, and notify listeners —
 * so every connected client replays exactly what the hub applied.
 */
export class SurfaceStore {
  state: SurfaceState = {
    v: 0,
    components: [],
    chat: [],
    log: [],
    agent: { phase: 'starting', model: 'GLM-5.3-Flash', costUsd: 0, turns: 0 },
  };

  private listeners = new Set<StateListener>();
  private persistPath?: string;
  private persistTimer: ReturnType<typeof setTimeout> | undefined;

  /** Restores prior state from disk and persists (debounced) on every commit. */
  attachPersistence(file: string): void {
    this.persistPath = file;
    try {
      const saved = JSON.parse(fs.readFileSync(file, 'utf8')) as SurfaceState;
      this.state = {
        v: saved.v ?? 0,
        components: saved.components ?? [],
        view: { expandedId: saved.view?.expandedId ?? null },
        chat: (saved.chat ?? []).map((m) => ({ ...m, streaming: false })),
        log: saved.log ?? [],
        agent: { ...this.state.agent, costUsd: saved.agent?.costUsd ?? 0, turns: saved.agent?.turns ?? 0 },
      };
    } catch {
      /* first boot */
    }
  }

  subscribe(fn: StateListener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private commit(ops: PatchOp[]): void {
    if (ops.length === 0) return;
    applyPatch(this.state, ops as never[], false, true);
    this.state.v += 1;
    for (const fn of this.listeners) fn(this.state.v, ops);
    if (this.persistPath && !this.persistTimer) {
      this.persistTimer = setTimeout(() => {
        this.persistTimer = undefined;
        try {
          fs.writeFileSync(this.persistPath!, JSON.stringify(this.state));
        } catch {
          /* disk hiccup — next commit retries */
        }
      }, 500);
    }
  }

  /** Shared focus is durable workspace state, just like panel order. */
  setPresentationFocus(scope:string,id:string|null): void {
    this.commit([{op:'add',path:'/view',value:{...this.state.view,expandedId:this.state.view?.expandedId??null,
      presentations:{...this.state.view?.presentations,[scope]:id}}}]);
  }

  setExpandedPanel(id: string | null): boolean {
    if (id !== null && (!this.getComponent(id) || this.getComponent(id)?.hidden)) return false;
    if ((this.state.view?.expandedId ?? null) === id) return false;
    this.commit([{ op: 'add', path: '/view', value: { expandedId: id } }]);
    return true;
  }

  // ---- components -------------------------------------------------------

  private componentIndex(id: string): number {
    return this.state.components.findIndex((c) => c.id === id);
  }

  getComponent(id: string): SurfaceComponent | undefined {
    return this.state.components.find((c) => c.id === id);
  }

  upsertComponent(input: {
    id?: string;
    type: ComponentType;
    title?: string;
    region?: Region;
    props: Record<string, unknown>;
  }): SurfaceComponent {
    const id = input.id ?? shortId(input.type);
    const existing = this.componentIndex(id);
    const now = Date.now();
    if (existing >= 0) {
      const prev = this.state.components[existing];
      const next: SurfaceComponent = {
        ...prev,
        type: input.type,
        title: input.title ?? prev.title,
        region: input.region ?? prev.region,
        props: input.props,
        hidden: false, // an agent re-show always brings the panel back on stage
        v: prev.v + 1,
        updatedAt: now,
      };
      this.commit([{ op: 'replace', path: `/components/${existing}`, value: next }]);
      return next;
    }
    const region = input.region ?? 'main';
    const order =
      this.state.components.reduce((m, c) => Math.max(m, c.order), 0) + 1;
    const comp: SurfaceComponent = {
      id,
      type: input.type,
      title: input.title,
      region,
      order,
      props: input.props,
      v: 1,
      createdAt: now,
      updatedAt: now,
    };
    this.commit([{ op: 'add', path: '/components/-', value: comp }]);
    return comp;
  }

  /** Shallow-merges `patch` into the component's props. Returns false if absent. */
  patchComponentProps(id: string, patch: Record<string, unknown>): boolean {
    const i = this.componentIndex(id);
    if (i < 0) return false;
    const prev = this.state.components[i];
    const props = { ...prev.props, ...patch };
    // Drop keys explicitly set to null so the agent can delete fields.
    for (const [k, v] of Object.entries(patch)) if (v === null) delete props[k];
    this.commit([
      { op: 'replace', path: `/components/${i}/props`, value: props },
      { op: 'replace', path: `/components/${i}/v`, value: prev.v + 1 },
      { op: 'replace', path: `/components/${i}/updatedAt`, value: Date.now() },
    ]);
    return true;
  }

  setComponentMeta(id: string, meta: { title?: string; region?: Region }): boolean {
    const i = this.componentIndex(id);
    if (i < 0) return false;
    const prev = this.state.components[i];
    const ops: PatchOp[] = [];
    if (meta.title !== undefined)
      ops.push({ op: prev.title === undefined ? 'add' : 'replace', path: `/components/${i}/title`, value: meta.title });
    if (meta.region !== undefined)
      ops.push({ op: 'replace', path: `/components/${i}/region`, value: meta.region });
    ops.push({ op: 'replace', path: `/components/${i}/v`, value: prev.v + 1 });
    this.commit(ops);
    return true;
  }

  /** Applies a human rearrangement: full ordered id lists per region. */
  arrange(main: string[], side: string[]): void {
    const ops: PatchOp[] = [];
    let order = 1;
    for (const [region, ids] of [['main', main], ['side', side]] as const) {
      for (const id of ids) {
        const i = this.componentIndex(id);
        if (i < 0) continue;
        const c = this.state.components[i];
        if (c.order !== order) ops.push({ op: 'replace', path: `/components/${i}/order`, value: order });
        if (c.region !== region) ops.push({ op: 'replace', path: `/components/${i}/region`, value: region });
        order += 1;
      }
    }
    this.commit(ops);
  }

  /** Soft show/hide — the panel stays in state so the human can reopen it. */
  setHidden(id: string, hidden: boolean): boolean {
    const i = this.componentIndex(id);
    if (i < 0) return false;
    const prev = this.state.components[i];
    if (hidden && this.state.view?.expandedId === id) this.setExpandedPanel(null);
    this.commit([
      { op: prev.hidden === undefined ? 'add' : 'replace', path: `/components/${i}/hidden`, value: hidden },
      { op: 'replace', path: `/components/${i}/v`, value: prev.v + 1 },
      { op: 'replace', path: `/components/${i}/updatedAt`, value: Date.now() },
    ]);
    return true;
  }

  removeComponent(id: string): boolean {
    const i = this.componentIndex(id);
    if (i < 0) return false;
    if (this.state.view?.expandedId === id) this.setExpandedPanel(null);
    this.commit([{ op: 'remove', path: `/components/${i}` }]);
    return true;
  }

  clearComponents(region?: Region): number {
    const victims = this.state.components.filter(
      (c) => region === undefined || c.region === region,
    );
    for (const c of victims) this.removeComponent(c.id);
    return victims.length;
  }

  // ---- chat --------------------------------------------------------------

  addChat(msg: Omit<ChatMessage, 'id' | 'ts'> & { id?: string }): ChatMessage {
    const full: ChatMessage = { id: msg.id ?? shortId('msg'), ts: Date.now(), ...msg };
    const ops: PatchOp[] = [{ op: 'add', path: '/chat/-', value: full }];
    if (this.state.chat.length >= MAX_CHAT_ENTRIES) ops.unshift({ op: 'remove', path: '/chat/0' });
    this.commit(ops);
    return full;
  }

  clearChat(): void {
    if (this.state.chat.length === 0) return;
    this.commit([{ op: 'replace', path: '/chat', value: [] }]);
  }

  removeChat(id: string): void {
    const i = this.state.chat.findIndex((m) => m.id === id);
    if (i >= 0) this.commit([{ op: 'remove', path: `/chat/${i}` }]);
  }

  /** Clears any lingering streaming flags (belt-and-braces at end of turn). */
  finishStreaming(): void {
    const ops: PatchOp[] = [];
    this.state.chat.forEach((m, i) => {
      if (m.streaming) ops.push({ op: 'replace', path: `/chat/${i}/streaming`, value: false });
    });
    this.commit(ops);
  }

  /** Replace the text of a chat message (used for streamed assistant text). */
  setChatText(id: string, text: string, streaming: boolean): void {
    const i = this.state.chat.findIndex((m) => m.id === id);
    if (i < 0) return;
    this.commit([
      { op: 'replace', path: `/chat/${i}/text`, value: text },
      { op: this.state.chat[i].streaming === undefined ? 'add' : 'replace', path: `/chat/${i}/streaming`, value: streaming },
    ]);
  }

  // ---- log ---------------------------------------------------------------

  addLog(level: LogLevel, source: LogEntry['source'], message: string, data?: unknown): LogEntry {
    const entry: LogEntry = { id: shortId('log'), ts: Date.now(), level, source, message };
    if (data !== undefined) entry.data = data;
    const ops: PatchOp[] = [{ op: 'add', path: '/log/-', value: entry }];
    if (this.state.log.length >= MAX_LOG_ENTRIES) ops.unshift({ op: 'remove', path: '/log/0' });
    this.commit(ops);
    return entry;
  }

  // ---- agent status ------------------------------------------------------

  patchAgent(patch: Partial<AgentStatus>): void {
    const ops: PatchOp[] = [];
    for (const [k, v] of Object.entries(patch)) {
      const key = k as keyof AgentStatus;
      if (v === undefined) {
        if (this.state.agent[key] !== undefined) ops.push({ op: 'remove', path: `/agent/${k}` });
      } else {
        ops.push({
          op: this.state.agent[key] === undefined ? 'add' : 'replace',
          path: `/agent/${k}`,
          value: v,
        });
      }
    }
    this.commit(ops);
  }
}
