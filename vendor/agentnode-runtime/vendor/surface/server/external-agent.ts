import type { AgentLike } from './agent.ts';
import type { SurfaceStore } from './state.ts';

/**
 * External-agent mode (SURFACE_AGENT=external): the hub owns panels but NOT
 * the conversation. Some other process — e.g. the TVPC bridge running a
 * persistent Claude Code session — owns the chat and calls the ui_* tools
 * through the stdio proxy. Everything the hub would have told its own agent
 * (ui-events, passive context notes, interrupts) is forwarded to that process
 * over HTTP instead, and it reports agent phase back via POST /internal/agent.
 */
export class ExternalAgent implements AgentLike {
  static url = process.env.SURFACE_AGENT_URL ?? 'http://127.0.0.1:8444';

  constructor(
    private store: SurfaceStore,
    private workspaceDir: string,
    private wsId: string,
  ) {
    store.patchAgent({ phase: 'idle', model: process.env.SURFACE_MODEL ?? 'external', warm: true });
    store.addLog('info', 'hub', `external agent mode — conversation owned by ${ExternalAgent.url}`);
  }

  private post(path: string, body: Record<string, unknown>): void {
    fetch(`${ExternalAgent.url}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ws: this.wsId, dir: this.workspaceDir, ...body }),
    }).catch((err) => this.store.addLog('warn', 'hub', `external agent unreachable: ${String(err).slice(0, 120)}`));
  }

  start(): void {
    /* nothing to spawn */
  }

  sendUser(text: string, files: string[] = []): void {
    this.post('/api/surface/event', { text, files });
  }

  pushContext(note: string, key?: string): void {
    this.post('/api/surface/context', { note, key });
  }

  async interrupt(): Promise<void> {
    this.post('/api/surface/interrupt', {});
  }

  async stop(): Promise<void> {
    /* nothing to stop */
  }

  async reset(): Promise<void> {
    this.store.clearChat();
    this.store.addLog('warn', 'human', 'reset requested — handled by the external agent');
    this.post('/api/surface/reset', {});
  }

  setWarm(_desired: boolean): void {
    /* always warm */
  }
}
