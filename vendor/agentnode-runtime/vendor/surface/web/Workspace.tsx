import { memo, useEffect, useRef, useState } from 'react';
import type { ChatAnnotation, Region, SurfaceComponent } from '../shared/protocol';
import { capturePanel, FreezeOverlay } from './annotate';
import { renderComponent } from './registry';
import { VIEW_MODE, PRESENTATION_SCOPE, type Surface } from './useSurface';

interface DndHooks {
  onStart: () => void;
  onOver: () => void;
  onDrop: () => void;
  onEnd: () => void;
  hint: boolean;
}

/**
 * Reports the workspace area size to the hub (the agent composes for an
 * unknown screen — this tells it how big the canvas is) and exposes it as
 * --ws-h so `height: "fill"` panels can take the visible area.
 */
function useViewportReporter(send: Surface['send']) {
  const ref = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const report = () => {
      const w = Math.round(el.clientWidth), h = Math.round(el.clientHeight);
      el.style.setProperty('--ws-h', `${h}px`);
      clearTimeout(timer);
      timer = setTimeout(() => send({ type: 'viewport', width: w, height: h, dpr: window.devicePixelRatio || 1 }), 250);
    };
    report();
    const ro = new ResizeObserver(report);
    ro.observe(el);
    return () => {
      ro.disconnect();
      clearTimeout(timer);
    };
  }, [send]);
  return ref;
}

export function Workspace({
  surface,
  onAnnotate,
}: {
  surface: Surface;
  onAnnotate: (ann: ChatAnnotation) => void;
}) {
  const { state, send, reloads } = surface;
  const [localExpanded,setLocalExpanded]=useState<string|null>(null);
  const focusedId=VIEW_MODE==='work'?localExpanded:(Object.prototype.hasOwnProperty.call(state?.view?.presentations??{},PRESENTATION_SCOPE)?state?.view?.presentations?.[PRESENTATION_SCOPE]:(PRESENTATION_SCOPE==='default'?state?.view?.expandedId:null));
  const expandedId = state?.components.some(c => c.id === focusedId && !c.hidden) ? focusedId : null;
  const setExpandedId = (id: string | null) => {
    if(VIEW_MODE==='work'){setLocalExpanded(id);return;}
    if(VIEW_MODE==='presenter')return;
    const componentId = id ?? expandedId;
    if (componentId) send({type: 'ui-event', componentId, action: '__workspace-focus', payload: {expanded: id !== null}});
  };
  const [frozen, setFrozen] = useState<{ id: string; snapshot: string | null } | null>(null);
  const dragId = useRef<string | null>(null);
  const [dropHint, setDropHint] = useState<string | null>(null);
  const mainRef = useViewportReporter(send);
  const components = (state?.components ?? [])
    .filter((c) => !c.hidden)
    .slice()
    .sort((a, b) => a.order - b.order);
  const main = components.filter((c) => c.region !== 'side');
  const side = components.filter((c) => c.region === 'side');

  // Esc closes the expanded panel.
  useEffect(() => {
    if (!expandedId) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setExpandedId(null);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [expandedId]);

  // Hidden or removed panels are cleared by the hub, with no viewer feedback loop.

  const emit = (componentId: string, action: string, payload?: unknown) =>
    send({ type: 'ui-event', componentId, action, payload });

  /** Insert the dragged panel before targetId (or append) in targetRegion. */
  const applyDrop = (targetId: string | null, targetRegion: Region) => {
    const src = dragId.current;
    dragId.current = null;
    setDropHint(null);
    if (!src || src === targetId) return;
    const lists: Record<Region, string[]> = {
      main: main.map((c) => c.id).filter((id) => id !== src),
      side: side.map((c) => c.id).filter((id) => id !== src),
    };
    const list = lists[targetRegion];
    const at = targetId ? list.indexOf(targetId) : -1;
    if (at >= 0) list.splice(at, 0, src);
    else list.push(src);
    send({ type: 'ui-arrange', main: lists.main, side: lists.side });
  };

  if (components.length === 0) {
    const hidden = (state?.components ?? []).filter((c) => c.hidden);
    return (
      <main className="workspace workspace-empty" ref={mainRef}>
        <div className="empty-hint">
          <div className="empty-glyph">▢</div>
          {hidden.length ? (
            <>
              <p>
                {hidden.length === 1 ? 'One panel is hidden.' : `All ${hidden.length} panels are hidden.`}
              </p>
              <p className="muted">
                <button className="ghost-btn" onClick={() => hidden.forEach((c) => emit(c.id, 'restore'))}>
                  reopen {hidden.length === 1 ? 'it' : 'them'}
                </button>
              </p>
            </>
          ) : (
            <>
              <p>The workspace is empty.</p>
              <p className="muted">The agent will compose panels here — apps, tables, media, designs.</p>
            </>
          )}
        </div>
      </main>
    );
  }

  const renderPanel = (c: SurfaceComponent) => (
    <Panel
      key={c.id}
      comp={c}
      v={c.v}
      reload={reloads[c.id] ?? 0}
      emit={emit}
      expanded={expandedId === c.id}
      onToggleExpand={() => setExpandedId(expandedId === c.id ? null : c.id)}
      frozen={frozen?.id === c.id ? frozen : null}
      onFreeze={(snapshot) => setFrozen({ id: c.id, snapshot })}
      onFreezeEnd={() => setFrozen(null)}
      onAnnotate={onAnnotate}
      dnd={{
        onStart: () => (dragId.current = c.id),
        onOver: () => setDropHint((h) => (h === c.id ? h : c.id)),
        onDrop: () => applyDrop(c.id, c.region),
        onEnd: () => {
          dragId.current = null;
          setDropHint(null);
        },
        hint: dropHint === c.id,
      }}
    />
  );

  const regionProps = (region: Region) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!dragId.current) return;
      e.preventDefault();
      setDropHint((h) => (h === `region:${region}` ? h : `region:${region}`));
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      applyDrop(null, region);
    },
  });

  return (
    <main className={`workspace ${side.length ? 'has-side' : ''}`} ref={mainRef}>
      <div className={`region region-main ${dropHint === 'region:main' ? 'drop-hint' : ''}`} {...regionProps('main')}>
        {main.map(renderPanel)}
      </div>
      {side.length > 0 && (
        <div className={`region region-side ${dropHint === 'region:side' ? 'drop-hint' : ''}`} {...regionProps('side')}>
          {side.map(renderPanel)}
        </div>
      )}
      {expandedId && <div className="expand-backdrop" onClick={() => setExpandedId(null)} />}
    </main>
  );
}

