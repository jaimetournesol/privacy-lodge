import { spawn, type ChildProcess } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/**
 * One headless `opencode serve` per hub. Every workspace gets its own
 * OpenCode session inside it (scoped by `?directory=`), the hub subscribes to
 * the server's global event stream once and fans events out per session.
 *
 * Isolation: the server is launched with OPENCODE_CONFIG pointing at a
 * hub-written config file (model, provider, permissions, MCP servers,
 * instructions). The user's global ~/.config/opencode is left untouched — the
 * hub only reads it to switch off its MCP servers inside Surface sessions, so
 * nothing from this service leaks into the rest of the laptop's OpenCode use.
 */

export interface LlmSettings {
  providerId: string;
  modelId: string;
  displayName: string;
  baseURL: string;
  apiKey: string;
  reasoningEffort: 'low' | 'high';
  contextLimit: number;
  outputLimit: number;
}

export interface OpencodeOptions {
  rootDir: string;
  hubDir: string;
  hubPort: number;
  toolsJsonPath: string;
  instructions: string;
  llm: LlmSettings;
  /** Extra env (service API keys) injected into the server and, through it, into every tool the agent runs. */
  extraEnv: NodeJS.ProcessEnv;
  log: (level: 'info' | 'warn' | 'error', message: string) => void;
}

export type OpencodeEvent = { type: string; properties: Record<string, any> };
type Listener = (ev: OpencodeEvent) => void;

export const OPENCODE_BIN = process.env.SURFACE_OPENCODE_BIN ?? 'opencode';

/** Sensible defaults for the lab GLM-5.3-Flash deployment; every field is env-overridable. */
export function llmFromEnv(): LlmSettings {
  const effort = process.env.SURFACE_REASONING_EFFORT === 'high' ? 'high' : 'low';
  return {
    providerId: process.env.SURFACE_LLM_PROVIDER ?? 'glm',
    modelId: process.env.SURFACE_MODEL ?? 'GLM-5.3-Flash',
    displayName: process.env.SURFACE_MODEL_NAME ?? 'GLM-5.3-Flash (lab, Tailscale)',
    baseURL: process.env.SURFACE_LLM_BASE_URL ?? 'http://100.64.0.26:8003/v1',
    apiKey: process.env.SURFACE_LLM_API_KEY ?? 'unused',
    reasoningEffort: effort,
    contextLimit: Number(process.env.SURFACE_LLM_CONTEXT ?? 262144),
    outputLimit: Number(process.env.SURFACE_LLM_MAX_OUTPUT ?? 32768),
  };
}

export class OpencodeServer {
  readonly port: number;
  readonly configPath: string;
  readonly instructionsPath: string;
  private password = randomBytes(18).toString('base64url');
  private child: ChildProcess | null = null;
  private ready = false;
  private stopped = false;
  private listeners = new Map<string, Set<Listener>>(); // sessionID -> handlers
  private anyListeners = new Set<Listener>();
  private sseAbort: AbortController | null = null;
  private readyWaiters: Array<() => void> = [];

  private pidFile: string;

  constructor(private opts: OpencodeOptions) {
    this.port = Number(process.env.SURFACE_OPENCODE_PORT ?? opts.hubPort + 1);
    const dir = path.join(opts.hubDir, 'opencode');
    fs.mkdirSync(dir, { recursive: true });
    // Per hub port, so a second hub (e.g. a test run on another port) never
    // clobbers the config of the one you are actually using.
    this.configPath = path.join(dir, `opencode.${opts.hubPort}.json`);
    this.instructionsPath = path.join(dir, 'SURFACE.md');
    this.pidFile = path.join(dir, `server.${opts.hubPort}.pid`);
    this.writeConfig();
  }

  get model(): string {
    return `${this.opts.llm.providerId}/${this.opts.llm.modelId}`;
  }

  get isReady(): boolean {
    return this.ready;
  }

  // ---- config ------------------------------------------------------------

  /** MCP servers from the user's global config get switched off in Surface sessions. */
  private globalMcpNames(): string[] {
    const file = path.join(os.homedir(), '.config', 'opencode', 'opencode.json');
    try {
      const cfg = JSON.parse(fs.readFileSync(file, 'utf8'));
      return Object.keys(cfg.mcp ?? {});
    } catch {
      return [];
    }
  }

