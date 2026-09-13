import { PRESENTATION_BRIDGE_JS } from './presentation.ts';
import chokidar, { type FSWatcher } from 'chokidar';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import type { IncomingMessage, ServerResponse } from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const CAPTURE_LIB_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  'node_modules/modern-screenshot/dist/index.js', // UMD → window.modernScreenshot
);

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.htm': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.avif': 'image/avif',
  '.ico': 'image/x-icon',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
  '.mov': 'video/quicktime',
  '.mp3': 'audio/mpeg',
  '.wav': 'audio/wav',
  '.m4a': 'audio/mp4',
  '.ogg': 'audio/ogg',
  '.pdf': 'application/pdf',
  '.txt': 'text/plain; charset=utf-8',
  '.md': 'text/plain; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8',
  '.woff2': 'font/woff2',
  '.wasm': 'application/wasm',
};

export function mimeFor(p: string): string {
  return MIME[path.extname(p).toLowerCase()] ?? 'application/octet-stream';
}

/** Streams a file with HTTP Range support (video/audio scrubbing). */
export function serveFile(req: IncomingMessage, res: ServerResponse, absPath: string): void {
  let stat: fs.Stats;
  try {
    stat = fs.statSync(absPath);
  } catch {
    res.writeHead(404).end('not found');
    return;
  }
  if (!stat.isFile()) {
    res.writeHead(404).end('not found');
    return;
  }
  const mime = mimeFor(absPath);
  const range = req.headers.range;
  if (range) {
    const m = /^bytes=(\d*)-(\d*)$/.exec(range);
    if (m && (m[1] || m[2])) {
      let start: number;
      let end: number;
      if (!m[1]) {
        // Suffix range: "bytes=-N" = the LAST N bytes (how browsers fetch a
        // trailing moov atom). Serving the head here stalls video playback.
        start = Math.max(0, stat.size - parseInt(m[2], 10));
        end = stat.size - 1;
      } else {
        start = parseInt(m[1], 10);
        end = m[2] ? Math.min(parseInt(m[2], 10), stat.size - 1) : stat.size - 1;
      }
      if (start <= end && start < stat.size) {
        res.writeHead(206, {
          'Content-Type': mime,
          'Content-Length': end - start + 1,
          'Content-Range': `bytes ${start}-${end}/${stat.size}`,
          'Accept-Ranges': 'bytes',
        });
        fs.createReadStream(absPath, { start, end }).pipe(res);
        return;
      }
    }
    res.writeHead(416, { 'Content-Range': `bytes */${stat.size}` }).end();
    return;
  }
  res.writeHead(200, {
    'Content-Type': mime,
    'Content-Length': stat.size,
    'Accept-Ranges': 'bytes',
  });
  fs.createReadStream(absPath).pipe(res);
}

/**
 * Injected into every app/html iframe. Exposes `window.surface`:
 *   surface.emit(action, payload)  → typed ui-event delivered to the agent
 *   surface.theme                  → the shell's design tokens
 *   surface.on('theme', fn)
 * Applies the shell theme as CSS variables on :root so agent-built apps can
 * simply use var(--bg), var(--panel), var(--text), var(--accent), ...
 */
const BRIDGE_JS = `(() => {
  const id = new URLSearchParams(location.search).get('surfaceId') || window.__SURFACE_ID || '';
  const listeners = { theme: [] };
  const surface = {
    id,
    theme: null,
    emit(action, payload) {
      parent.postMessage({ __surface: true, componentId: id, action, payload }, '*');
    },
    on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); },
  };
  window.surface = surface;
  addEventListener('message', (e) => {
    const d = e.data;
    if (!d || !d.__surfaceTheme) return;
    surface.theme = d.__surfaceTheme;
    const root = document.documentElement;
    for (const [k, v] of Object.entries(d.__surfaceTheme)) root.style.setProperty('--' + k, v);
    root.style.colorScheme = 'dark';
    (listeners.theme || []).forEach((fn) => fn(surface.theme));
  });
  // Freeze support: the shell asks for a bitmap of this document; the capture
  // lib is lazy-loaded from the hub and the PNG travels back over postMessage.
  let capLib = null;
  const ensureCaptureLib = () => {
    if (window.modernScreenshot) return Promise.resolve();
    capLib = capLib || new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = '/__surface/capture.js';
      s.onload = res;
      s.onerror = () => rej(new Error('capture lib failed to load'));
      document.head.appendChild(s);
    });
    return capLib;
  };
  addEventListener('message', async (e) => {
    const d = e.data;
    if (!d || !d.__surfaceCaptureReq) return;
    try {
      await ensureCaptureLib();
      const url = await window.modernScreenshot.domToPng(document.documentElement, { scale: 1 });
      parent.postMessage({ __surfaceCaptureRes: d.__surfaceCaptureReq, dataUrl: url }, '*');
    } catch (err) {
      parent.postMessage({ __surfaceCaptureRes: d.__surfaceCaptureReq, error: String(err) }, '*');
    }
  });
  parent.postMessage({ __surface: true, componentId: id, action: '__ready' }, '*');
})();`;

interface AppMount {
  appId: string;
  dir: string;
  componentId: string;
  wsId: string;
  watcher: FSWatcher;
}

export interface UploadInfo {
  path: string;
  name: string;
  size: number;
  mime: string;
}

export class AssetHub {
  private assets = new Map<string, { path: string; name: string }>();
  private apps = new Map<string, AppMount>();
  private byPath = new Map<string, string>(); // absPath -> token

  constructor(
    private resolveInbox: (wsId: string) => string | null,
    private onAppChange: (wsId: string, componentId: string) => void,
    private onUpload: (wsId: string, file: UploadInfo) => void,
  ) {}

