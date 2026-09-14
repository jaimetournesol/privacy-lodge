import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import type { ServerMsg, WorkspaceInfo } from '../shared/protocol.ts';
import { AgentHost } from './agent.ts';
import type { AgentLike } from './agent.ts';
import { ExternalAgent } from './external-agent.ts';
import type { AssetHub } from './assets.ts';
import type { OpencodeServer } from './opencode.ts';
import { SurfaceStore } from './state.ts';
import { createToolRunner } from './tools.ts';

interface WorkspaceMeta {
  id: string;
  name: string;
  dir: string;
  attached: boolean;
  createdAt: number;
}

export interface Workspace {
  meta: WorkspaceMeta;
  store: SurfaceStore;
  agent: AgentLike;
  runTool: (name: string, args: Record<string, unknown>) => Promise<string>;
}

const sanitizeName = (name: string) =>
  name.trim().replace(/[^\w.\- ]/g, '').replace(/\s+/g, ' ').slice(0, 60);

/**
 * One Surface per folder: each workspace owns a store (UI state persisted in
 * <dir>/.surface/), an agent (its own resumable OpenCode session keyed to that
 * cwd — memory and CLAUDE.md/AGENTS.md instructions come from the folder
 * itself), an inbox/ and an apps/ directory.
 */
export class WorkspaceManager {
  private metas: WorkspaceMeta[] = [];
  private active = new Map<string, Workspace>();
  private registryFile: string;
  private managedRoot: string;
  private trashDir: string;

  constructor(
    private rootDir: string,
    private opencode: OpencodeServer | null,
    private assets: AssetHub,
    private broadcastToWs: (wsId: string, msg: ServerMsg) => void,
    private onPhase: (wsId: string, phase: string) => void,
    private capture: (wsId: string) => Promise<string>,
    private viewers: (wsId: string) => { width: number; height: number; dpr: number }[] = () => [],
  ) {
    const hubDir = path.join(rootDir, '.surface-hub');
    fs.mkdirSync(hubDir, { recursive: true });
    this.registryFile = path.join(hubDir, 'workspaces.json');
    this.managedRoot = path.join(rootDir, 'workspaces');
    this.trashDir = path.join(hubDir, 'trash');
    fs.mkdirSync(this.managedRoot, { recursive: true });
    this.loadRegistry();
    // First boot: adopt the original single workspace as "main".
    if (this.metas.length === 0) {
      const legacy = path.join(rootDir, 'workspace');
      if (fs.existsSync(legacy)) {
        this.metas.push({ id: 'main', name: 'main', dir: legacy, attached: false, createdAt: Date.now() });
        this.saveRegistry();
      }
    }
  }

  private loadRegistry(): void {
    try {
      this.metas = JSON.parse(fs.readFileSync(this.registryFile, 'utf8'));
    } catch {
      this.metas = [];
    }
  }

  private saveRegistry(): void {
    fs.writeFileSync(this.registryFile, JSON.stringify(this.metas, null, 2));
  }

  list(): WorkspaceInfo[] {
    return this.metas.map((m) => {
      const info: WorkspaceInfo = { id: m.id, name: m.name, dir: m.dir, attached: m.attached };
      const live = this.active.get(m.id);
      if (live) {
        info.phase = live.store.state.agent.phase;
        info.costUsd = live.store.state.agent.costUsd;
        info.panels = live.store.state.components.length;
        info.lastActive = Date.now();
      } else {
        // Cheap peek at the persisted state for the home screen.
        try {
          const file = path.join(m.dir, '.surface', 'state.json');
          const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
          info.costUsd = saved.agent?.costUsd ?? 0;
          info.panels = saved.components?.length ?? 0;
          info.lastActive = fs.statSync(file).mtimeMs;
        } catch {
          /* never used yet */
        }
      }
      return info;
    });
  }

  info(id: string): WorkspaceInfo | undefined {
    const m = this.metas.find((x) => x.id === id);
    return m && { id: m.id, name: m.name, dir: m.dir, attached: m.attached };
  }

  defaultId(): string | undefined {
    return this.metas[0]?.id;
  }

  /** The workspace whose folder is `dir` (the surface MCP proxy identifies itself by cwd). */
  byDir(dir: string): Workspace | undefined {
    const target = path.resolve(dir);
    const meta = this.metas.find((m) => path.resolve(m.dir) === target || fs.realpathSync.native(m.dir) === target);
    return meta ? this.get(meta.id) : undefined;
  }

  /** Lazy activation: stores and agents come alive on first use. */
  get(id: string): Workspace | undefined {
    const existing = this.active.get(id);
    if (existing) return existing;
    const meta = this.metas.find((m) => m.id === id);
    if (!meta || !fs.existsSync(meta.dir)) return undefined;

    for (const sub of ['.surface', 'inbox', 'apps']) {
      fs.mkdirSync(path.join(meta.dir, sub), { recursive: true });
    }

    const store = new SurfaceStore();
    store.attachPersistence(path.join(meta.dir, '.surface', 'state.json'));
    store.subscribe((v, ops) => {
      this.broadcastToWs(meta.id, { type: 'patch', v, ops });
      for (const op of ops) {
        if (op.path === '/agent/phase') this.onPhase(meta.id, String(op.value));
      }
    });
    const agent: AgentLike = this.opencode
      ? new AgentHost(store, meta.dir, this.opencode)
      : new ExternalAgent(store, meta.dir, meta.id);
    const runTool = createToolRunner({
      store,
      assets: this.assets,
      workspaceDir: meta.dir,
      wsId: meta.id,
      capture: () => this.capture(meta.id),
      viewers: () => this.viewers(meta.id),
    });
    const ws: Workspace = { meta, store, agent, runTool };
    this.active.set(id, ws);
    this.rematerialize(ws);
    return ws;
  }

