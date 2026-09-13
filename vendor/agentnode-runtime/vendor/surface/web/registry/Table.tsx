import { useMemo, useState } from 'react';
import type { SurfaceComponent } from '../../shared/protocol';
import type { EmitFn } from './index';

export function TablePanel({ comp, emit }: { comp: SurfaceComponent; emit: EmitFn }) {
  const p = comp.props as { columns?: string[]; rows?: unknown[][]; selectable?: boolean };
  const columns = p.columns ?? [];
  const rows = p.rows ?? [];
  const [sort, setSort] = useState<{ col: number; dir: 1 | -1 } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows.map((r, i) => [r, i] as const);
    return rows
      .map((r, i) => [r, i] as const)
      .sort(([a], [b]) => {
        const av = a[sort.col];
        const bv = b[sort.col];
        if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sort.dir;
        return String(av ?? '').localeCompare(String(bv ?? '')) * sort.dir;
      });
  }, [rows, sort]);

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th
                key={i}
                onClick={() => setSort((s) => (s?.col === i ? (s.dir === 1 ? { col: i, dir: -1 } : null) : { col: i, dir: 1 }))}
              >
                {c}
                {sort?.col === i && <span className="sort-arrow">{sort.dir === 1 ? ' ↑' : ' ↓'}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map(([row, origIndex]) => (
            <tr
              key={origIndex}
              className={p.selectable ? 'selectable' : ''}
              onClick={() => p.selectable && emit(comp.id, 'row-select', { index: origIndex, row })}
            >
              {columns.map((_, ci) => (
                <td key={ci}>{formatCell(row[ci])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="muted table-empty">no rows</div>}
    </div>
  );
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}
