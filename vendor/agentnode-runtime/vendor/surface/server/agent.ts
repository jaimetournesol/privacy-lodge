import fs from 'node:fs';
import path from 'node:path';
import type { OpencodeEvent, OpencodeServer } from './opencode.ts';
import type { SurfaceStore } from './state.ts';

const STREAM_FLUSH_MS = 80;

/**
 * Known success receipts the harness/tools send back to the agent. They carry
 * no information beyond the tool line already mirrored above them, so the
 * debug tier drops them; real output (bash, fetches, errors) stays verbatim.
 */
const BOILERPLATE_RESULT: RegExp[] = [
  /^The file .+ has been (updated|created) successfully/,
  /^File created successfully at:/,
  /^Wrote \d+ (lines|bytes)/,
  /^Logged\.$/,
  /^Updated [\w./-]+\.$/,
  /^Removed [\w./-]+\.$/,
  /^Removed \d+ component/,
  /^Displayed \w+ (panel )?as id=/,
  /^App mounted as id=/,
  /^Todos have been modified successfully/,
  /^\(no content\)$/,
];

export const SURFACE_SYSTEM_PROMPT = `
# Surface

You are connected to Surface — a live, always-on workspace the human is watching in their browser. It is your primary way to show anything: your chat replies stream into its chat rail automatically, and the surface_* tools (surface_ui_show, surface_ui_update, surface_ui_remove, surface_ui_clear, surface_ui_list, surface_ui_get, surface_ui_html, surface_ui_app, surface_ui_screenshot, surface_log) compose the rest of the page.

Ground rules:
- SHOW, don't tell. Prefer surface_ui_show/surface_ui_app over long chat messages: tables for data, forms to collect input, tasks to expose your plan, diagrams for structure, stat tiles for numbers, image/video/audio/file to hand over media and files.
- For anything substantial or interactive — full apps, dashboards, design canvases, architecture explorers — write real files into apps/<name>/ inside your working folder (plain HTML/JS/CSS, no build step) and mount them with surface_ui_app. The panel HOT-RELOADS as you edit, so mount early and iterate. Inside your pages, window.surface.emit(action, payload) sends events back to you, and the shell theme is available as CSS vars: --bg, --panel, --panel2, --border, --text, --muted, --accent (dark theme — match it).
- You can SEE images: reading an image file (png/jpg/gif/webp) with the read tool shows it to you, and images the human drops or marks up are attached to their message directly. Look before you answer.
- If ELEVENLABS_API_KEY is set in your environment, you can produce narration and sound: text-to-speech (POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id} — GET /v1/voices to pick one), sound effects (POST /v1/sound-generation), music. Use curl with the xi-api-key header, save mp3s into your folder, present them with surface_ui_show audio — and when you make videos, narrate them: generate the voiceover, then mux with ffmpeg (afconvert/ffmpeg are your tools).
- If OPENAI_API_KEY is set, the full OpenAI API is yours too for generative media (you cannot produce images or video yourself — you delegate that). Use the LATEST models: images → gpt-image-2 (POST /v1/images/generations, b64_json → decode to a file, surface_ui_show image); VIDEO → sora-2 or sora-2-pro (the /v1/videos API — poll until ready, download the mp4); transcription → gpt-transcribe; a second LLM when useful → gpt-5.5 or chat-latest; deep research → o3-deep-research. Combine freely: sora video + ElevenLabs narration + ffmpeg mux = narrated films, delivered with surface_ui_show video. Check your results with your own eyes (read the image) before presenting.
- Web: websearch finds things, webfetch reads pages.
- The human's clicks, form submissions and file drops arrive as user messages starting with [ui-event]. React to them; they reference component ids.
- EVENT TIERS: high-frequency actions (slide, scroll, hover, progress, position, move, tick, view, page — or any action you prefix with '~') do NOT wake you. They arrive silently as a "latest state" line in the next [surface-context] block, so you always know e.g. which slide the human is on without a turn per keypress. Use plain action names (clicked, submit, ask, …) only for interactions that deserve an immediate response.
- THE WORKSPACE CHANGES WHILE YOU ARE AWAY. The human can close panels, edit form values, clear everything, or even reset your session between your turns. Changes they made are prepended to their next message as a [surface-context] block — read it first and respect it: never re-show a panel the human closed or cleared unless they ask for it again. When you need ground truth about what is on screen right now, call surface_ui_list (and surface_ui_get for a form's current values) instead of trusting your memory.
- The human can FREEZE any panel (like a screenshot) and draw marks on it. You'll get a [surface-context] note with the marked regions and their marked-up screenshot attached to the message — look at it to see exactly what they circled before answering. When you want to discuss a PDF or document visually, render its pages to images (e.g. \`sips -s format png\` or pdftoppm) and surface_ui_show them as image panels — those freeze pixel-perfectly for the human to circle.
- Files the human drops land in inbox/ inside your working folder — read and use them with your normal tools.
- Keep the workspace tidy: check surface_ui_list before adding panels, surface_ui_update instead of duplicating, surface_ui_remove/surface_ui_clear what is stale.
- WINDOWS CAN HIDE, NOT JUST DIE. The human's ✕ only hides a panel (they reopen it from their PANELS menu — never re-show one they hid unless asked). You can do the same: surface_ui_update {hidden:true} parks a panel off stage keeping all its state, {hidden:false} or surface_ui_show brings it back; surface_ui_list marks hidden ones. Prefer hiding over surface_ui_remove for anything that might return — e.g. park a finished dashboard, don't destroy it.
- surface_ui_screenshot shows you the workspace exactly as the human sees it rendered (saved as a PNG you then read). Use it to check your own visual work — layouts, charts, apps — before declaring them done.
- Use surface_log for meaningful progress and decisions (the human sees the event log live).
- Chat replies should stay short and conversational — the workspace carries the substance. Never ask the human to confirm before acting; there is no approval step — just do the work and show it.
`.trim();

