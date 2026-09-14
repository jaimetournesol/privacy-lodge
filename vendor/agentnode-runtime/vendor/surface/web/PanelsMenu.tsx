import { useEffect, useRef, useState } from 'react';
import type { Surface } from './useSurface';

/** All windows of the workspace — visible and hidden — with show/hide toggles. */
export function PanelsMenu({ surface, onClose }: { surface: Surface; onClose: () => void }) {
  const { state, send } = surface;
  const [armedDiscard, setArmedDiscard] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const components = (state?.components ?? []).slice().sort((a, b) => a.order - b.order);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  useEffect(() => {
    if (!armedDiscard) return;
    const t = setTimeout(() => setArmedDiscard(null), 3000);
    return () => clearTimeout(t);
  }, [armedDiscard]);

  return (
    <div className="panels-menu" ref={rootRef}>
      <div className="ws-section">windows · {components.filter((c) => !c.hidden).length} shown / {components.length} total</div>
      {components.length === 0 && <div className="muted panels-empty">nothing here yet</div>}
      {components.map((c) => (
        <div key={c.id} className={`panels-row ${c.hidden ? 'is-hidden' : ''}`}>
          <button
            className="panels-row-main"
            title={c.hidden ? 'Show this panel' : 'Hide this panel'}
            onClick={() => send({ type: 'ui-event', componentId: c.id, action: c.hidden ? 'restore' : 'dismiss' })}
          >
            <span className="panels-eye">{c.hidden ? '◌' : '●'}</span>
            <span className="panel-type">{c.type}</span>
            <span className="panels-title">{c.title ?? c.id}</span>
          </button>
          <button
            className={`icon-btn ${armedDiscard === c.id ? 'danger' : ''}`}
            title="Delete permanently"
            onClick={() => {
              if (armedDiscard === c.id) {
                send({ type: 'ui-event', componentId: c.id, action: 'discard' });
                setArmedDiscard(null);
              } else {
                setArmedDiscard(c.id);
              }
            }}
          >
            {armedDiscard === c.id ? 'sure?' : '✕'}
          </button>
        </div>
      ))}
    </div>
  );
}
