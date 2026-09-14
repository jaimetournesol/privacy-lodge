import { useEffect, useRef, useState } from 'react';
import type { Surface } from './useSurface';

type Mode =
  | { kind: 'list' }
  | { kind: 'create' }
  | { kind: 'attach' }
  | { kind: 'rename'; id: string; current: string }
  | { kind: 'instructions'; id: string; name: string };

export function WorkspaceMenu({ surface }: { surface: Surface }) {
  const { workspaces, workspace, switchWorkspace } = surface;
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>({ kind: 'list' });
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [armedDelete, setArmedDelete] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) close();
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [open]);

  useEffect(() => {
    if (!armedDelete) return;
    const t = setTimeout(() => setArmedDelete(null), 3000);
    return () => clearTimeout(t);
  }, [armedDelete]);

  const close = () => {
    setOpen(false);
    setMode({ kind: 'list' });
    setText('');
    setError('');
    setArmedDelete(null);
  };

  const api = async (method: string, url: string, body?: unknown): Promise<any> => {
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error ?? `request failed (${res.status})`);
    return data;
  };

  const run = (fn: () => Promise<void>) => {
    setError('');
    fn().catch((err) => setError(String(err instanceof Error ? err.message : err)));
  };

  const submit = () =>
    run(async () => {
      if (mode.kind === 'create') {
        const info = await api('POST', '/api/workspaces', { name: text });
        switchWorkspace(info.id);
        close();
      } else if (mode.kind === 'attach') {
        const info = await api('POST', '/api/workspaces', { path: text });
        switchWorkspace(info.id);
        close();
      } else if (mode.kind === 'rename') {
        await api('PATCH', `/api/workspaces/${mode.id}`, { name: text });
        setMode({ kind: 'list' });
        setText('');
      } else if (mode.kind === 'instructions') {
        await api('PUT', `/api/instructions?ws=${encodeURIComponent(mode.id)}`, { text });
        setMode({ kind: 'list' });
        setText('');
      }
    });

  const openInstructions = (id: string, name: string) =>
    run(async () => {
      const data = await api('GET', `/api/instructions?ws=${encodeURIComponent(id)}`);
      setText(data.text ?? '');
      setMode({ kind: 'instructions', id, name });
    });

  const remove = (id: string) =>
    run(async () => {
      await api('DELETE', `/api/workspaces/${id}`);
      setArmedDelete(null);
    });

  return (
    <div className="ws-menu" ref={rootRef}>
      <button className="ws-trigger" onClick={() => (open ? close() : setOpen(true))} title={workspace?.dir}>
        <span className="ws-glyph">▣</span>
        {workspace?.name ?? '…'}
        <span className="ws-caret">{open ? '▴' : '▾'}</span>
      </button>

      {open && (
        <div className={`ws-dropdown ${mode.kind === 'instructions' ? 'ws-dropdown-wide' : ''}`}>
          {mode.kind === 'list' && (
            <>
              <div className="ws-section">workspaces</div>
              {workspaces.map((w) => (
                <div key={w.id} className={`ws-row ${w.id === workspace?.id ? 'active' : ''}`}>
                  <button
                    className="ws-row-name"
                    title={w.dir}
                    onClick={() => {
                      switchWorkspace(w.id);
                      close();
                    }}
                  >
                    {w.name}
                    {w.attached && <span className="ws-tag">linked</span>}
                    {w.phase && ['thinking', 'responding', 'tool'].includes(w.phase) && <span className="spinner" />}
                    {w.phase === 'error' && <span className="ws-err-dot" title="agent error">⚠</span>}
                  </button>
                  <span className="ws-row-actions">
                    <button className="icon-btn" title="Instructions (CLAUDE.md)" onClick={() => openInstructions(w.id, w.name)}>
                      ☰
                    </button>
                    <button
                      className="icon-btn"
                      title="Rename"
                      onClick={() => {
                        setText(w.name);
                        setMode({ kind: 'rename', id: w.id, current: w.name });
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
              ))}
              <div className="ws-footer">
                <button className="ghost-btn" onClick={() => { setText(''); setMode({ kind: 'create' }); }}>
                  + NEW
                </button>
                <button className="ghost-btn" onClick={() => { setText(''); setMode({ kind: 'attach' }); }}>
                  ⇱ ATTACH FOLDER
                </button>
              </div>
            </>
          )}

          {(mode.kind === 'create' || mode.kind === 'attach' || mode.kind === 'rename') && (
            <div className="ws-form">
              <div className="ws-section">
                {mode.kind === 'create' ? 'new workspace' : mode.kind === 'attach' ? 'attach existing folder' : `rename “${mode.current}”`}
              </div>
              <input
                autoFocus
                value={text}
                placeholder={mode.kind === 'attach' ? '/path/to/project or ~/…' : 'name'}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && submit()}
              />
              <div className="ws-form-actions">
                <button className="ghost-btn" onClick={() => { setMode({ kind: 'list' }); setError(''); }}>
                  BACK
                </button>
                <button className="primary-btn" onClick={submit} disabled={!text.trim()}>
                  {mode.kind === 'rename' ? 'Rename' : mode.kind === 'attach' ? 'Attach' : 'Create'}
                </button>
              </div>
            </div>
          )}

          {mode.kind === 'instructions' && (
            <div className="ws-form">
              <div className="ws-section">instructions for “{mode.name}” · CLAUDE.md</div>
              <textarea
                autoFocus
                value={text}
                rows={12}
                placeholder={'Standing instructions for this workspace’s agent…'}
                onChange={(e) => setText(e.target.value)}
              />
              <div className="ws-form-actions">
                <button className="ghost-btn" onClick={() => { setMode({ kind: 'list' }); setText(''); }}>
                  BACK
                </button>
                <button className="primary-btn" onClick={submit}>
                  Save
                </button>
              </div>
            </div>
          )}

          {error && <div className="ws-error">{error}</div>}
        </div>
      )}
    </div>
  );
}