  private writeConfig(): void {
    const { llm } = this.opts;
    fs.writeFileSync(this.instructionsPath, this.opts.instructions);
    const mcp: Record<string, unknown> = {};
    for (const name of this.globalMcpNames()) mcp[name] = { enabled: false };
    mcp.surface = {
      type: 'local',
      command: [process.execPath, path.join(this.opts.rootDir, 'server', 'mcp-stdio.mjs')],
      environment: {
        SURFACE_HUB_URL: `http://127.0.0.1:${this.opts.hubPort}`,
        SURFACE_TOOLS_JSON: this.opts.toolsJsonPath,
      },
      enabled: true,
      timeout: 15000,
    };
    const config = {
      $schema: 'https://opencode.ai/config.json',
      model: this.model,
      small_model: this.model,
      share: 'disabled',
      autoupdate: false,
      instructions: [this.instructionsPath],
      // Headless: nothing can answer a prompt, so everything is pre-approved
      // and the interactive "question" tool is switched off.
      permission: { '*': 'allow', question: 'deny', skill: 'deny' },
      provider: {
        [llm.providerId]: {
          name: llm.displayName,
          npm: '@ai-sdk/openai-compatible',
          options: { baseURL: llm.baseURL, apiKey: llm.apiKey },
          models: {
            [llm.modelId]: {
              name: llm.modelId,
              tools: true,
              reasoning: true,
              attachment: true,
              modalities: { input: ['text', 'image'], output: ['text'] },
              limit: { context: llm.contextLimit, output: llm.outputLimit },
              // GLM-5.3-Flash MUST receive reasoning_effort (low|high); anything
              // else falls through to unbounded thinking and an empty answer.
              options: { reasoningEffort: llm.reasoningEffort },
              variants: { low: { reasoningEffort: 'low' }, high: { reasoningEffort: 'high' } },
            },
          },
        },
      },
      mcp,
    };
    fs.writeFileSync(this.configPath, JSON.stringify(config, null, 2));
  }