  /** Register a local file and get a stable serving URL. */
  registerAsset(absPath: string, restoredToken?: string): { url: string; name: string; size: number; mime: string } {
    const resolved = path.resolve(absPath);
    const stat = fs.statSync(resolved); // throws if missing — callers surface the error
    if (!stat.isFile()) throw new Error(`not a file: ${resolved}`);
    let token = this.byPath.get(resolved);
    if (!token) {
      // Keep persisted panel URLs usable by an already-open player after a
      // restart. Only reuse well-formed, unclaimed IDs from saved components.
      token = restoredToken && /^[0-9a-f]{8}-[0-9a-f]{3}$/.test(restoredToken) && !this.assets.has(restoredToken)
        ? restoredToken : randomUUID().slice(0, 12);
      this.byPath.set(resolved, token);
      this.assets.set(token, { path: resolved, name: path.basename(resolved) });
    }
    const name = path.basename(resolved);
    return {
      url: `/assets/${token}/${encodeURIComponent(name)}`,
      name,
      size: stat.size,
      mime: mimeFor(resolved),
    };
  }

  /** Mount a directory as a live-reloading app panel. */
  mountApp(dir: string, componentId: string, wsId: string, restoredId?: string): { appId: string; url: string } {
    const resolved = path.resolve(dir);
    if (!fs.existsSync(path.join(resolved, 'index.html'))) {
      throw new Error(`no index.html in ${resolved}`);
    }
    // Remount of the same dir reuses the mount (keeps URLs stable).
    for (const mount of this.apps.values()) {
      if (mount.dir === resolved) {
        mount.componentId = componentId;
        mount.wsId = wsId;
        return { appId: mount.appId, url: this.appUrl(mount.appId, componentId) };
      }
    }
    const appId = restoredId && /^[0-9a-f]{8}-[0-9a-f]{3}$/.test(restoredId) && !this.apps.has(restoredId)
      ? restoredId : randomUUID().slice(0, 12);
    let debounce: ReturnType<typeof setTimeout> | undefined;
    const watcher = chokidar.watch(resolved, { ignoreInitial: true }).on('all', () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        const m = this.apps.get(appId);
        if (m) this.onAppChange(m.wsId, m.componentId);
      }, 200);
    });
    this.apps.set(appId, { appId, dir: resolved, componentId, wsId, watcher });
    return { appId, url: this.appUrl(appId, componentId) };
  }

  private appUrl(appId: string, componentId: string): string {
    return `/apps/${appId}/index.html?surfaceId=${encodeURIComponent(componentId)}`;
  }

  async unmountApp(appId: string): Promise<void> {
    const m = this.apps.get(appId);
    if (m) {
      this.apps.delete(appId);
      await m.watcher.close();
    }
  }

  /** Returns true if the request was handled. */
  handle(req: IncomingMessage, res: ServerResponse): boolean {
    const url = new URL(req.url ?? '/', 'http://local');
    const p = url.pathname;

    if (p === '/__surface/bridge.js') {
      res.writeHead(200, { 'Content-Type': 'text/javascript; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end(BRIDGE_JS + '\n' + PRESENTATION_BRIDGE_JS);
      return true;
    }

    if (p === '/__surface/capture.js') {
      serveFile(req, res, CAPTURE_LIB_PATH);
      return true;
    }

    if (p.startsWith('/assets/')) {
      const token = p.split('/')[2];
      const asset = token ? this.assets.get(token) : undefined;
      if (!asset) {
        res.writeHead(404).end('unknown asset');
        return true;
      }
      serveFile(req, res, asset.path);
      return true;
    }

    if (p.startsWith('/apps/')) {
      const [, , appId, ...rest] = p.split('/');
      const mount = appId ? this.apps.get(appId) : undefined;
      if (!mount) {
        res.writeHead(404).end('unknown app');
        return true;
      }
      const rel = rest.join('/') || 'index.html';
      const abs = path.resolve(mount.dir, rel);
      if (abs !== mount.dir && !abs.startsWith(mount.dir + path.sep)) {
        res.writeHead(403).end('forbidden');
        return true;
      }
      if (/\.html?$/.test(abs) && fs.existsSync(abs)) {
        // Inject the bridge so window.surface exists inside the app.
        let html = fs.readFileSync(abs, 'utf8');
        const tag = '<script src="/__surface/bridge.js"></script>';
        html = /<head[^>]*>/i.test(html)
          ? html.replace(/<head[^>]*>/i, (m) => `${m}\n${tag}`)
          : tag + '\n' + html;
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
        res.end(html);
        return true;
      }
      serveFile(req, res, abs);
      return true;
    }

    if (p === '/upload' && req.method === 'POST') {
      const wsId = url.searchParams.get('ws') ?? '';
      const inboxDir = this.resolveInbox(wsId);
      if (!inboxDir) {
        res.writeHead(400).end('unknown workspace');
        return true;
      }
      const rawName = url.searchParams.get('name') ?? 'upload.bin';
      const safe = rawName.replace(/[^\w.\- ()]/g, '_').slice(0, 120) || 'upload.bin';
      let dest = path.join(inboxDir, safe);
      let n = 1;
      while (fs.existsSync(dest)) {
        const ext = path.extname(safe);
        dest = path.join(inboxDir, `${path.basename(safe, ext)}-${n++}${ext}`);
      }
      const out = fs.createWriteStream(dest);
      req.pipe(out);
      out.on('finish', async () => {
        const stat = await fsp.stat(dest);
        const info = { path: dest, name: path.basename(dest), size: stat.size, mime: mimeFor(dest) };
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(info));
        this.onUpload(wsId, info);
      });
      out.on('error', (err) => {
        res.writeHead(500).end(String(err));
      });
      return true;
    }

    return false;
  }
}
