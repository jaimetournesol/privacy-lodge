import { useEffect, useRef, useState } from 'react';
import type { LogEntry, LogLevel } from '../shared/protocol';

const LEVELS: LogLevel[] = ['debug', 'info', 'success', 'warn', 'error', 'tool'];

export function EventLog({
  entries,
  open,
  onToggle,
}: {
  entries: LogEntry[];
  open: boolean;
  onToggle: () => void;
}) {
  const [hidden, setHidden] = useState<Set<LogLevel>>(new Set(['debug']));
  const [expanded, setExpanded] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const visible = entries.filter((e) => !hidden.has(e.level));

  useEffect(() => {
    // Opening the drawer always lands on the LATEST events, pinned to the tail.
    if (open) pinned.current = true;
    const el = scrollRef.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [visible.length, open]);

  const toggleLevel = (lv: LogLevel) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(lv)) next.delete(lv);
      else next.add(lv);
      return next;
    });

  return (
    <section className={`eventlog ${open ? 'open' : 'closed'}`}>
      <header className="eventlog-head" onClick={onToggle}>
        <span className="eventlog-caret">{open ? '▾' : '▸'}</span>
        <span className="eventlog-title">EVENTS</span>
        <span className="eventlog-count mono">{visible.length}</span>
        <span className="eventlog-filters" onClick={(e) => e.stopPropagation()}>
          {LEVELS.map((lv) => (
            <button
              key={lv}
              className={`lv-chip lv-${lv} ${hidden.has(lv) ? 'off' : ''}`}
              onClick={() => toggleLevel(lv)}
            >
              {lv}
            </button>
          ))}
        </span>
      </header>
      {open && (
        <div
          className="eventlog-scroll"
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          }}
        >
          {visible.map((e) => (
            <div
              key={e.id}
              className={`log-row lv-${e.level} ${e.data !== undefined ? 'has-data' : ''}`}
              onClick={() => e.data !== undefined && setExpanded(expanded === e.id ? null : e.id)}
            >
              <span className="log-time mono">{time(e.ts)}</span>
              <span className={`log-dot lv-${e.level}`} />
              <span className="log-src mono">{e.source}</span>
              <span className="log-msg">{e.message}</span>
              {expanded === e.id && e.data !== undefined && (
                <pre className="log-data mono">{JSON.stringify(e.data, null, 2)}</pre>
              )}
            </div>
          ))}
          {visible.length === 0 && <div className="muted log-empty">no events yet</div>}
        </div>
      )}
    </section>
  );
}

function time(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}
