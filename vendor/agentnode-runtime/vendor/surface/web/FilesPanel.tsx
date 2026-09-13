import { useEffect, useState } from 'react';
import type { FileEntry } from '../shared/protocol';
import { prettySize } from './registry/FileCard';
import { CodeBlock } from './registry/Code';

const TEXT_EXT = /\.(txt|md|json|jsonc|csv|tsv|log|js|mjs|cjs|ts|tsx|jsx|css|html|htm|svg|py|rb|go|rs|sh|zsh|yml|yaml|toml|ini|xml|sql|env)$/i;
const IMG_EXT = /\.(png|jpe?g|gif|webp|avif|ico|bmp)$/i;
const VID_EXT = /\.(mp4|webm|mov|m4v)$/i;
const AUD_EXT = /\.(mp3|wav|m4a|ogg|flac|aac)$/i;
const LANG_BY_EXT: Record<string, string> = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', tsx: 'tsx', jsx: 'jsx',
  py: 'python', rb: 'ruby', go: 'go', rs: 'rust', sh: 'bash', zsh: 'bash', md: 'markdown',
  json: 'json', jsonc: 'json', css: 'css', html: 'html', htm: 'html', svg: 'xml', xml: 'xml',
  yml: 'yaml', yaml: 'yaml', toml: 'toml', sql: 'sql', csv: 'csv',
};

export function FilesPanel({ wsId, wsName, onClose }: { wsId: string; wsName: string; onClose: () => void }) {
  const [dir, setDir] = useState('');
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<{ path: string; name: string; size: number } | null>(null);

  const load = (path: string) => {
    setError('');
    fetch(`/api/files?ws=${encodeURIComponent(wsId)}&path=${encodeURIComponent(path)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('could not list folder'))))
      .then((d) => {
        setDir(path);
        setEntries(d.entries);
      })
      .catch((e) => setError(String(e.message ?? e)));
  };

  useEffect(() => {
    load('');
    setPreview(null);
  }, [wsId]); // eslint-disable-line react-hooks/exhaustive-deps

  const crumbs = dir === '' ? [] : dir.split('/');

  return (
    <aside className="files-panel">
      <header className="files-head">
        <span className="files-title">FILES</span>
        <span className="files-ws mono">{wsName}</span>
        <button className="icon-btn" title="Refresh" onClick={() => load(dir)}>
          ⟳
        </button>
        <button className="icon-btn" title="Close" onClick={onClose}>
          ✕
        </button>
      </header>

      <div className="files-crumbs mono">
        <button className="crumb" onClick={() => load('')}>
          ~
        </button>
        {crumbs.map((c, i) => (
          <span key={i}>
            <span className="crumb-sep">/</span>
            <button className="crumb" onClick={() => load(crumbs.slice(0, i + 1).join('/'))}>
              {c}
            </button>
          </span>
        ))}
      </div>

      <div className="files-list">
        {error && <div className="ws-error">{error}</div>}
        {entries.map((e) => {
          const full = dir ? `${dir}/${e.name}` : e.name;
          return (
            <button
              key={e.name}
              className={`file-row ${e.kind}`}
              onClick={() => (e.kind === 'dir' ? load(full) : setPreview({ path: full, name: e.name, size: e.size }))}
            >
              <span className="file-row-glyph">{e.kind === 'dir' ? '▸' : '·'}</span>
              <span className="file-row-name">{e.name}</span>
              {e.kind === 'file' && <span className="file-row-size mono">{prettySize(e.size)}</span>}
            </button>
          );
        })}
        {entries.length === 0 && !error && <div className="muted files-empty">empty folder</div>}
      </div>

      {preview && <FilePreview wsId={wsId} file={preview} onClose={() => setPreview(null)} />}
    </aside>
  );
}

function FilePreview({
  wsId,
  file,
  onClose,
}: {
  wsId: string;
  file: { path: string; name: string; size: number };
  onClose: () => void;
}) {
  const url = `/api/file?ws=${encodeURIComponent(wsId)}&path=${encodeURIComponent(file.path)}`;
  const [text, setText] = useState<string | null>(null);
  const isText = TEXT_EXT.test(file.name) && file.size < 300_000;

  useEffect(() => {
    setText(null);
    if (isText) {
      fetch(url)
        .then((r) => r.text())
        .then(setText)
        .catch(() => setText('(could not read file)'));
    }
  }, [url, isText]);

  const ext = file.name.split('.').pop()?.toLowerCase() ?? '';

  return (
    <div className="preview-overlay" onClick={onClose}>
      <div className="preview-box" onClick={(e) => e.stopPropagation()}>
        <header className="preview-head">
          <span className="preview-name">{file.name}</span>
          <span className="mono muted">{prettySize(file.size)}</span>
          <a className="icon-btn" href={url} download={file.name} title="Download">
            ↓
          </a>
          <button className="icon-btn" onClick={onClose} title="Close">
            ✕
          </button>
        </header>
        <div className="preview-body">
          {IMG_EXT.test(file.name) ? (
            <img src={url} alt={file.name} />
          ) : VID_EXT.test(file.name) ? (
            <video src={url} controls autoPlay={false} />
          ) : AUD_EXT.test(file.name) ? (
            <audio src={url} controls />
          ) : file.name.toLowerCase().endsWith('.pdf') ? (
            <iframe src={url} title={file.name} className="preview-pdf" />
          ) : isText ? (
            text === null ? (
              <div className="muted">loading…</div>
            ) : (
              <CodeBlock code={text} lang={LANG_BY_EXT[ext] ?? 'text'} bare />
            )
          ) : (
            <div className="muted preview-noview">
              no inline preview for this type — use the download button
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