type EmitFn = (componentId: string, action: string, payload?: unknown) => void;

const RESIZABLE = new Set(['app', 'html', 'video', 'chart']);

/**
 * NOTE: expansion is CSS-only — the SAME panel element (and any iframe/video
 * inside it) is promoted to a fixed overlay. Never render a second copy: a
 * duplicated app iframe boots a fresh instance and stateful content (audio,
 * video, running apps) would play twice / lose its state.
 */
const Panel = memo(
  ({
    comp,
    reload,
    emit,
    expanded,
    onToggleExpand,
    frozen,
    onFreeze,
    onFreezeEnd,
    onAnnotate,
    dnd,
  }: {
    comp: SurfaceComponent;
    v: number;
    reload: number;
    emit: EmitFn;
    expanded: boolean;
    onToggleExpand: () => void;
    frozen: { id: string; snapshot: string | null } | null;
    onFreeze: (snapshot: string | null) => void;
    onFreezeEnd: () => void;
    onAnnotate: (ann: ChatAnnotation) => void;
    dnd: DndHooks;
  }) => {
    const sectionRef = useRef<HTMLElement>(null);
    const bodyRef = useRef<HTMLDivElement>(null);
    const touched = Date.now() - comp.updatedAt < 2000;

    const startFreeze = async () => {
      const snapshot = bodyRef.current ? await capturePanel(comp, bodyRef.current) : null;
      onFreeze(snapshot);
    };

    const startResize = (e: React.PointerEvent) => {
      if (expanded) return;
      e.preventDefault();
      const target = bodyRef.current?.querySelector<HTMLElement>('.app-frame-wrap, iframe.panel-frame, .chart-wrap, video');
      if (!target) return;
      const startY = e.clientY;
      const startH = target.offsetHeight;
      let h = startH;
      const move = (ev: PointerEvent) => {
        h = Math.min(1400, Math.max(140, startH + ev.clientY - startY));
        target.style.height = `${h}px`;
      };
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        if (h !== startH) emit(comp.id, 'resize', { height: h });
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    };

    return (
      <section
        ref={sectionRef}
        className={`panel panel-${comp.type} ${touched ? 'panel-touched' : ''} ${frozen ? 'panel-frozen' : ''} ${dnd.hint ? 'drop-hint' : ''} ${expanded ? 'panel-live-expanded' : ''} ${comp.props.height === 'fill' && !expanded ? 'panel-fill' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          e.stopPropagation();
          dnd.onOver();
        }}
        onDrop={(e) => {
          e.preventDefault();
          e.stopPropagation();
          dnd.onDrop();
        }}
      >
        <PanelChrome
          comp={comp}
          emit={emit}
          expanded={expanded}
          onExpand={onToggleExpand}
          onPen={() => void startFreeze()}
          onFullscreen={expanded ? () => void sectionRef.current?.requestFullscreen?.().catch(() => {}) : undefined}
          onDragStart={expanded ? undefined : dnd.onStart}
          onDragEnd={dnd.onEnd}
        />
        <div className="panel-body-wrap">
          <div className="panel-body" ref={bodyRef}>
            {renderComponent(comp, emit, reload, expanded)}
          </div>
          {frozen && (
            <FreezeOverlay
              comp={comp}
              snapshot={frozen.snapshot}
              onCancel={onFreezeEnd}
              onDone={(ann) => {
                onAnnotate(ann);
                onFreezeEnd();
              }}
            />
          )}
        </div>
        {!expanded && comp.props.height !== 'fill' && RESIZABLE.has(comp.type) && (
          <div className="resize-handle" title="Drag to resize" onPointerDown={startResize} />
        )}
      </section>
    );
  },
  (a, b) =>
    a.v === b.v && a.reload === b.reload && a.frozen === b.frozen && a.dnd.hint === b.dnd.hint && a.expanded === b.expanded,
);

function PanelChrome({
  comp,
  emit,
  onExpand,
  onPen,
  onFullscreen,
  onDragStart,
  onDragEnd,
  expanded,
}: {
  comp: SurfaceComponent;
  emit: EmitFn;
  onExpand: () => void;
  onPen?: () => void;
  onFullscreen?: () => void;
  onDragStart?: () => void;
  onDragEnd?: () => void;
  expanded?: boolean;
}) {
  return (
    <header
      className={`panel-head ${onDragStart ? 'grabbable' : ''}`}
      draggable={Boolean(onDragStart)}
      onDragStart={(e) => {
        e.dataTransfer.setData('text/plain', comp.id);
        e.dataTransfer.effectAllowed = 'move';
        onDragStart?.();
      }}
      onDragEnd={onDragEnd}
    >
      <span className="panel-type">{comp.type}</span>
      <span className="panel-title">{comp.title ?? comp.id}</span>
      <span className="panel-actions">
        {onPen && (
          <button className="icon-btn" title="Freeze & mark up this panel" onClick={onPen}>
            ✎
          </button>
        )}
        {expanded && onFullscreen && (
          <button className="icon-btn" title="True fullscreen" onClick={onFullscreen}>
            ⛶
          </button>
        )}
        <button className="icon-btn" title={expanded ? 'Close (Esc)' : 'Expand'} onClick={onExpand}>
          {expanded ? '⤡' : '⤢'}
        </button>
        {!expanded && (
          <button className="icon-btn" title="Close panel" onClick={() => emit(comp.id, 'dismiss')}>
            ✕
          </button>
        )}
      </span>
    </header>
  );
}
