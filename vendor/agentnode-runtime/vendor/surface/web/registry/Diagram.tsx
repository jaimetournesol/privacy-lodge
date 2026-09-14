import { memo, useEffect, useRef, useState } from 'react';

let mermaidPromise: Promise<typeof import('mermaid')> | null = null;
async function loadMermaid() {
  mermaidPromise ??= import('mermaid').then((m) => {
    m.default.initialize({
      startOnLoad: false,
      theme: 'dark',
      darkMode: true,
      fontFamily: "'Inter', system-ui, sans-serif",
      themeVariables: {
        background: '#111116',
        primaryColor: '#17171c',
        primaryBorderColor: '#38e1ff',
        primaryTextColor: '#e8e8ee',
        lineColor: '#8b8b98',
      },
    });
    return m;
  });
  return mermaidPromise;
}

let seq = 0;

export const Diagram = memo(
  ({ source }: { source: string; v: number }) => {
    const [svg, setSvg] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const alive = useRef(true);

    useEffect(() => {
      alive.current = true;
      setError(null);
      loadMermaid()
        .then((m) => m.default.render(`surface-mmd-${seq++}`, source))
        .then((out) => {
          if (alive.current) setSvg(out.svg);
        })
        .catch((err) => {
          if (alive.current) setError(String(err));
        });
      return () => {
        alive.current = false;
      };
    }, [source]);

    if (error) {
      return (
        <div className="diagram-error">
          <div className="muted">diagram failed to render</div>
          <pre className="mono">{source}</pre>
        </div>
      );
    }
    return svg ? (
      <div className="diagram" dangerouslySetInnerHTML={{ __html: svg }} />
    ) : (
      <div className="muted diagram-loading">rendering…</div>
    );
  },
  (a, b) => a.v === b.v,
);
