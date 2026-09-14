import { useEffect, useState } from 'react';
import type { WorkspaceInfo } from '../shared/protocol';
import { api } from './hubapi';
import type { Surface } from './useSurface';

const BUSY = new Set(['thinking', 'responding', 'tool']);

function ago(ts?: number): string {
  if (!ts) return 'never used';
  const s = (Date.now() - ts) / 1000;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function HomeScreen({ surface, onOpen }: { surface: Surface; onOpen: (id: string) => void }) {
  const { workspaces, status } = surface;
  const [renaming, setRenaming] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [creating, setCreating] = useState<'new' | 'attach' | null>(null);
  const [armedDelete, setArmedDelete] = useState<string | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!armedDelete) return;
    const t = setTimeout(() => setArmedDelete(null), 3000);
    return () => clearTimeout(t);
  }, [armedDelete]);

  const run = (fn: () => Promise<void>) => {
    setError('');
    fn().catch((err) => setError(String(err instanceof Error ? err.message : err)));
  };

  const submitRename = (id: string) =>
    run(async () => {
      await api('PATCH', `/api/workspaces/${id}`, { name: text });
      setRenaming(null);
      setText('');
    });

  const submitCreate = () =>
    run(async () => {
      const info: WorkspaceInfo = await api(
        'POST',
        '/api/workspaces',
        creating === 'attach' ? { path: text } : { name: text },
      );
      setCreating(null);
      setText('');
      onOpen(info.id);
    });

  const remove = (id: string) =>
    run(async () => {
      await api('DELETE', `/api/workspaces/${id}`);
      setArmedDelete(null);
    });

  return (
    <div className="home">
      <header className="home-head">
        <span className={`conn-dot ${status}`} />
        <span className="wordmark home-wordmark">SURFACE</span>
        <span className="muted home-sub">one agent per folder — pick a surface</span>
      </header>

      {error && <div className="ws-error home-error">{error}</div>}

      <div className="home-grid">
        {workspaces.map((w) => {
          const busy = w.phase && BUSY.has(w.phase);
          return (
            <div key={w.id} className={`home-card ${busy ? 'busy' : ''}`} onClick={() => renaming !== w.id && onOpen(w.id)}>
              <div className="home-card-top">
                {renaming === w.id ? (
                  <input
                    autoFocus
                    value={text}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') submitRename(w.id);
                      if (e.key === 'Escape') setRenaming(null);
                    }}
                    onBlur={() => setRenaming(null)}
                  />
                ) : (
                  <span className="home-card-name">{w.name}</span>
                )}
                <span className="home-card-actions" onClick={(e) => e.stopPropagation()}>
                  <button
                    className="icon-btn"
                    title="Rename"
                    onClick={() => {
                      setText(w.name);
                      setRenaming(w.id);
                    }}
                  >
                    ✎
                  </button>
                  <button
                    className={`icon-btn ${armedDelete === w.id ? 'danger' : ''}`}
                    title={w.attached ? 'Detach (folder is kept)' : 'Delete (moved to hub trash)'}
                    onClick={() => (armedDelete === w.id ? remove(w.id) : setArmedDelete(w.id))}
                  >
                    {armedDelete === w.id ? 'sure?' : '✕'}
                  </button>
                </span>
              </div>
              <div className="home-card-dir mono" title={w.dir}>
                {w.dir.replace(/^\/Users\/[^/]+/, '~')}
              </div>
              <div className="home-card-meta">
                {busy ? (
                  <span className="home-badge live">
                    <span className="spinner" /> {w.phase}
                  </span>
                ) : (
                  <span className="home-badge">{w.phase === 'error' ? '⚠ error' : 'idle'}</span>
                )}
                <span className="home-badge">{w.panels ?? 0} panels</span>
                {w.attached && <span className="ws-tag">linked</span>}
                <span className="home-ago">{ago(w.lastActive)}</span>
              </div>
            </div>
          );
        })}

        {creating ? (
          <div className="home-card home-card-new creating">
            <div className="ws-section">{creating === 'attach' ? 'attach existing folder' : 'new surface'}</div>
            <input
              autoFocus
              value={text}
              placeholder={creating === 'attach' ? '/path/to/project or ~/…' : 'name'}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submitCreate();
                if (e.key === 'Escape') {
                  setCreating(null);
                  setText('');
                }
              }}
            />
            <div className="ws-form-actions">
              <button className="ghost-btn" onClick={() => { setCreating(null); setText(''); }}>
                CANCEL
              </button>
              <button className="primary-btn" onClick={submitCreate} disabled={!text.trim()}>
                {creating === 'attach' ? 'Attach' : 'Create'}
              </button>
            </div>
          </div>
        ) : (
          <div className="home-card home-card-new">
            <button className="ghost-btn" onClick={() => setCreating('new')}>
              + NEW SURFACE
            </button>
            <button className="ghost-btn" onClick={() => setCreating('attach')}>
              ⇱ ATTACH FOLDER
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