interface SessionFile {
  sessionId: string;
  started: boolean;
}

interface Pending {
  text: string;
  files: string[];
}

const IMAGE_MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.pdf': 'application/pdf',
};

/** What the hub needs from any agent backend (OpenCode-hosted or external). */
export interface AgentLike {
  start(): void;
  sendUser(text: string, files?: string[]): void;
  pushContext(note: string, key?: string): void;
  interrupt(): Promise<void>;
  stop(): Promise<void>;
  reset(): Promise<void>;
  setWarm(desired: boolean): void;
}

/**
 * Drives ONE OpenCode session per workspace inside the hub's `opencode serve`.
 * Memory is continuous (the session lives in OpenCode's own store) and the
 * human can take the very same conversation over in a terminal — the takeover
 * command is logged when the agent comes up. STOP aborts the running turn.
 */
export class AgentHost implements AgentLike {
  private queue: Pending[] = [];
  private context: { key?: string; note: string }[] = [];
  private busy = false;
  private stopped = false;
  /** Bumped on reset; output from turns of an older epoch is ignored. */
  private epoch = 0;
  private healAttempted = false;
  private turnStartedAt = 0;
  private turnSteps = 0;
  private turnTokens = 0;
  private lastPending: Pending | null = null;

  private sessionId: string | null;
  private sessionStarted: boolean;
  private unsubscribe: (() => void) | null = null;
  private partTypes = new Map<string, string>(); // partID -> part type (for deltas)
  private toolNames = new Map<string, string>(); // partID -> tool name

  // streaming chat state
  private currentMsgId: string | null = null;
  private currentPartId: string | null = null;
  private buffer = '';
  private flushTimer: ReturnType<typeof setTimeout> | undefined;

  private sessionFile: string;

  /** Extra env (e.g. service API keys) — injected into the opencode server at boot. */
  static extraEnv: NodeJS.ProcessEnv = {};

