import fs from 'node:fs';
import { access, ticket, cookie, loginPage, trustedOrigin, canView, type Access } from './auth.ts';
import http from 'node:http';
import https from 'node:https';
import type { IncomingMessage, ServerResponse } from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { WebSocketServer, WebSocket } from 'ws';
import type { ClientMsg, FileEntry, ServerMsg } from '../shared/protocol.ts';
import { HUB_PORT } from '../shared/protocol.ts';
import { AgentHost, SURFACE_SYSTEM_PROMPT } from './agent.ts';
import { AssetHub, serveFile } from './assets.ts';
import { OpencodeServer, llmFromEnv } from './opencode.ts';
import { TOOL_DEFS } from './tools.ts';
import { WorkspaceManager } from './workspaces.ts';

const APP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = path.resolve(process.env.SURFACE_STATE_DIR ?? APP_ROOT);
const WEB_DIST = path.join(APP_ROOT, 'web', 'dist');
const HUB_DIR = path.join(ROOT, '.surface-hub');
const PORT = Number(process.env.HUB_PORT ?? HUB_PORT);

fs.mkdirSync(HUB_DIR, { recursive: true });

const toolsJsonPath = path.join(HUB_DIR, 'tools.json');
fs.writeFileSync(toolsJsonPath, JSON.stringify(TOOL_DEFS, null, 2));

// Service keys the agents may use (e.g. ELEVENLABS_API_KEY) live here — one
// place, easy to rotate, injected into every agent turn's environment.
try {
  const secrets = JSON.parse(fs.readFileSync(path.join(HUB_DIR, 'secrets.json'), 'utf8'));
  AgentHost.extraEnv = { ...AgentHost.extraEnv, ...secrets };
} catch {
  /* no secrets configured */
}

// ---- the agent runtime: one headless opencode server, GLM behind it -----
// (or none at all: SURFACE_AGENT=external hands the conversation to another
// process — see external-agent.ts — and the hub only owns panels.)

const EXTERNAL = process.env.SURFACE_AGENT === 'external';
const HOST = process.env.HUB_HOST ?? '127.0.0.1';

const opencode: OpencodeServer | null = EXTERNAL ? null : new OpencodeServer({
  rootDir: ROOT,
  hubDir: HUB_DIR,
  hubPort: PORT,
  toolsJsonPath,
  instructions: SURFACE_SYSTEM_PROMPT,
  llm: llmFromEnv(),
  extraEnv: AgentHost.extraEnv,
  log: (level, message) => {
    console.log(`[opencode] ${message}`);
    mgr?.forEachActive((_id, ws) => ws.store.addLog(level, 'hub', message));
  },
});

// ---- socket bookkeeping -------------------------------------------------

const permissions = new Map<WebSocket,Access>();
const sockets = new Map<WebSocket, string>(); // socket -> bound workspace id
export interface Viewport { width: number; height: number; dpr: number }
const viewports = new Map<WebSocket, Viewport>(); // socket -> its workspace area size

function viewersOf(wsId: string): Viewport[] {
  const out: Viewport[] = [];
  for (const [sock, bound] of sockets) {
    if (bound !== wsId || sock.readyState !== WebSocket.OPEN) continue;
    const v = viewports.get(sock);
    if (v) out.push(v);
  }
  return out;
}