  /** Re-register asset URLs and app mounts for components restored from disk. */
  private rematerialize(ws: Workspace): void {
    for (const comp of [...ws.store.state.components]) {
      const p = comp.props as { path?: string; appId?: string; src?: string };
      if (!p.path || typeof p.path !== 'string') continue;
      const abs = path.isAbsolute(p.path) ? p.path : path.resolve(ws.meta.dir, p.path);
      try {
        if (comp.type === 'app') {
          const mount = this.assets.mountApp(abs, comp.id, ws.meta.id, p.appId);
          ws.store.patchComponentProps(comp.id, { path: abs, appId: mount.appId, url: mount.url });
        } else if (comp.type === 'image' || comp.type === 'video' || comp.type === 'audio' || comp.type === 'file') {
          const token = typeof p.src === 'string' ? /^\/assets\/([0-9a-f]{8}-[0-9a-f]{3})\//.exec(p.src)?.[1] : undefined;
          const asset = this.assets.registerAsset(abs, token);
          ws.store.patchComponentProps(comp.id, { path: abs, src: asset.url });
        }
      } catch (err) {
        // Never delete a panel over a re-registration hiccup — leave it in
        // place (possibly broken) and say so.
        ws.store.addLog('warn', 'hub', `could not re-serve ${comp.id} (${abs}): ${err instanceof Error ? err.message : err}`);
      }
    }
  }

  create(name: string): WorkspaceInfo {
    const clean = sanitizeName(name);
    if (!clean) throw new Error('workspace name required');
    const dir = path.join(this.managedRoot, clean);
    if (fs.existsSync(dir) || this.metas.some((m) => m.dir === dir)) {
      throw new Error(`a workspace folder named “${clean}” already exists`);
    }
    fs.mkdirSync(dir, { recursive: true });
    const meta: WorkspaceMeta = {
      id: `ws-${Date.now().toString(36)}`,
      name: clean,
      dir,
      attached: false,
      createdAt: Date.now(),
    };
    this.metas.push(meta);
    this.saveRegistry();
    return this.info(meta.id)!;
  }

  attach(dirPath: string): WorkspaceInfo {
    const dir = path.resolve(dirPath.replace(/^~(?=\/|$)/, os.homedir()));
    if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) {
      throw new Error(`not a directory: ${dir}`);
    }
    const existing = this.metas.find((m) => m.dir === dir);
    if (existing) return this.info(existing.id)!;
    const meta: WorkspaceMeta = {
      id: `ws-${Date.now().toString(36)}`,
      name: path.basename(dir),
      dir,
      attached: true,
      createdAt: Date.now(),
    };
    this.metas.push(meta);
    this.saveRegistry();
    return this.info(meta.id)!;
  }

  /**
   * Renames the workspace. Managed folders are physically renamed; the agent's
   * OpenCode session is addressed by id, so its memory survives the move.
   * Attached folders only change display name.
   */
  async rename(id: string, newName: string): Promise<WorkspaceInfo> {
    const meta = this.metas.find((m) => m.id === id);
    if (!meta) throw new Error('unknown workspace');
    const clean = sanitizeName(newName);
    if (!clean) throw new Error('name required');

    if (!meta.attached) {
      const newDir = path.join(this.managedRoot, clean);
      if (newDir !== meta.dir) {
        if (fs.existsSync(newDir)) throw new Error(`a folder named “${clean}” already exists`);
        await this.deactivate(id);
        const oldDir = meta.dir;
        fs.renameSync(oldDir, newDir);
        // Any absolute component paths under the old folder move with it.
        try {
          const stateFile = path.join(newDir, '.surface', 'state.json');
          const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
          for (const comp of state.components ?? []) {
            const cp = comp.props?.path;
            if (typeof cp === 'string' && cp.startsWith(oldDir + path.sep)) {
              comp.props.path = path.join(newDir, path.relative(oldDir, cp));
            }
          }
          fs.writeFileSync(stateFile, JSON.stringify(state));
        } catch {
          /* no state yet */
        }
        meta.dir = newDir;
      }
    }
    meta.name = clean;
    this.saveRegistry();
    return this.info(id)!;
  }

  /** Managed folders go to the hub trash (recoverable); attached ones only detach. */
  async remove(id: string): Promise<void> {
    const meta = this.metas.find((m) => m.id === id);
    if (!meta) return;
    await this.deactivate(id);
    if (!meta.attached && fs.existsSync(meta.dir)) {
      fs.mkdirSync(this.trashDir, { recursive: true });
      fs.renameSync(meta.dir, path.join(this.trashDir, `${meta.name}-${Date.now()}`));
    }
    this.metas = this.metas.filter((m) => m.id !== id);
    this.saveRegistry();
  }

  private async deactivate(id: string): Promise<void> {
    const ws = this.active.get(id);
    if (!ws) return;
    await ws.agent.stop();
    this.active.delete(id);
  }

  forEachActive(cb: (id: string, ws: Workspace) => void): void {
    for (const [id, ws] of this.active) cb(id, ws);
  }

  async stopAll(): Promise<void> {
    await Promise.all([...this.active.values()].map((ws) => ws.agent.stop()));
  }
}