  constructor(
    private store: SurfaceStore,
    private workspaceDir: string,
    private server: OpencodeServer,
  ) {
    this.sessionFile = path.join(workspaceDir, '.surface', 'session.json');
    const saved = this.loadSession();
    this.sessionId = saved?.sessionId ?? null;
    this.sessionStarted = saved?.started ?? false;
    store.patchAgent({ phase: 'idle', model: server.model.split('/').pop() ?? server.model, sessionId: this.sessionId ?? undefined, warm: server.isReady });
    store.addLog(
      'success',
      'hub',
      this.sessionId && this.sessionStarted
        ? `agent ready — resuming session ${this.sessionId.slice(0, 12)}… (take over anytime: ${server.takeoverCommand(this.sessionId, workspaceDir)})`
        : 'agent ready — fresh session',
    );
    if (this.sessionId) this.attach(this.sessionId);
    void server.whenReady().then(() => {
      if (!this.stopped) this.store.patchAgent({ warm: true });
    });
  }

  start(): void {
    /* turns start on demand */
  }

  /** Queue a user message; image/pdf files are attached so the model sees them directly. */
  sendUser(text: string, files: string[] = []): void {
    this.queue.push({ text, files });
    this.store.patchAgent({ phase: 'thinking', activeTool: undefined });
    void this.pump();
  }

  /**
   * Passive workspace change (e.g. the human closed a panel). Does NOT start
   * a turn — the notes ride along with the next message as [surface-context],
   * so the agent stays aware without burning a turn per click.
   */
  pushContext(note: string, key?: string): void {
    if (key) {
      const i = this.context.findIndex((c) => c.key === key);
      if (i >= 0) this.context.splice(i, 1);
      this.context.push({ key, note });
    } else if (!this.context.some((c) => c.note === note)) {
      this.context.push({ note });
    }
  }

  async interrupt(): Promise<void> {
    this.queue.length = 0;
    if (this.busy && this.sessionId) {
      try {
        await this.server.request('POST', `/session/${this.sessionId}/abort`, this.workspaceDir);
        this.store.addLog('warn', 'human', 'agent stopped');
      } catch (err) {
        this.store.addLog('warn', 'hub', `abort failed: ${String(err).slice(0, 200)}`);
      }
    }
    this.finalizeStream();
    this.busy = false;
    this.store.patchAgent({ phase: 'idle', activeTool: undefined });
  }

