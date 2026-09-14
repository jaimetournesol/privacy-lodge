import { useEffect, useRef, useState } from 'react';
import type { BridgeEvent, ChatAnnotation } from '../shared/protocol';
import { ChatRail } from './ChatRail';
import { EventLog } from './EventLog';
import { FilesPanel } from './FilesPanel';
import { HomeScreen } from './HomeScreen';
import { PanelsMenu } from './PanelsMenu';
import { EMBED, VIEW_MODE, PRESENTATION_SCOPE, THEME_VARS, useSurface } from './useSurface';
import { Workspace } from './Workspace';
import { WorkspaceMenu } from './WorkspaceMenu';

const PHASE_LABEL: Record<string, string> = {
  starting: 'starting…',
  idle: 'idle',
  thinking: 'thinking',
  responding: 'responding',
  tool: 'working',
  stopped: 'stopped',
  error: 'error',
};

interface Toast {
  key: number;
  wsId: string;
  text: string;
  kind: 'done' | 'error';
}

export default function App() {
  const surface = useSurface();
  const { state, status, send, uploadFiles, workspace, workspaces, activity, switchWorkspace } = surface;
  const [view, setView] = useState<'home' | 'surface'>(() => {
    if (EMBED) return 'surface';
    try {
      return localStorage.getItem('surface-last-workspace') ? 'surface' : 'home';
    } catch {
      return 'home';
    }
  });
  const [dragging, setDragging] = useState(false);
  const [resetArmed, setResetArmed] = useState(false);
  const [annotations, setAnnotations] = useState<ChatAnnotation[]>([]);
  const [filesOpen, setFilesOpen] = useState(false);
  const [panelsOpen, setPanelsOpen] = useState(false);
  const [eventsOpen, setEventsOpen] = useState(false);
  const [zen, setZen] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastSeq = useRef(0);

  useEffect(() => {
    if (!resetArmed) return;
    const t = setTimeout(() => setResetArmed(false), 3000);
    return () => clearTimeout(t);
  }, [resetArmed]);

  // Leaving browser fullscreen (Esc) also leaves zen mode.
  useEffect(() => {
    const onFsChange = () => {
      if (!document.fullscreenElement) setZen(false);
    };
    document.addEventListener('fullscreenchange', onFsChange);
    return () => document.removeEventListener('fullscreenchange', onFsChange);
  }, []);

  const toggleZen = () => {
    if (zen) {
      if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
      setZen(false);
    } else {
      void document.documentElement.requestFullscreen?.().catch(() => {});
      setZen(true);
    }
  };

  // Background-agent activity → toast (unless it's the workspace on screen).
  useEffect(() => {
    if (!activity) return;
    if (activity.id === workspace?.id && view === 'surface') return;
    const key = ++toastSeq.current;
    setToasts((t) => [
      ...t,
      {
        key,
        wsId: activity.id,
        text: activity.phase === 'error' ? `${activity.name} · agent hit an error` : `${activity.name} · agent finished`,
        kind: activity.phase === 'error' ? 'error' : 'done',
      },
    ]);
    const timer = setTimeout(() => setToasts((t) => t.filter((x) => x.key !== key)), 7000);
    return () => clearTimeout(timer);
  }, [activity]); // eslint-disable-line react-hooks/exhaustive-deps

  const localPositions=useRef<Record<string,any>>({});
  // Ordinary previews own their cursor. Presenter/controller views follow their output group.
  const presentationView = useRef({state, status, workspace});
  presentationView.current = {state, status, workspace};
  const sendPresentation = (frame: HTMLIFrameElement) => {
    const current = presentationView.current;
    const props=current.state?.components.find(c=>c.id===frame.dataset.surfaceId)?.props;
    const local=localPositions.current[(current.workspace?.id||'')+':'+frame.dataset.surfaceId];
    const position=VIEW_MODE==='work'?local:((props?.__presentations as Record<string,unknown>)?.[PRESENTATION_SCOPE]??(PRESENTATION_SCOPE==='default'?props?.__presentation:undefined)??local);
    frame.contentWindow?.postMessage({__surfacePresentation:{online:current.status === 'open',state:position}}, '*');
  };
  useEffect(() => {
    document.querySelectorAll<HTMLIFrameElement>('iframe[data-surface-id]').forEach(sendPresentation);
  }, [state?.v, status]);

  // Events + theme handshake for iframe panels (apps & html).
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const d = e.data as BridgeEvent | undefined;
      if (!d || d.__surface !== true) return;
      const frame = [...document.querySelectorAll<HTMLIFrameElement>('iframe[data-surface-id]')].find(f => f.contentWindow === e.source && f.dataset.surfaceId === d.componentId);
      if (!frame) return;
      if (d.action === '__presentation-ready') {sendPresentation(frame);return;}
      if (d.action.startsWith('__presentation-')) {
        if(VIEW_MODE==='work'){
          const payload=d.payload as any;const id=(presentationView.current.workspace?.id||'')+':'+d.componentId;
          let current=localPositions.current[id];
          if(d.action==='__presentation-register'&&payload&&Number.isSafeInteger(payload.count)&&payload.count>0){
            if(!current||current.deck!==payload.deck)current=localPositions.current[id]={deck:payload.deck,count:payload.count,index:0,revision:0};
          }else if(d.action==='__presentation-command'&&current&&payload?.deck===current.deck){
            const index=payload.command==='next'?current.index+1:payload.command==='previous'?current.index-1:payload.command==='go'?payload.index:current.index;
            if(Number.isSafeInteger(index))current.index=Math.max(0,Math.min(current.count-1,index));current.revision++;
          }
          sendPresentation(frame);return;
        }
        sendPresentation(frame);
        if(VIEW_MODE==='presenter'){
          const payload=d.payload as any;const id=(presentationView.current.workspace?.id||'')+':'+d.componentId;
          if(d.action==='__presentation-register'&&payload&&Number.isSafeInteger(payload.count)&&payload.count>0){localPositions.current[id]={deck:payload.deck,count:payload.count,index:0,revision:0};sendPresentation(frame);}
          return;
        }
        if (presentationView.current.status !== 'open') return;
      }
      if (d.action === '__ready') {
        (e.source as Window | null)?.postMessage({ __surfaceTheme: THEME_VARS }, '*');
        return;
      }
      send({ type: 'ui-event', componentId: d.componentId, action: d.action, payload: d.payload });
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [send]);

  // Whole-window drag & drop.
  useEffect(() => {
    let depth = 0;
    const enter = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes('Files')) return;
      depth++;
      setDragging(true);
    };
    const leave = () => {
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const over = (e: DragEvent) => e.preventDefault();
    const drop = (e: DragEvent) => {
      e.preventDefault();
      depth = 0;
      setDragging(false);
      if (e.dataTransfer?.files.length) void uploadFiles(e.dataTransfer.files);
    };
    window.addEventListener('dragenter', enter);
    window.addEventListener('dragleave', leave);
    window.addEventListener('dragover', over);
    window.addEventListener('drop', drop);
    return () => {
      window.removeEventListener('dragenter', enter);
      window.removeEventListener('dragleave', leave);
      window.removeEventListener('dragover', over);
      window.removeEventListener('drop', drop);
    };
  }, [uploadFiles]);

  // Keyboard shortcuts (all ⌘/Ctrl-based, so typing stays unaffected).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const k = e.key.toLowerCase();
      if (k >= '1' && k <= '9') {
        const w = workspaces[Number(k) - 1];
        if (w) {
          e.preventDefault();
          switchWorkspace(w.id);
          setView('surface');
        }
      } else if (k === 'k') {
        e.preventDefault();
        setView((v) => (v === 'home' ? 'surface' : 'home'));
      } else if (k === '.') {
        e.preventDefault();
        send({ type: 'interrupt' });
      } else if (k === 'e') {
        e.preventDefault();
        setEventsOpen((o) => !o);
      } else if (k === 'u') {
        e.preventDefault();
        setFilesOpen((o) => !o);
      } else if (k === 'f' && e.shiftKey) {
        e.preventDefault();
        toggleZen();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [workspaces, switchWorkspace, send, zen]); // eslint-disable-line react-hooks/exhaustive-deps

  const agent = state?.agent;
  const busy = agent && ['thinking', 'responding', 'tool'].includes(agent.phase);

  const toastStack = toasts.length > 0 && (
    <div className="toasts">
      {toasts.map((t) => (
        <button
          key={t.key}
          className={`toast toast-${t.kind}`}
          onClick={() => {
            switchWorkspace(t.wsId);
            setView('surface');
            setToasts((x) => x.filter((y) => y.key !== t.key));
          }}
        >
          {t.kind === 'error' ? '⚠' : '✓'} {t.text}
          <span className="toast-open">open ↗</span>
        </button>
      ))}
    </div>
  );

  if (view === 'home') {
    return (
      <>
        <HomeScreen
          surface={surface}
          onOpen={(id) => {
            switchWorkspace(id);
            setView('surface');
          }}
        />
        {toastStack}
      </>
    );
  }

  if (EMBED) {
    // Panels only: the host page (TVPC agent UI) supplies chat, status and controls.
    const hiddenCount = state?.components.filter((c) => c.hidden).length ?? 0;
    const total = state?.components.length ?? 0;
    return (
      <div className="shell shell-embed">
        <Workspace
          surface={surface}
          onAnnotate={(ann) => window.parent?.postMessage({ __surfaceAnnotation: ann, workspaceId: workspace?.id }, '*')}
        />
        {total > 0 && (
          <div className={`panels-anchor embed-panels ${hiddenCount ? 'has-hidden' : ''}`}>
            <button
              className={`ghost-btn ${panelsOpen ? 'on' : ''}`}
              title="All windows in this workspace — show, hide, reopen"
              onClick={() => setPanelsOpen((o) => !o)}
            >
              PANELS{hiddenCount ? ` · ${hiddenCount} hidden` : ''}
            </button>
            {panelsOpen && <PanelsMenu surface={surface} onClose={() => setPanelsOpen(false)} />}
          </div>
        )}
        {dragging && (
          <div className="drop-overlay">
            <div className="drop-box">Drop files for the agent</div>
          </div>
        )}
        {status !== 'open' && <div className="reconnect-banner">connecting to hub…</div>}
      </div>
    );
  }

  return (
    <div className={`shell ${zen ? 'shell-zen' : ''}`}>
      <header className="topbar">
        <div className="brand">
          <span className={`conn-dot ${status}`} title={`connection: ${status}`} />
          <button className="wordmark wordmark-btn" title="Home (⌘K)" onClick={() => setView('home')}>
            SURFACE
          </button>
          <WorkspaceMenu surface={surface} />
        </div>
        <div className="topbar-right">
          {agent && (
            <>
              <span className={`phase phase-${agent.phase}`}>
                {busy && <span className="spinner" />}
                {PHASE_LABEL[agent.phase] ?? agent.phase}
                {agent.phase === 'tool' && agent.activeTool ? ` · ${prettyTool(agent.activeTool)}` : ''}
              </span>
              {agent.warm && <span className="warm-bolt" title="warm session attached — instant dispatch">⚡</span>}
              <span className="chip mono" title="model">{agent.model.replace('claude-', '')}</span>
              <span className="chip mono" title="tokens this session">
                {(agent.tokens ?? 0) >= 1000 ? `${((agent.tokens ?? 0) / 1000).toFixed(1)}k` : agent.tokens ?? 0} tok
              </span>
            </>
          )}
          <div className="panels-anchor">
            <button
              className={`ghost-btn ${panelsOpen ? 'on' : ''}`}
              title="All windows in this workspace — show, hide, reopen"
              onClick={() => setPanelsOpen((o) => !o)}
            >
              PANELS{state && state.components.some((c) => c.hidden) ? ` ${state.components.filter((c) => !c.hidden).length}/${state.components.length}` : ''}
            </button>
            {panelsOpen && <PanelsMenu surface={surface} onClose={() => setPanelsOpen(false)} />}
          </div>
          <button
            className={`ghost-btn ${filesOpen ? 'on' : ''}`}
            title="Browse this workspace's folder (⌘U)"
            onClick={() => setFilesOpen((o) => !o)}
          >
            FILES
          </button>
          <button
            className={`ghost-btn ${zen ? 'on' : ''}`}
            title={zen ? 'Exit fullscreen workspace (⌘⇧F)' : 'Fullscreen workspace (⌘⇧F)'}
            onClick={toggleZen}
          >
            ⛶
          </button>
          <button
            className="ghost-btn"
            title="Remove every panel from the workspace"
            onClick={() => send({ type: 'clear-workspace' })}
          >
            CLEAR
          </button>
          <button
            className={`ghost-btn ${resetArmed ? 'armed' : ''}`}
            title="Start a brand-new agent session (history is discarded; files stay)"
            onClick={() => {
              if (resetArmed) {
                setResetArmed(false);
                send({ type: 'reset-agent' });
              } else {
                setResetArmed(true);
              }
            }}
          >
            {resetArmed ? 'SURE?' : 'RESET'}
          </button>
          <button className="stop-btn" title="Stop the agent immediately (⌘.)" onClick={() => send({ type: 'interrupt' })}>
            <span className="stop-square" /> STOP
          </button>
        </div>
      </header>

      <ChatRail
        surface={surface}
        annotations={annotations}
        onRemoveAnnotation={(i) => setAnnotations((a) => a.filter((_, j) => j !== i))}
        onClearAnnotations={() => setAnnotations([])}
      />
      <Workspace surface={surface} onAnnotate={(ann) => setAnnotations((a) => [...a, ann])} />
      <EventLog entries={state?.log ?? []} open={eventsOpen} onToggle={() => setEventsOpen((o) => !o)} />

      {filesOpen && surface.workspace && (
        <FilesPanel wsId={surface.workspace.id} wsName={surface.workspace.name} onClose={() => setFilesOpen(false)} />
      )}

      {dragging && (
        <div className="drop-overlay">
          <div className="drop-box">Drop files for the agent</div>
        </div>
      )}

      {status !== 'open' && state && <div className="reconnect-banner">reconnecting to hub…</div>}

      {toastStack}
    </div>
  );
}

export function prettyTool(name: string): string {
  return name.replace(/^mcp__surface__/, 'surface:').replace(/^mcp__(\w+)__/, '$1:');
}
