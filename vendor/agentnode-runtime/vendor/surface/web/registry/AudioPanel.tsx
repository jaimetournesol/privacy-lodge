export function AudioPanel({ src, name }: { src: string; name?: string }) {
  return (
    <div className="audio-view">
      {name && <span className="audio-name">{name}</span>}
      <audio src={src} controls preload="metadata" />
    </div>
  );
}
