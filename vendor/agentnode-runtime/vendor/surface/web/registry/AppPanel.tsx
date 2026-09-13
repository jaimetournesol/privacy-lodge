import { memo, useEffect, useState } from 'react';
import type { SurfaceComponent } from '../../shared/protocol';

/**
 * An agent-built app directory, served by the hub and hot-reloaded whenever
 * the agent edits its files (the `reload` nonce keys the iframe).
 */
export const AppPanel = memo(
  ({ comp, reload, expanded }: { comp: SurfaceComponent; v: number; reload: number; expanded: boolean }) => {
    const p = comp.props as { url?: string; height?: number | 'fill' };
    const [shimmer, setShimmer] = useState(false);

    useEffect(() => {
      if (reload === 0) return;
      setShimmer(true);
      const t = setTimeout(() => setShimmer(false), 700);
      return () => clearTimeout(t);
    }, [reload]);

    return (
      <div className={`app-frame-wrap ${shimmer ? 'shimmer' : ''}`} style={{ height: expanded || p.height === 'fill' ? '100%' : (p.height ?? 480) }}>
        <iframe
          key={reload}
          className="panel-frame"
          data-surface-id={comp.id}
          title={comp.title ?? comp.id}
          // Keyboard-driven apps (decks, games) get focus as soon as the pointer is over them.
          onMouseEnter={(e) => e.currentTarget.contentWindow?.focus()}
          onLoad={(e) => e.currentTarget.contentWindow?.focus()}
          sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-downloads"
          src={p.url}
        />
      </div>
    );
  },
  // `v` is a scalar snapshot — comp itself is mutated in place by the patcher.
  (a, b) => a.v === b.v && a.reload === b.reload && a.expanded === b.expanded,
);
