export function FileCard({ src, name, size, mime }: { src: string; name: string; size: number; mime: string }) {
  return (
    <a className="file-card" href={src} download={name}>
      <span className="file-glyph">{glyph(mime)}</span>
      <span className="file-meta">
        <span className="file-name">{name}</span>
        <span className="file-info mono">
          {prettySize(size)} · {mime.split(';')[0]}
        </span>
      </span>
      <span className="file-dl">↓</span>
    </a>
  );
}

function glyph(mime: string): string {
  if (mime.startsWith('image/')) return '🖼';
  if (mime.startsWith('video/')) return '🎞';
  if (mime.startsWith('audio/')) return '🎧';
  if (mime.includes('pdf')) return '📕';
  if (mime.includes('json') || mime.includes('csv') || mime.startsWith('text/')) return '📄';
  return '📦';
}

export function prettySize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
