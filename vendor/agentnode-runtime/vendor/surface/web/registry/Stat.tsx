interface StatItem {
  label: string;
  value: string | number;
  unit?: string;
  delta?: number | string;
}

export function StatTiles({ items }: { items: StatItem[] }) {
  return (
    <div className="stats">
      {items.map((s, i) => {
        const delta = typeof s.delta === 'string' ? parseFloat(s.delta) : s.delta;
        return (
          <div key={i} className="stat-tile">
            <div className="stat-value">
              {s.value}
              {s.unit && <span className="stat-unit">{s.unit}</span>}
            </div>
            <div className="stat-label">{s.label}</div>
            {delta !== undefined && !Number.isNaN(delta) && (
              <div className={`stat-delta ${delta >= 0 ? 'up' : 'down'}`}>
                {delta >= 0 ? '▲' : '▼'} {Math.abs(delta)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
