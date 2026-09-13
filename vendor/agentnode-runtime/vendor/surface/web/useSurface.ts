import { applyPatch } from 'fast-json-patch';
import { useEffect, useRef, useState } from 'react';
import type { ClientMsg, ServerMsg, SurfaceState, WorkspaceInfo } from '../shared/protocol';

export type ConnStatus = 'connecting' | 'open' | 'closed';

export interface WsActivity {
  id: string;
  name: string;
  phase: string;
  nonce: number;
}

export interface Surface {
  state: SurfaceState | null;
  status: ConnStatus;
  /** componentId → nonce, bumped when an app panel should reload. */
  reloads: Record<string, number>;
  workspaces: WorkspaceInfo[];
  workspace: WorkspaceInfo | null;
  /** Last background-agent activity event (turn finished / failed). */
  activity: WsActivity | null;
  send: (msg: ClientMsg) => void;
  switchWorkspace: (id: string) => void;
  uploadFiles: (files: Iterable<File>) => Promise<void>;
}

const LAST_WS_KEY = 'surface-last-workspace';
/** Panels-only rendering inside another page (e.g. the TVPC agent UI). */
const accessKey='surface-access:'+location.pathname+location.search;
const fragmentAccess=new URLSearchParams(location.hash.slice(1)).get('access');
if(fragmentAccess){sessionStorage.setItem(accessKey,fragmentAccess);history.replaceState(null,'',location.pathname+location.search);}
export const ACCESS=sessionStorage.getItem(accessKey)||'';
export const VIEW_MODE=new URLSearchParams(location.search).get('view')||'work';
export const PRESENTATION_SCOPE=new URLSearchParams(location.search).get('presentation')||'default';
export const EMBED = new URLSearchParams(location.search).get('embed') === '1';

export function useSurface(): Surface {
  const [, setTick] = useState(0);
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const [reloads, setReloads] = useState<Record<string, number>>({});
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([]);
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [activity, setActivity] = useState<WsActivity | null>(null);
  const activityNonce = useRef(0);
  const doc = useRef<SurfaceState | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let dead = false;
    let retryMs = 500;
    let timer: ReturnType<typeof setTimeout>;

    const connect = () => {
      if (dead) return;
      setStatus('connecting');
      // ?ws=<id> pins the workspace (used by embed mode); otherwise the last one viewed.
      let last = new URLSearchParams(location.search).get('ws') ?? '';
      if (!last) {
        try {
          last = localStorage.getItem(LAST_WS_KEY) ?? '';
        } catch {
          /* storage unavailable */
        }
      }
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws?ws=${encodeURIComponent(last)}&access=${encodeURIComponent(ACCESS)}&view=${VIEW_MODE}&presentation=${encodeURIComponent(PRESENTATION_SCOPE)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        retryMs = 500;
        setStatus('open');
      };

      ws.onmessage = (e) => {
        let msg: ServerMsg;
        try {
          msg = JSON.parse(e.data);
        } catch {
          return;
        }
        if (msg.type === 'snapshot') {
          doc.current = msg.state;
          setWorkspace(msg.workspace);
          setReloads({});
          try {
            if (!EMBED) localStorage.setItem(LAST_WS_KEY, msg.workspace.id);
          } catch {
            /* fine */
          }
          setTick((t) => t + 1);
        } else if (msg.type === 'patch') {
          const d = doc.current;
          if (!d || d.v + 1 !== msg.v) {
            ws.send(JSON.stringify({ type: 'resync' } satisfies ClientMsg));
            return;
          }
          try {
            applyPatch(d, msg.ops as never[], false, true);
            d.v = msg.v;
            setTick((t) => t + 1);
          } catch {
            ws.send(JSON.stringify({ type: 'resync' } satisfies ClientMsg));
          }
        } else if (msg.type === 'app-reload') {
          setReloads((r) => ({ ...r, [msg.componentId]: (r[msg.componentId] ?? 0) + 1 }));
        } else if (msg.type === 'workspaces') {
          setWorkspaces(msg.items);
        } else if (msg.type === 'ws-activity') {
          setActivity({ id: msg.id, name: msg.name, phase: msg.phase, nonce: ++activityNonce.current });
        } else if (msg.type === 'capture-request') {
          // Agent self-sight: rasterize the workspace area and post it back.
          void (async () => {
            try {
              const { domToPng } = await import('modern-screenshot');
              const el = document.querySelector('.workspace') as HTMLElement | null;
              if (!el) throw new Error('workspace not rendered');
              const dataUrl = await Promise.race([
                domToPng(el, {
                  backgroundColor: '#0a0a0c',
                  scale: 1,
                  // Rasterizing live video/iframe/audio stalls the serializer —
                  // they come out as styled empty boxes instead.
                  filter: (node) =>
                    !(node instanceof Element && ['VIDEO', 'IFRAME', 'AUDIO', 'OBJECT', 'EMBED'].includes(node.tagName)),
                }),
                new Promise<never>((_, rej) => setTimeout(() => rej(new Error('capture timeout')), 9000)),
              ]);
              await fetch('/internal/capture', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reqId: msg.reqId, dataUrl }),
              });
            } catch (err) {
              await fetch('/internal/capture', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reqId: msg.reqId, error: String(err) }),
              }).catch(() => {});
            }
          })();
        }
      };

      ws.onclose = () => {
        setStatus('closed');
        if (!dead) {
          timer = setTimeout(connect, retryMs);
          retryMs = Math.min(retryMs * 2, 5000);
        }
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      dead = true;
      clearTimeout(timer);
      wsRef.current?.close();
    };
  }, []);

  const send = (msg: ClientMsg) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  };

  const switchWorkspace = (id: string) => send({ type: 'switch-workspace', id });

  const uploadFiles = async (files: Iterable<File>) => {
    const wsId = workspace?.id ?? '';
    for (const f of files) {
      await fetch(`/upload?ws=${encodeURIComponent(wsId)}&name=${encodeURIComponent(f.name)}`, {
        method: 'POST',
        body: f,
      });
    }
  };

  return { state: doc.current, status, reloads, workspaces, workspace, activity, send, switchWorkspace, uploadFiles };
}

/** Design tokens pushed into iframe panels so agent-built pages match the shell. */
export const THEME_VARS: Record<string, string> = {
  bg: '#0a0a0c',
  panel: '#111116',
  panel2: '#17171c',
  border: '#26262e',
  text: '#e8e8ee',
  muted: '#8b8b98',
  accent: '#38e1ff',
  good: '#3ddc97',
  warn: '#f5c96b',
  bad: '#ff5d6c',
  font: "'Inter', system-ui, sans-serif",
  mono: "'JetBrains Mono', ui-monospace, monospace",
};