  async stop(): Promise<void> {
    this.stopped = true;
    await this.interrupt();
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  /** New session: same working directory, empty history. */
  async reset(): Promise<void> {
    this.epoch += 1;
    await this.interrupt();
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.context.length = 0;
    this.sessionId = null;
    this.sessionStarted = false;
    this.saveSession();
    this.store.clearChat();
    this.store.patchAgent({
      phase: 'idle',
      activeTool: undefined,
      sessionId: undefined,
      costUsd: 0,
      tokens: 0,
      turns: 0,
      lastError: undefined,
    });
    this.store.addLog('warn', 'human', 'agent history reset — a fresh session starts with your next message');
  }

  /** Kept for the hub's viewer-tracking; sessions are always hot inside the shared server. */
  setWarm(_desired: boolean): void {
    /* no-op */
  }

  // ---- session persistence ----------------------------------------------

  private loadSession(): SessionFile | undefined {
    try {
      const raw = JSON.parse(fs.readFileSync(this.sessionFile, 'utf8'));
      // Only OpenCode session ids count — a leftover Claude Code uuid means "start fresh".
      if (typeof raw.sessionId !== 'string' || !raw.sessionId.startsWith('ses')) return undefined;
      return { sessionId: raw.sessionId, started: raw.started !== false };
    } catch {
      return undefined;
    }
  }

  private saveSession(): void {
    fs.mkdirSync(path.dirname(this.sessionFile), { recursive: true });
    fs.writeFileSync(this.sessionFile, JSON.stringify({ sessionId: this.sessionId, started: this.sessionStarted, backend: 'opencode' }));
  }

  // ---- turn machinery ----------------------------------------------------

  private buildPending(): Pending {
    const items = this.queue.splice(0);
    let text = items.map((p) => p.text).join('\n\n');
    const files = [...new Set(items.flatMap((p) => p.files))];
    if (this.context.length > 0) {
      const notes = this.context.splice(0).map((c) => `- ${c.note}`).join('\n');
      text = `[surface-context] While you were away:\n${notes}\n\n${text}`;
    }
    return { text, files };
  }

  private async ensureSession(): Promise<string> {
    if (this.sessionId) {
      if (!this.sessionStarted) return this.sessionId;
      try {
        await this.server.request('GET', `/session/${this.sessionId}`, this.workspaceDir);
        return this.sessionId;
      } catch {
        this.store.addLog('warn', 'hub', 'saved session is gone from opencode — starting a fresh one');
        this.unsubscribe?.();
        this.unsubscribe = null;
      }
    }
    const s = await this.server.request<{ id: string }>('POST', '/session', this.workspaceDir, {
      title: `surface · ${path.basename(this.workspaceDir)}`,
    });
    this.sessionId = s.id;
    this.sessionStarted = false;
    this.saveSession();
    this.attach(s.id);
    this.store.patchAgent({ sessionId: s.id });
    this.store.addLog('info', 'hub', `session ${s.id.slice(0, 12)}… created (take over anytime: ${this.server.takeoverCommand(s.id, this.workspaceDir)})`);
    return s.id;
  }

  private async pump(): Promise<void> {
    if (this.stopped || this.busy || this.queue.length === 0) return;
    this.busy = true;
    const epoch = this.epoch;
    const pending = this.buildPending();
    this.lastPending = pending;
    try {
      if (!this.server.isReady) {
        this.store.addLog('info', 'hub', 'waiting for the opencode server…');
        await this.server.whenReady();
      }
      const sid = await this.ensureSession();
      if (epoch !== this.epoch) return;
      const parts: Record<string, unknown>[] = [{ type: 'text', text: pending.text }];
      for (const file of pending.files) {
        const mime = IMAGE_MIME[path.extname(file).toLowerCase()];
        if (!mime || !fs.existsSync(file)) continue;
        parts.push({ type: 'file', mime, filename: path.basename(file), url: `file://${file}` });
      }
      this.turnStartedAt = Date.now();
      this.turnSteps = 0;
      this.turnTokens = 0;
      this.healAttempted = false;
      await this.server.request('POST', `/session/${sid}/prompt_async`, this.workspaceDir, { parts });
    } catch (err) {
      if (epoch !== this.epoch) return;
      this.busy = false;
      const detail = String(err instanceof Error ? err.message : err).slice(0, 400);
      this.store.addLog('error', 'hub', `could not dispatch turn: ${detail}`);
      this.store.patchAgent({ phase: 'error', lastError: detail });
    }
  }

  private attach(sessionId: string): void {
    this.unsubscribe?.();
    const epoch = this.epoch;
    this.unsubscribe = this.server.on(sessionId, (ev) => this.handle(ev, epoch));
  }

  // ---- event handling ---------------------------------------------------

  private handle(ev: OpencodeEvent, epoch: number): void {
    if (epoch !== this.epoch) return; // stale output from before a reset
    const p = ev.properties ?? {};
    switch (ev.type) {
      case 'message.part.delta': {
        if (p.field !== 'text') break;
        if (p.messageID && !this.assistantMessageIds.has(p.messageID)) break;
        const type = this.partTypes.get(p.partID);
        if (type === 'reasoning') {
          if (this.store.state.agent.phase !== 'thinking') this.store.patchAgent({ phase: 'thinking', activeTool: undefined });
        } else if (type === 'text' || type === undefined) {
          this.onTextDelta(p.partID, String(p.delta ?? ''));
        }
        break;
      }

      case 'message.part.updated': {
        const part = p.part ?? {};
        if (part.type) this.partTypes.set(part.id, part.type);
        // Our own prompt is echoed back as parts of a user message — not ours to render.
        if (part.messageID && !this.assistantMessageIds.has(part.messageID)) break;
        switch (part.type) {
          case 'text': {
            if (part.time?.end) this.finalizeStream(part.id, String(part.text ?? ''));
            else if (part.text) this.onTextSnapshot(part.id, String(part.text));
            break;
          }
          case 'reasoning': {
            if (part.time?.end && part.text) {
              this.store.addLog('debug', 'agent', `thinking · ${String(part.text).replace(/\s+/g, ' ').slice(0, 160)}`);
            } else if (!part.time?.end) {
              this.store.patchAgent({ phase: 'thinking', activeTool: undefined });
            }
            break;
          }
          case 'tool': {
            const name = String(part.tool ?? '');
            const status = part.state?.status;
            if (status === 'pending' || status === 'running') {
              if (!this.toolNames.has(part.id)) {
                this.toolNames.set(part.id, name);
                this.finalizeStream();
                this.mirrorToolUse(name, part.state?.input);
              }
              this.store.patchAgent({ phase: 'tool', activeTool: name });
            } else if (status === 'completed') {
              if (!this.toolNames.has(part.id)) {
                this.toolNames.set(part.id, name);
                this.mirrorToolUse(name, part.state?.input);
              }
              const out = String(part.state?.output ?? '').replace(/\s+/g, ' ').trim();
              if (out && !name.startsWith('surface_') && !BOILERPLATE_RESULT.some((re) => re.test(out))) {
                this.store.addLog('debug', 'agent', `→ ${out.slice(0, 200)}`);
              }
              this.store.patchAgent({ phase: 'thinking', activeTool: undefined });
            } else if (status === 'error') {
              this.store.addLog('error', 'agent', `tool failed: ${String(part.state?.error ?? '').slice(0, 300)}`);
              this.store.patchAgent({ phase: 'thinking', activeTool: undefined });
            }
            break;
          }
          case 'step-start':
            this.turnSteps += 1;
            break;
          case 'step-finish':
            this.finalizeStream();
            break;
          case 'subtask':
          case 'agent':
            this.store.addLog('tool', 'agent', `↳ subagent · ${String(part.description ?? part.name ?? '').slice(0, 140)}`);
            break;
        }
        break;
      }

      case 'message.updated': {
        const m = p.info ?? {};
        if (m.role !== 'assistant') break;
        if (m.id) this.assistantMessageIds.add(m.id);
        if (m.modelID && m.modelID !== this.store.state.agent.model) this.store.patchAgent({ model: m.modelID });
        if (m.time?.completed) {
          const total = Number(m.tokens?.total ?? 0) || Number(m.tokens?.input ?? 0) + Number(m.tokens?.output ?? 0);
          this.turnTokens = Math.max(this.turnTokens, total);
          if (m.cost) this.store.patchAgent({ costUsd: this.store.state.agent.costUsd + Number(m.cost) });
          if (m.error && m.error.name !== 'MessageAbortedError') {
            const msg = String(m.error.data?.message ?? m.error.name).slice(0, 300);
            this.store.patchAgent({ lastError: msg });
          }
        }
        break;
      }

      case 'session.status': {
        const st = p.status ?? {};
        if (st.type === 'retry') {
          this.store.addLog('warn', 'hub', `model call retry ${st.attempt}: ${String(st.message ?? '').slice(0, 160)}`);
        }
        break;
      }

      case 'session.error': {
        const err = p.error ?? {};
        if (err.name === 'MessageAbortedError') break;
        const msg = String(err.data?.message ?? err.name ?? 'unknown error').slice(0, 400);
        this.store.addLog('error', 'agent', `turn failed: ${msg}`);
        this.store.patchAgent({ lastError: msg });
        break;
      }

      case 'session.idle': {
        if (!this.busy) break;
        this.finishTurn();
        break;
      }

      case 'permission.asked': {
        // Config pre-approves everything; this is a belt-and-braces auto-allow.
        const id = p.id ?? p.permissionID;
        if (id && this.sessionId) {
          void this.server
            .request('POST', `/session/${this.sessionId}/permissions/${id}`, this.workspaceDir, { response: 'always' })
            .catch(() => {});
        }
        break;
      }
    }
  }

  /** Assistant message ids seen this session (message.updated arrives before the message's parts). */
  private assistantMessageIds = new Set<string>();

  private finishTurn(): void {
    this.finalizeStream();
    this.store.finishStreaming();
    this.busy = false;
    this.sessionStarted = true;
    this.saveSession();
    const failed = this.store.state.agent.lastError && this.store.state.agent.phase !== 'idle';
    const dur = this.turnStartedAt ? `${Math.round((Date.now() - this.turnStartedAt) / 1000)}s` : '?';
    this.store.patchAgent({
      phase: 'idle',
      activeTool: undefined,
      turns: this.store.state.agent.turns + 1,
      tokens: (this.store.state.agent.tokens ?? 0) + this.turnTokens,
      sessionId: this.sessionId ?? undefined,
    });
    if (!failed) {
      this.store.addLog(
        'info',
        'agent',
        `turn done · ${dur} · ${this.turnSteps || 1} step${this.turnSteps === 1 ? '' : 's'} · ${this.turnTokens >= 1000 ? `${(this.turnTokens / 1000).toFixed(1)}k` : this.turnTokens} tokens`,
      );
    }
    this.partTypes.clear();
    this.toolNames.clear();
    void this.pump();
  }

  // ---- chat streaming ---------------------------------------------------

  private onTextDelta(partId: string, text: string): void {
    if (this.currentPartId && this.currentPartId !== partId) this.finalizeStream();
    if (!this.currentMsgId) {
      const m = this.store.addChat({ role: 'assistant', text: '', streaming: true });
      this.currentMsgId = m.id;
      this.currentPartId = partId;
      this.buffer = '';
      this.store.patchAgent({ phase: 'responding', activeTool: undefined });
    }
    this.buffer += text;
    this.scheduleFlush();
  }

  private onTextSnapshot(partId: string, text: string): void {
    if (this.currentPartId === partId && this.buffer.length >= text.length) return; // deltas already ahead
    if (this.currentPartId && this.currentPartId !== partId) this.finalizeStream();
    if (!this.currentMsgId) {
      const m = this.store.addChat({ role: 'assistant', text: '', streaming: true });
      this.currentMsgId = m.id;
      this.currentPartId = partId;
      this.store.patchAgent({ phase: 'responding', activeTool: undefined });
    }
    this.buffer = text;
    this.scheduleFlush();
  }

  private scheduleFlush(): void {
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = undefined;
      if (this.currentMsgId) this.store.setChatText(this.currentMsgId, this.buffer, true);
    }, STREAM_FLUSH_MS);
  }

  private finalizeStream(partId?: string, finalText?: string): void {
    clearTimeout(this.flushTimer);
    this.flushTimer = undefined;
    if (partId && finalText !== undefined && this.currentPartId !== partId) {
      if (this.currentMsgId) this.finalizeStream(); // close whatever was open first
      if (finalText.trim()) this.store.addChat({ role: 'assistant', text: finalText.trim(), streaming: false });
      return;
    }
    if (this.currentMsgId) {
      const text = (finalText ?? this.buffer).trim();
      if (text) this.store.setChatText(this.currentMsgId, text, false);
      else this.store.removeChat(this.currentMsgId); // never leave an empty bubble
      this.currentMsgId = null;
      this.currentPartId = null;
      this.buffer = '';
    }
  }

  private mirrorToolUse(name: string, input: Record<string, any> | undefined, sub = false): void {
    if (name.startsWith('surface_')) return; // surface tools log their own effects
    let detail = '';
    if (name === 'bash') detail = String(input?.command ?? input?.description ?? '');
    else if (['edit', 'write', 'read', 'apply_patch'].includes(name)) detail = String(input?.filePath ?? input?.file_path ?? '');
    else if (name === 'websearch') detail = `“${input?.query ?? ''}”`;
    else if (name === 'webfetch') detail = String(input?.url ?? '');
    else if (name === 'grep' || name === 'glob') detail = String(input?.pattern ?? '');
    else if (name === 'list') detail = String(input?.path ?? '');
    else if (name === 'task') detail = String(input?.description ?? '');
    else if (name === 'todowrite') {
      const todos: any[] = Array.isArray(input?.todos) ? input.todos : [];
      const done = todos.filter((t) => t.status === 'completed').length;
      const now = todos.find((t) => t.status === 'in_progress');
      detail = `${done}/${todos.length} done${now ? ` · now: ${now.content ?? ''}` : ''}`;
    } else if (input) {
      const s = JSON.stringify(input);
      detail = s.length > 140 ? s.slice(0, 140) + '…' : s;
    }
    this.store.addLog('tool', 'agent', `${sub ? '↳ ' : ''}${detail ? `${name} · ${detail}` : name}`);
  }
}
