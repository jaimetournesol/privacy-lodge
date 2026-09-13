import { useState } from 'react';

export function ImagePanel({ src, name }: { src: string; name?: string }) {
  const [zoom, setZoom] = useState(false);
  return (
    <>
      <img className="image-view" src={src} alt={name ?? ''} onClick={() => setZoom(true)} />
      {zoom && (
        <div className="lightbox" onClick={() => setZoom(false)}>
          <img src={src} alt={name ?? ''} />
        </div>
      )}
    </>
  );
}
