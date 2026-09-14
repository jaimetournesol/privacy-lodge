import { useEffect, useRef, useState } from 'react';
import type { ChatAnnotation, SurfaceComponent } from '../shared/protocol';

export interface Stroke {
  /** Normalized (0..1) points relative to the frozen snapshot. */
  points: [number, number][];
}

const MARKER = 'rgba(255, 93, 108, 0.85)';
const MAX_SNAPSHOT_PX = 1800;

// ---- capture ------------------------------------------------------------

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = rej;
    img.src = src;
  });
}

function drawToDataUrl(source: CanvasImageSource, w: number, h: number): string {
  const scale = Math.min(1, MAX_SNAPSHOT_PX / Math.max(w, h));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  canvas.getContext('2d')!.drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/png');
}

let captureSeq = 0;

function captureIframe(iframe: HTMLIFrameElement): Promise<string> {
  return new Promise((resolve, reject) => {
    const id = `cap-${captureSeq++}`;
    const timer = setTimeout(() => {
      window.removeEventListener('message', onMsg);
      reject(new Error('capture timed out'));
    }, 5000);
    const onMsg = (e: MessageEvent) => {
      if (e.data?.__surfaceCaptureRes !== id) return;
      clearTimeout(timer);
      window.removeEventListener('message', onMsg);
      if (e.data.dataUrl) resolve(e.data.dataUrl);
      else reject(new Error(e.data.error ?? 'capture failed'));
    };
    window.addEventListener('message', onMsg);
    iframe.contentWindow?.postMessage({ __surfaceCaptureReq: id }, '*');
  });
}

/**
 * Freeze a panel into a PNG data URL — like taking a screenshot of just that
 * panel. Strategy depends on what the panel is:
 *   image/video → exact pixels straight from the element;
 *   html/app (iframes) → the injected bridge rasterizes its own document;
 *   everything else → rasterize the shell-rendered DOM directly.
 * Returns null when nothing could be captured (annotation degrades to
 * geometry-only).
 */
function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return Promise.race([
    p,
    new Promise<never>((_, rej) => setTimeout(() => rej(new Error('capture timeout')), ms)),
  ]);
}

export async function capturePanel(
  comp: SurfaceComponent,
  bodyEl: HTMLElement,
): Promise<string | null> {
  try {
    if (comp.type === 'image') {
      const img = bodyEl.querySelector('img');
      if (img && img.naturalWidth) return drawToDataUrl(img, img.naturalWidth, img.naturalHeight);
    }
    if (comp.type === 'video') {
      const video = bodyEl.querySelector('video');
      if (video && video.videoWidth) return drawToDataUrl(video, video.videoWidth, video.videoHeight);
    }
    if (comp.type === 'html' || comp.type === 'app') {
      const iframe = bodyEl.querySelector('iframe');
      if (iframe) return await captureIframe(iframe);
      return null;
    }
    const { domToPng } = await import('modern-screenshot');
    return await withTimeout(domToPng(bodyEl, { backgroundColor: '#111116', scale: 1 }), 6000);
  } catch {
    return null;
  }
}

// ---- drawing ------------------------------------------------------------

/** Draws on top of whatever is already in the context — callers clear if needed. */
function drawStrokes(
  ctx: CanvasRenderingContext2D,
  strokes: Stroke[],
  w: number,
  h: number,
  live?: [number, number][],
): void {
  ctx.strokeStyle = MARKER;
  ctx.lineWidth = Math.max(3, w / 320);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  const all = live ? [...strokes, { points: live }] : strokes;
  for (const s of all) {
    if (s.points.length < 2) continue;
    ctx.beginPath();
    ctx.moveTo(s.points[0][0] * w, s.points[0][1] * h);
    for (const [x, y] of s.points.slice(1)) ctx.lineTo(x * w, y * h);
    ctx.stroke();
  }
}

function bbox(s: Stroke): { x: number; y: number; w: number; h: number } {
  let x0 = 1, y0 = 1, x1 = 0, y1 = 0;
  for (const [x, y] of s.points) {
    x0 = Math.min(x0, x); y0 = Math.min(y0, y);
    x1 = Math.max(x1, x); y1 = Math.max(y1, y);
  }
  const r = (n: number) => Math.round(n * 1000) / 1000;
  return { x: r(x0), y: r(y0), w: r(x1 - x0), h: r(y1 - y0) };
}

/** Burn the strokes into the snapshot and build the ChatAnnotation. */
export async function buildAnnotation(
  comp: SurfaceComponent,
  snapshot: string | null,
  strokes: Stroke[],
): Promise<ChatAnnotation> {
  const ann: ChatAnnotation = {
    componentId: comp.id,
    componentType: comp.type,
    title: comp.title,
    marks: strokes.map(bbox),
  };
  if (snapshot) {
    const img = await loadImage(snapshot);
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(img, 0, 0);
    drawStrokes(ctx, strokes, canvas.width, canvas.height);
    ann.imageDataUrl = canvas.toDataURL('image/png');
  }
  return ann;
}

// ---- the freeze overlay --------------------------------------------------

export function FreezeOverlay({
  comp,
  snapshot,
  onDone,
  onCancel,
}: {
  comp: SurfaceComponent;
  snapshot: string | null;
  onDone: (ann: ChatAnnotation) => void;
  onCancel: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [strokes, setStrokes] = useState<Stroke[]>([]);
  const live = useRef<[number, number][] | null>(null);

  const redraw = () => {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext('2d')!;
    ctx.clearRect(0, 0, c.width, c.height);
    drawStrokes(ctx, strokes, c.width, c.height, live.current ?? undefined);
  };

  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const rect = c.parentElement!.getBoundingClientRect();
    c.width = rect.width;
    c.height = rect.height;
    redraw();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(redraw, [strokes]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onCancel();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const norm = (e: React.PointerEvent): [number, number] => {
    const rect = canvasRef.current!.getBoundingClientRect();
    return [
      Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)),
      Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)),
    ];
  };

  return (
    <div className="freeze-overlay">
      {snapshot ? (
        <img className="freeze-snapshot" src={snapshot} alt="" draggable={false} />
      ) : (
        <div className="freeze-nosnap muted">live view — couldn’t freeze this panel; your marks are sent as regions</div>
      )}
      <canvas
        ref={canvasRef}
        className="freeze-canvas"
        onPointerDown={(e) => {
          e.preventDefault();
          try {
            e.currentTarget.setPointerCapture(e.pointerId);
          } catch {
            /* synthetic/injected events may carry uncapturable pointer ids */
          }
          live.current = [norm(e)];
        }}
        onPointerMove={(e) => {
          if (!live.current) return;
          live.current.push(norm(e));
          redraw();
        }}
        onPointerUp={() => {
          const pts = live.current; // capture now — the updater below runs lazily
          live.current = null;
          if (pts && pts.length > 1) setStrokes((s) => [...s, { points: pts }]);
        }}
        onPointerCancel={() => {
          const pts = live.current;
          live.current = null;
          if (pts && pts.length > 1) setStrokes((s) => [...s, { points: pts }]);
        }}
      />
      <div className="freeze-toolbar">
        <span className="freeze-hint">frozen — circle what you mean</span>
        <button className="ghost-btn" onClick={() => setStrokes([])} disabled={strokes.length === 0}>
          CLEAR
        </button>
        <button className="ghost-btn" onClick={onCancel}>
          CANCEL
        </button>
        <button
          className="primary-btn"
          disabled={strokes.length === 0}
          onClick={() => void buildAnnotation(comp, snapshot, strokes).then(onDone)}
        >
          Attach to chat
        </button>
      </div>
    </div>
  );
}
