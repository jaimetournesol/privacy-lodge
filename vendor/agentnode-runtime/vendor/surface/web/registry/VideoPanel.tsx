export function VideoPanel({ src }: { src: string }) {
  return <video className="video-view" src={src} controls preload="metadata" />;
}