function sendTo(ws: WebSocket, msg: ServerMsg): void {
  const grant=permissions.get(ws);
  if(grant?.exp&&grant.exp<=Date.now()/1000){ws.close(4401);return;}
  if(grant?.role==='presenter'){
    if(msg.type==='workspaces')msg={...msg,items:msg.items.filter(w=>canView(grant,w.id)).map(w=>({...w,dir:''}))};
    if(msg.type==='snapshot')msg={...msg,workspace:{...msg.workspace,dir:''},state:{...msg.state,chat:[],log:[],agent:{phase:'idle',model:'',costUsd:0,turns:0}}};
    if(msg.type==='patch')msg={...msg,ops:msg.ops.filter(op=>/^\/(components|view)(\/|$)/.test(op.path))};
    if(msg.type==='capture-request')return;
    if(msg.type==='ws-activity'&&!canView(grant,msg.id))return;
  }
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function broadcastToWs(wsId: string, msg: ServerMsg): void {
  for (const [sock, bound] of sockets) if (bound === wsId) sendTo(sock, msg);
}

function broadcastAll(msg: ServerMsg): void {
  for (const sock of sockets.keys()) sendTo(sock, msg);
}

// ---- assets & workspaces ------------------------------------------------

const assets = new AssetHub(
  (wsId) => {
    const ws = mgr.get(wsId);
    return ws ? path.join(ws.meta.dir, 'inbox') : null;
  },
  (wsId, componentId) => broadcastToWs(wsId, { type: 'app-reload', componentId }),
  (wsId, file) => {
    const ws = mgr.get(wsId);
    if (!ws) return;
    const asset = assets.registerAsset(file.path);
    ws.store.upsertComponent({
      id: `inbox-${file.name.replace(/[^\w.-]/g, '_')}`,
      type: 'file',
      title: 'received file',
      region: 'side',
      props: { path: file.path, src: asset.url, name: file.name, size: file.size, mime: file.mime },
    });
    ws.store.addLog('info', 'human', `dropped file ${file.name} (${(file.size / 1024).toFixed(1)} KB)`);
    ws.agent.sendUser(
      `[ui-event] ${JSON.stringify({ action: 'file-upload', payload: { path: file.path, name: file.name, size: file.size, mime: file.mime } })}`,
      file.mime.startsWith('image/') || file.mime === 'application/pdf' ? [file.path] : [],
    );
  },
);

// ---- agent phase → home-screen badges & background-turn toasts ----------

const lastPhase = new Map<string, string>();
const BUSY = new Set(['thinking', 'responding', 'tool']);
let wsListTimer: ReturnType<typeof setTimeout> | undefined;

function onPhase(wsId: string, phase: string): void {
  const prev = lastPhase.get(wsId);
  lastPhase.set(wsId, phase);
  // Rebroadcast the enriched workspace list (debounced) so badges stay live.
  clearTimeout(wsListTimer);
  wsListTimer = setTimeout(() => broadcastAll({ type: 'workspaces', items: mgr.list() }), 250);
  // A turn just finished (or failed) — let every client decide whether to toast.
  if (prev && BUSY.has(prev) && (phase === 'idle' || phase === 'error')) {
    const info = mgr.info(wsId);
    if (info) broadcastAll({ type: 'ws-activity', id: wsId, name: info.name, phase: phase as never });
  }
}

// ---- agent self-sight: ask a viewing browser to rasterize the workspace --

const pendingCaptures = new Map<string, { resolve: (dataUrl: string) => void; reject: (err: Error) => void }>();

function captureWorkspace(wsId: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const viewer = [...sockets.entries()].find(([sock, bound]) => bound === wsId && sock.readyState === WebSocket.OPEN);
    if (!viewer) {
      reject(new Error('no browser is currently viewing this workspace'));
      return;
    }
    const reqId = `cap-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    const timer = setTimeout(() => {
      pendingCaptures.delete(reqId);
      reject(new Error('capture timed out'));
    }, 12000);
    pendingCaptures.set(reqId, {
      resolve: (dataUrl) => {
        clearTimeout(timer);
        pendingCaptures.delete(reqId);
        const ws = mgr.get(wsId);
        if (!ws || !dataUrl.startsWith('data:image/png;base64,')) {
          reject(new Error('capture failed'));
          return;
        }
        const dir = path.join(ws.meta.dir, '.surface', 'screens');
        fs.mkdirSync(dir, { recursive: true });
        const file = path.join(dir, `screen-${Date.now()}.png`);
        fs.writeFileSync(file, Buffer.from(dataUrl.slice('data:image/png;base64,'.length), 'base64'));
        resolve(file);
      },
      reject: (err) => {
        clearTimeout(timer);
        pendingCaptures.delete(reqId);
        reject(err);
      },
    });
    sendTo(viewer[0], { type: 'capture-request', reqId });
  });
}

const mgr: WorkspaceManager = new WorkspaceManager(ROOT, opencode, assets, broadcastToWs, onPhase, captureWorkspace, viewersOf);
opencode?.start();

import { presentationEvent } from './presentation.ts';

/** ui-event actions that are context, not conversation. */
const PASSIVE_ACTIONS = new Set(['slide', 'scroll', 'hover', 'progress', 'position', 'move', 'tick', 'view', 'page']);

// ---- http helpers -------------------------------------------------------

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

function json(res: ServerResponse, status: number, value: unknown): void {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(value));
}

/** Resolve a user-supplied relative path inside a workspace dir, or null. */
function safeJoin(baseDir: string, rel: string): string | null {
  const abs = path.resolve(baseDir, rel.replace(/^\/+/, ''));
  return abs === baseDir || abs.startsWith(baseDir + path.sep) ? abs : null;
}

// ---- http ---------------------------------------------------------------

const requestHandler = (req: IncomingMessage, res: ServerResponse) => {
  void (async () => {
    const requestUrl=new URL(req.url??'/','http://local');
    res.setHeader('Referrer-Policy','no-referrer');
    res.setHeader('Cache-Control','no-store');
    if(requestUrl.pathname==='/healthz'){json(res,200,{ok:true});return;}
    // Public runtime code carries no workspace data; sandboxed srcdoc frames
    // have opaque origins and must be able to load this bootstrap script.
    if(requestUrl.pathname==='/__surface/bridge.js'&&req.method==='GET'){assets.handle(req,res);return;}
    if(requestUrl.pathname==='/api/auth/session'&&req.method==='POST'){
      const value=String(req.headers['x-agentnode-token']??'');
      if(!trustedOrigin(req)||!ticket(value)){json(res,401,{error:'unauthorized'});return;}
      res.setHeader('Set-Cookie',cookie(value));json(res,200,{ok:true});return;
    }
    const grant=access(req);
    if(!grant){
      if(requestUrl.pathname==='/'&&req.method==='GET'){res.writeHead(200,{'Content-Type':'text/html'}).end(loginPage);return;}
      json(res,401,{error:'unauthorized'});return;
    }
    if(grant.role==='presenter'){
      const p=requestUrl.pathname;
      if(!['GET','HEAD'].includes(req.method??'')){json(res,403,{error:'Presentation is read-only'});return;}
      if(p==='/api/workspaces'){json(res,200,{items:mgr.list().filter(w=>canView(grant,w.id)).map(w=>({...w,dir:''}))});return;}
      if(p.startsWith('/api/')||p.startsWith('/internal/')||p.startsWith('/debug/')){json(res,403,{error:'Presentation is read-only'});return;}
      if(p.startsWith('/assets/')||p.startsWith('/apps/')){
        const namespace=p.split('/').slice(0,3).join('/')+'/';
        const allowed=(grant.ws??[]).some(id=>JSON.stringify(mgr.get(id)?.store.state.components??[]).includes(namespace));
        if(!allowed){json(res,403,{error:'Asset is outside this presentation'});return;}
      }
    }
    if (assets.handle(req, res)) return;

    const url = new URL(req.url ?? '/', 'http://local');
    const p = url.pathname;

    if (p === '/healthz') {
      json(res, 200, { ok: true, workspaces: mgr.list().length });
      return;
    }

    // -- workspace CRUD --
    if (p === '/api/workspaces' && req.method === 'GET') {
      json(res, 200, { items: mgr.list() });
      return;
    }
    if (p === '/api/workspaces' && req.method === 'POST') {
      try {
        const body = JSON.parse(await readBody(req));
        const info = body.path ? mgr.attach(String(body.path)) : mgr.create(String(body.name ?? ''));
        broadcastAll({ type: 'workspaces', items: mgr.list() });
        json(res, 200, info);
      } catch (err) {
        json(res, 400, { error: String(err instanceof Error ? err.message : err) });
      }
      return;
    }
    const wsMatch = /^\/api\/workspaces\/([^/]+)$/.exec(p);
    if (wsMatch && req.method === 'PATCH') {
      try {
        const body = JSON.parse(await readBody(req));
        const info = await mgr.rename(wsMatch[1], String(body.name ?? ''));
        broadcastAll({ type: 'workspaces', items: mgr.list() });
        json(res, 200, info);
      } catch (err) {
        json(res, 400, { error: String(err instanceof Error ? err.message : err) });
      }
      return;
    }
    if (wsMatch && req.method === 'DELETE') {
      const id = wsMatch[1];
      await mgr.remove(id);
      const fallback = mgr.defaultId();
      for (const [sock, bound] of sockets) {
        if (bound === id) {
          if (fallback) {
            sockets.set(sock, fallback);
            sendSnapshot(sock, fallback);
          } else {
            sock.close();
          }
        }
      }
      broadcastAll({ type: 'workspaces', items: mgr.list() });
      json(res, 200, { ok: true });
      return;
    }

    // -- file browsing --
    if (p === '/api/files' && req.method === 'GET') {
      const ws = mgr.get(url.searchParams.get('ws') ?? '');
      const abs = ws && safeJoin(ws.meta.dir, url.searchParams.get('path') ?? '');
      if (!ws || !abs || !fs.existsSync(abs)) {
        json(res, 404, { error: 'not found' });
        return;
      }
      const entries: FileEntry[] = fs
        .readdirSync(abs, { withFileTypes: true })
        .filter((d) => d.name !== '.surface' && d.name !== '.DS_Store')
        .map((d) => {
          const st = fs.statSync(path.join(abs, d.name));
          return {
            name: d.name,
            kind: d.isDirectory() ? ('dir' as const) : ('file' as const),
            size: st.size,
            mtime: st.mtimeMs,
          };
        })
        .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'dir' ? -1 : 1));
      json(res, 200, { entries });
      return;
    }
    if (p === '/api/file' && req.method === 'GET') {
      const ws = mgr.get(url.searchParams.get('ws') ?? '');
      const abs = ws && safeJoin(ws.meta.dir, url.searchParams.get('path') ?? '');
      if (!ws || !abs) {
        res.writeHead(404).end('not found');
        return;
      }
      serveFile(req, res, abs);
      return;
    }

    // -- per-workspace instructions (CLAUDE.md in the folder; opencode reads it natively) --
    if (p === '/api/instructions') {
      const ws = mgr.get(url.searchParams.get('ws') ?? '');
      if (!ws) {
        json(res, 404, { error: 'unknown workspace' });
        return;
      }
      const file = path.join(ws.meta.dir, 'CLAUDE.md');
      if (req.method === 'GET') {
        json(res, 200, { text: fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '' });
        return;
      }
      if (req.method === 'PUT') {
        const body = JSON.parse(await readBody(req));
        fs.writeFileSync(file, String(body.text ?? ''));
        ws.store.addLog('info', 'human', 'updated workspace instructions (CLAUDE.md)');
        json(res, 200, { ok: true });
        return;
      }
    }

    // -- capture responses from viewing browsers --
    if (p === '/internal/capture' && req.method === 'POST') {
      try {
        const { reqId, dataUrl, error } = JSON.parse(await readBody(req));
        const pending = pendingCaptures.get(String(reqId));
        if (pending) {
          if (error) pending.reject(new Error(String(error)));
          else pending.resolve(String(dataUrl ?? ''));
        }
        json(res, 200, { ok: true });
      } catch (err) {
        json(res, 400, { error: String(err) });
      }
      return;
    }

    // -- spoken replies: ElevenLabs TTS proxied so the key stays server-side --
    if (p === '/api/tts' && req.method === 'POST') {
      const key = AgentHost.extraEnv.ELEVENLABS_API_KEY ?? process.env.ELEVENLABS_API_KEY;
      if (!key) {
        json(res, 400, { error: 'no ELEVENLABS_API_KEY configured' });
        return;
      }
      try {
        const { text } = JSON.parse(await readBody(req));
        const clean = String(text ?? '')
          .replace(/```[\s\S]*?```/g, ' (code) ')
          .replace(/[*_#`>|]/g, '')
          .slice(0, 900)
          .trim();
        if (!clean) throw new Error('nothing to speak');
        const voice = process.env.SURFACE_TTS_VOICE ?? '21m00Tcm4TlvDq8ikWAM';
        const r = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${voice}?output_format=mp3_44100_128`, {
          method: 'POST',
          headers: { 'xi-api-key': key, 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: clean, model_id: 'eleven_multilingual_v2' }),
        });
        if (!r.ok) throw new Error(`elevenlabs ${r.status}: ${(await r.text()).slice(0, 200)}`);
        const buf = Buffer.from(await r.arrayBuffer());
        res.writeHead(200, { 'Content-Type': 'audio/mpeg', 'Content-Length': buf.length });
        res.end(buf);
      } catch (err) {
        json(res, 500, { error: String(err instanceof Error ? err.message : err) });
      }
      return;
    }

    // -- external agent reports its phase / cost / session (SURFACE_AGENT=external) --
    if (p === '/internal/agent' && req.method === 'POST') {
      try {
        const { ws: wsId, phase, activeTool, model, sessionId, costUsd, tokens, turns } = JSON.parse(await readBody(req));
        const ws = mgr.get(String(wsId ?? ''));
        if (!ws) throw new Error(`unknown workspace: ${wsId}`);
        ws.store.patchAgent({
          ...(phase ? { phase } : {}),
          activeTool: activeTool || undefined,
          ...(model ? { model } : {}),
          ...(sessionId !== undefined ? { sessionId: sessionId || undefined } : {}),
          ...(typeof costUsd === 'number' ? { costUsd } : {}),
          ...(typeof tokens === 'number' ? { tokens } : {}),
          ...(typeof turns === 'number' ? { turns } : {}),
        });
        json(res, 200, { ok: true });
      } catch (err) {
        json(res, 400, { error: String(err instanceof Error ? err.message : err) });
      }
      return;
    }

    // -- external agent mirrors chat into the store (so the full shell shows it too) --
    if (p === '/internal/chat' && req.method === 'POST') {
      try {
        const { ws: wsId, role, text } = JSON.parse(await readBody(req));
        const ws = mgr.get(String(wsId ?? ''));
        if (!ws) throw new Error(`unknown workspace: ${wsId}`);
        ws.store.addChat({ role: role === 'assistant' ? 'assistant' : 'user', text: String(text ?? '') });
        json(res, 200, { ok: true });
      } catch (err) {
        json(res, 400, { error: String(err instanceof Error ? err.message : err) });
      }
      return;
    }

    // -- surface tool execution (from the per-turn stdio MCP proxy) --
    if (p === '/internal/tool' && req.method === 'POST') {
      try {
        const { ws: wsId, dir, name, args } = JSON.parse(await readBody(req));
        // The MCP proxy identifies its workspace by the folder opencode started it in.
        const ws = (wsId ? mgr.get(String(wsId)) : undefined) ?? (dir ? mgr.byDir(String(dir)) : undefined);
        if (!ws) throw new Error(`unknown workspace: ${wsId ?? dir}`);
        const text = await ws.runTool(name, args ?? {});
        json(res, 200, { text });
      } catch (err) {
        json(res, 400, { error: String(err instanceof Error ? err.message : err) });
      }
      return;
    }

    // Dev helper: inject a component without spending an agent turn.
    if (p === '/debug/show' && req.method === 'POST') {
      try {
        const ws = mgr.get(url.searchParams.get('ws') ?? mgr.defaultId() ?? '');
        if (!ws) throw new Error('no workspace');
        const { type, props, title, region, id } = JSON.parse(await readBody(req));
        const comp = ws.store.upsertComponent({ id, type, props, title, region });
        json(res, 200, { id: comp.id });
      } catch (err) {
        json(res, 400, { error: String(err) });
      }
      return;
    }

    // Production: serve the built shell.
    if (fs.existsSync(WEB_DIST)) {
      const rel = p === '/' ? 'index.html' : p.slice(1);
      const abs = path.resolve(WEB_DIST, rel);
      if (abs.startsWith(WEB_DIST) && fs.existsSync(abs) && fs.statSync(abs).isFile()) {
        serveFile(req, res, abs);
        return;
      }
      serveFile(req, res, path.join(WEB_DIST, 'index.html'));
      return;
    }

    res.writeHead(404).end('surface hub — run the web shell via `npm run dev`');
  })().catch((err) => {
    if (!res.headersSent) json(res, 500, { error: String(err) });
  });
};

const server = http.createServer(requestHandler);

// Optional HTTPS twin (HUB_TLS_CERT/HUB_TLS_KEY): needed when the shell is
// embedded in an HTTPS page — browsers refuse http iframes there.
const TLS_PORT = Number(process.env.HUB_TLS_PORT ?? PORT + 43);
let tlsServer: https.Server | null = null;
if (process.env.HUB_TLS_CERT && process.env.HUB_TLS_KEY) {
  try {
    tlsServer = https.createServer(
      { cert: fs.readFileSync(process.env.HUB_TLS_CERT), key: fs.readFileSync(process.env.HUB_TLS_KEY) },
      requestHandler,
    );
  } catch (err) {
    console.log(`[hub] tls disabled: ${String(err)}`);
  }
}

// ---- websocket ----------------------------------------------------------

function sendSnapshot(sock: WebSocket, wsId: string): void {
  const grant=permissions.get(sock);if(grant&&!canView(grant,wsId)){sock.close(4403);return;}
  const ws = mgr.get(wsId);
  const info = mgr.info(wsId);
  if (ws && info) sendTo(sock, { type: 'snapshot', state: ws.store.state, workspace: info });
}

const onConnection = (sock: WebSocket, req: IncomingMessage): void => {
  const url = new URL(req.url ?? '/', 'http://local');
  const grant=access(req);
  if(!grant){sock.close(4401);return;}
  permissions.set(sock,grant);
  const requested = url.searchParams.get('ws');
  const wsId = (requested && mgr.info(requested) ? requested : mgr.defaultId()) ?? '';
  if(!canView(grant,wsId)){sock.close(4403);return;}
  sockets.set(sock, wsId);
  sendTo(sock, { type: 'workspaces', items: mgr.list() });
  if (wsId) sendSnapshot(sock, wsId);

  sock.on('message', (raw) => {
    if(grant.exp&&grant.exp<=Date.now()/1000){sock.close(4401);return;}
    let msg: ClientMsg;
    try {
      msg = JSON.parse(String(raw));
    } catch {
      return;
    }

    if (msg.type === 'switch-workspace') {
      if (canView(grant,msg.id)&&mgr.info(msg.id)) {
        sockets.set(sock, msg.id);
        sendSnapshot(sock, msg.id);
      }
      return;
    }

    if(grant.exp&&grant.exp<=Date.now()/1000){sock.close(4401);return;}
    if(grant.role==='presenter'&&!['resync','viewport'].includes(msg.type))return;
    const boundId = sockets.get(sock) ?? '';
    const ws = mgr.get(boundId);
    if (!ws) return;
    const { store, agent, meta } = ws;

    switch (msg.type) {
      case 'chat': {
        const text = msg.text.trim();
        if (!text) return;
        const files: string[] = [];
        for (const ann of msg.annotations ?? []) {
          const boxes = JSON.stringify(ann.marks);
          let note =
            `the human froze the ${ann.componentType} panel “${ann.title ?? ann.componentId}” (id=${ann.componentId}) ` +
            `and circled ${ann.marks.length} region(s) — normalized {x,y,w,h} boxes: ${boxes}`;
          if (ann.imageDataUrl?.startsWith('data:image/png;base64,')) {
            const file = path.join(
              meta.dir,
              'inbox',
              `annotation-${Date.now()}-${ann.componentId.replace(/[^\w-]/g, '_')}.png`,
            );
            fs.writeFileSync(file, Buffer.from(ann.imageDataUrl.slice('data:image/png;base64,'.length), 'base64'));
            note += `; their marked-up screenshot (${file}) is attached to this message — look at it to see exactly what they mean`;
            files.push(file);
          }
          agent.pushContext(note);
          store.addLog('info', 'human', `marked up panel ${ann.componentId} (${ann.marks.length} region${ann.marks.length === 1 ? '' : 's'})`);
        }
        store.addChat({
          role: 'user',
          text,
          attachments: msg.annotations?.length
            ? msg.annotations.map((a) => ({ name: `✎ ${a.title ?? a.componentType}`, size: a.marks.length }))
            : undefined,
        });
        agent.sendUser(text, files);
        break;
      }
      case 'ui-arrange': {
        store.arrange(msg.main, msg.side);
        agent.pushContext('the human rearranged the workspace panels — ui_list shows the current layout');
        break;
      }
      case 'ui-event': {
        const scope=url.searchParams.get('presentation')??'default';
        if(!/^[A-Za-z0-9_-]{1,100}$/.test(scope))return;
        if((msg.action.startsWith('__presentation-')||msg.action==='__workspace-focus')&&url.searchParams.get('view')!=='control')return;
        if (presentationEvent(store, msg.componentId, msg.action, msg.payload, (note, key) => agent.pushContext(note, key),scope)) return;
        if (msg.action === '__ready') return;
        // High-frequency events never start a turn — they ride into the next
        // prompt as a "latest state" [surface-context] line instead.
        const passiveName = msg.action.replace(/^~/, '');
        if (msg.action.startsWith('~') || PASSIVE_ACTIONS.has(msg.action)) {
          const comp = store.getComponent(msg.componentId);
          const payload = msg.payload === undefined ? '' : ` — ${JSON.stringify(msg.payload).slice(0, 200)}`;
          agent.pushContext(
            `latest “${passiveName}” on the ${comp?.type ?? 'unknown'} panel “${comp?.title ?? msg.componentId}” (id=${msg.componentId})${payload}`,
            `${msg.componentId}:${passiveName}`,
          );
          store.addLog('debug', 'human', `${passiveName} on ${msg.componentId}`);
          return;
        }
        if (msg.action === 'resize') {
          // Presentation-only: sync the human's panel height silently.
          const payload = msg.payload as { height?: number } | undefined;
          if (typeof payload?.height === 'number' && payload.height >= 120) {
            store.patchComponentProps(msg.componentId, { height: Math.round(payload.height) });
          }
          return;
        }
        if (msg.action === 'change') {
          const payload = msg.payload as { values?: Record<string, unknown> } | undefined;
          if (payload?.values) {
            store.patchComponentProps(msg.componentId, { values: payload.values });
            const comp = store.getComponent(msg.componentId);
            agent.pushContext(
              `the human edited values in the form “${comp?.title ?? msg.componentId}” (id=${msg.componentId}) — ui_get shows the current values`,
              `${msg.componentId}:change`,
            );
          }
          return;
        }
        if (msg.action === 'dismiss') {
          // Soft close: hidden, not deleted — reopenable from the PANELS menu.
          const comp = store.getComponent(msg.componentId);
          store.setHidden(msg.componentId, true);
          store.addLog('info', 'human', `hid panel ${msg.componentId}`);
          agent.pushContext(
            `the human hid the ${comp?.type ?? 'unknown'} panel “${comp?.title ?? msg.componentId}” (id=${msg.componentId}) — they can reopen it themselves; don't re-show it unasked`,
            `${msg.componentId}:visibility`,
          );
          return;
        }
        if (msg.action === 'restore') {
          const comp = store.getComponent(msg.componentId);
          store.setHidden(msg.componentId, false);
          store.addLog('info', 'human', `reopened panel ${msg.componentId}`);
          agent.pushContext(
            `the human reopened the ${comp?.type ?? 'unknown'} panel “${comp?.title ?? msg.componentId}” (id=${msg.componentId})`,
            `${msg.componentId}:visibility`,
          );
          return;
        }
        if (msg.action === 'discard') {
          const comp = store.getComponent(msg.componentId);
          store.removeComponent(msg.componentId);
          store.addLog('info', 'human', `deleted panel ${msg.componentId}`);
          agent.pushContext(
            `the human permanently deleted the ${comp?.type ?? 'unknown'} panel “${comp?.title ?? msg.componentId}” (id=${msg.componentId}) — don't bring it back unasked`,
          );
          return;
        }
        store.addLog('info', 'human', `${msg.action} on ${msg.componentId}`);
        agent.sendUser(
          `[ui-event] ${JSON.stringify({ componentId: msg.componentId, action: msg.action, payload: msg.payload })}`,
        );
        break;
      }
      case 'viewport': {
        const w = Math.round(Number(msg.width)), h = Math.round(Number(msg.height));
        if (!(w > 0 && h > 0)) return;
        const prev = viewports.get(sock);
        viewports.set(sock, { width: w, height: h, dpr: Number(msg.dpr) || 1 });
        // Tell the agent once per meaningful change (not per pixel while the window is dragged).
        if (!prev || Math.abs(prev.width - w) > 40 || Math.abs(prev.height - h) > 40) {
          const all = viewersOf(boundId);
          agent.pushContext(
            `the human's workspace area is ${w}×${h} px${all.length > 1 ? ` (${all.length} viewers: ${all.map((v) => `${v.width}×${v.height}`).join(', ')})` : ''} — size panels to fit it (height "fill" for one-panel layouts)`,
            'viewport',
          );
        }
        break;
      }
      case 'interrupt': {
        void agent.interrupt();
        break;
      }
      case 'clear-workspace': {
        const n = store.clearComponents();
        store.addLog('info', 'human', `cleared the workspace (${n} component${n === 1 ? '' : 's'})`);
        if (n > 0) agent.pushContext(`the human cleared the whole workspace (${n} panels removed) — don't restore them unasked`);
        break;
      }
      case 'reset-agent': {
        void agent.reset();
        break;
      }
      case 'resync': {
        sendSnapshot(sock, boundId);
        break;
      }
    }
  });

  sock.on('close', () => {
    permissions.delete(sock);
    sockets.delete(sock);
    viewports.delete(sock);
  });
};

new WebSocketServer({ server, path: '/ws', verifyClient:(info:{req:IncomingMessage})=>!!access(info.req) }).on('connection', onConnection);
if (tlsServer) new WebSocketServer({ server: tlsServer, path: '/ws', verifyClient:(info:{req:IncomingMessage})=>!!access(info.req) }).on('connection', onConnection);

// ---- boot ---------------------------------------------------------------

server.listen(PORT, HOST, () => {
  console.log(`surface hub → http://${HOST}:${PORT} · ${mgr.list().length} workspace(s)${EXTERNAL ? ' · external agent mode' : ''}`);
});
tlsServer?.listen(TLS_PORT, HOST, () => console.log(`surface hub → https://${HOST}:${TLS_PORT}`));

for (const sig of ['SIGINT', 'SIGTERM'] as const) {
  process.on(sig, async () => {
    await mgr.stopAll();
    await opencode?.stop();
    process.exit(0);
  });
}