  /** Env for `opencode serve` — and, inherited, for every tool the agent runs. */
  private serverEnv(): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = { ...process.env, ...this.opts.extraEnv };
    for (const key of Object.keys(env)) {
      if (key.startsWith('CLAUDE_CODE_') || key === 'CLAUDECODE' || key === 'CLAUDE_SESSION_ID') delete env[key];
    }
    env.OPENCODE_CONFIG = this.configPath;
    env.OPENCODE_SERVER_PASSWORD = this.password;
    env.OPENCODE_SERVER_USERNAME = 'surface';
    env.OPENCODE_ENABLE_EXA = env.OPENCODE_ENABLE_EXA ?? '1'; // built-in websearch tool
    return env;
  }

  /** Shell line a human can use to take a Surface session over in a terminal. */
  takeoverCommand(sessionId: string, dir: string): string {
    return `cd ${JSON.stringify(dir)} && OPENCODE_CONFIG=${JSON.stringify(this.configPath)} ${OPENCODE_BIN} --session ${sessionId}`;
  }

  // ---- lifecycle ---------------------------------------------------------

  /**
   * A hub that died without cleanup (crash, hot-reload) leaves its opencode
   * child holding the port. The pidfile lets the next hub stop exactly that
   * process — nothing else on the machine is touched.
   */
  private stopPrevious(): void {
    let pid = 0;
    try {
      pid = Number(fs.readFileSync(this.pidFile, 'utf8'));
    } catch {
      return;
    }
    if (!pid || pid === process.pid) return;
    try {
      process.kill(pid, 0); // alive?
    } catch {
      fs.rmSync(this.pidFile, { force: true });
      return;
    }
    try {
      process.kill(pid, 'SIGTERM');
      this.opts.log('warn', `stopped an orphaned opencode server (pid ${pid}) left by a previous hub run`);
    } catch {
      /* already gone */
    }
    fs.rmSync(this.pidFile, { force: true });
  }

  start(): void {
    if (this.stopped) return;
    if (!this.child) this.stopPrevious();
    let child: ChildProcess;
    try {
      child = spawn(OPENCODE_BIN, ['serve', '--port', String(this.port), '--hostname', '127.0.0.1', '--pure'], {
        cwd: this.opts.rootDir,
        env: this.serverEnv(),
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      this.opts.log('error', `could not launch ${OPENCODE_BIN}: ${err}`);
      return;
    }
    this.child = child;
    if (child.pid) fs.writeFileSync(this.pidFile, String(child.pid));
    let tail = '';
    child.stderr?.on('data', (c) => {
      tail = (tail + String(c)).slice(-2000);
    });
    child.stdout?.on('data', () => {});
    child.on('error', (err) => this.opts.log('error', `opencode server error: ${err}`));
    child.on('close', (code) => {
      if (this.child === child) this.child = null;
      this.setReady(false);
      if (this.stopped) {
        fs.rmSync(this.pidFile, { force: true });
        return;
      }
      this.opts.log('warn', `opencode server exited (code ${code}) — restarting in 3s${tail.trim() ? ` · ${tail.trim().split('\n').slice(-2).join(' ').slice(0, 300)}` : ''}`);
      setTimeout(() => this.start(), 3000);
    });
    void this.waitHealthy();
  }

  private async waitHealthy(): Promise<void> {
    for (let i = 0; i < 60 && !this.stopped && this.child; i++) {
      try {
        const r = await fetch(`http://127.0.0.1:${this.port}/global/health`, { headers: this.headers() });
        if (r.ok) {
          const v = (await r.json()) as { version?: string };
          this.opts.log('info', `opencode ${v.version ?? ''} serving on 127.0.0.1:${this.port} · model ${this.model}`);
          this.setReady(true);
          this.subscribe();
          return;
        }
      } catch {
        /* not up yet */
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    if (this.child && !this.stopped) this.opts.log('error', 'opencode server did not become healthy in 30s');
  }

  private setReady(v: boolean): void {
    this.ready = v;
    if (v) {
      for (const w of this.readyWaiters.splice(0)) w();
    }
  }

  whenReady(): Promise<void> {
    if (this.ready) return Promise.resolve();
    return new Promise((r) => this.readyWaiters.push(r));
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.sseAbort?.abort();
    const child = this.child;
    if (!child) return;
    child.kill('SIGTERM');
    await new Promise<void>((resolve) => {
      const t = setTimeout(() => {
        child.kill('SIGKILL');
        resolve();
      }, 3000);
      child.on('close', () => {
        clearTimeout(t);
        resolve();
      });
    });
  }

  // ---- http --------------------------------------------------------------

  private headers(): Record<string, string> {
    return {
      Authorization: `Basic ${Buffer.from(`surface:${this.password}`).toString('base64')}`,
      'Content-Type': 'application/json',
    };
  }

  async request<T = unknown>(method: string, apiPath: string, dir: string, body?: unknown): Promise<T> {
    const url = `http://127.0.0.1:${this.port}${apiPath}${apiPath.includes('?') ? '&' : '?'}directory=${encodeURIComponent(dir)}`;
    const r = await fetch(url, { method, headers: this.headers(), body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await r.text();
    if (!r.ok) throw new Error(`opencode ${method} ${apiPath} → ${r.status}: ${text.slice(0, 300)}`);
    if (!text) return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      return text as unknown as T;
    }
  }

  // ---- events ------------------------------------------------------------

  on(sessionId: string, fn: Listener): () => void {
    let set = this.listeners.get(sessionId);
    if (!set) this.listeners.set(sessionId, (set = new Set()));
    set.add(fn);
    return () => {
      set!.delete(fn);
      if (set!.size === 0) this.listeners.delete(sessionId);
    };
  }

  onAny(fn: Listener): () => void {
    this.anyListeners.add(fn);
    return () => this.anyListeners.delete(fn);
  }

  private subscribe(): void {
    this.sseAbort?.abort();
    const ac = new AbortController();
    this.sseAbort = ac;
    void (async () => {
      while (!ac.signal.aborted && !this.stopped) {
        try {
          const r = await fetch(`http://127.0.0.1:${this.port}/global/event`, { headers: this.headers(), signal: ac.signal });
          if (!r.ok || !r.body) throw new Error(`event stream ${r.status}`);
          const reader = r.body.getReader();
          const dec = new TextDecoder();
          let buf = '';
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let i: number;
            while ((i = buf.indexOf('\n\n')) >= 0) {
              const frame = buf.slice(0, i);
              buf = buf.slice(i + 2);
              const data = frame
                .split('\n')
                .filter((l) => l.startsWith('data:'))
                .map((l) => l.slice(5).trim())
                .join('');
              if (data) this.dispatch(data);
            }
          }
        } catch (err) {
          if (ac.signal.aborted || this.stopped) return;
          this.opts.log('warn', `opencode event stream dropped (${String(err).slice(0, 120)}) — reconnecting`);
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
    })();
  }

  private dispatch(data: string): void {
    let raw: any;
    try {
      raw = JSON.parse(data);
    } catch {
      return;
    }
    const ev: OpencodeEvent = raw?.payload ?? raw;
    if (!ev || typeof ev.type !== 'string') return;
    const props = ev.properties ?? {};
    const sid: string | undefined = props.sessionID ?? props.info?.sessionID ?? props.part?.sessionID;
    for (const fn of this.anyListeners) fn(ev);
    if (sid) for (const fn of this.listeners.get(sid) ?? []) fn(ev);
  }
}
