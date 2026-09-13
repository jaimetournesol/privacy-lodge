"""Host-level tools: live screen stream (mss) and platform info."""
import asyncio
import platform
import struct
import sys
import threading
import time

from .util import log


class ScreenStreamer:
    def __init__(self, loop, fps: float = 8, frame_w: int = 1280, jpeg_q: int = 65, backend: str = "x11"):
        self.loop = loop
        self.backend = backend
        self.fps, self.frame_w, self.jpeg_q = fps, frame_w, jpeg_q
        self.clients: set = set()
        self.latest: bytes | None = None
        self.cursor = (65535, 65535)   # 65535 = unknown (UI hides the cursor)
        self.size = (0, 0)
        self.error: str | None = None
        self.frame_time = 0
        self.geometry_revision = 0
        self._geometry = None
        self._stopping = threading.Event()
        self._capture_context = None
        self._send_future = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    async def close(self):
        self._stopping.set()
        if self._send_future: self._send_future.cancel()
        await asyncio.gather(*(asyncio.wait_for(ws.close(code=1001),2) for ws in list(self.clients)),return_exceptions=True)
        self.clients.clear()
        await asyncio.to_thread(self._thread.join, 3)
        self.latest = None

    def _release_capture(self):
        if self._capture_context:
            try: self._capture_context.close()
            except Exception: pass
            self._capture_context = None

    def _pointer(self):
        try:
            if sys.platform == "darwin":
                from Quartz import CGEventCreate, CGEventGetLocation  # pyobjc, optional
                p = CGEventGetLocation(CGEventCreate(None))
                return int(p.x), int(p.y)
            from Xlib import display as xdisplay
            if not hasattr(self, "_xd"):
                self._xd = xdisplay.Display()
            p = self._xd.screen().root.query_pointer()
            return p.root_x, p.root_y
        except Exception:
            return (65535, 65535)

    def _run(self):
        try:
            self._capture()
        finally:
            self._release_capture()
            if hasattr(self, '_xd'):
                try: self._xd.close()
                except Exception: pass

    def _capture(self):
        import io
        import mss
        from PIL import Image
        period = 1.0 / self.fps
        sct = None                      # one capture context, reused (creating it per frame costs ~25 ms on X11)
        last_raw, last_jpeg, last_sent = None, None, 0.0
        while not self._stopping.is_set():
            if not self.clients:
                self._stopping.wait(0.25)
                continue
            t0 = time.time()
            try:
                if self.backend == "wayland":
                    from .wayland import desktop
                    d = desktop()
                    img = d.image()
                    if img is None:
                        self._stopping.wait(0.2)
                        continue
                    self.size = d.size or img.size
                    shot_size = img.size
                    raw = img.tobytes()
                else:
                    if sct is None:
                        sct = self._capture_context = mss.mss()
                    mon = sct.monitors[1]
                    self.size = (mon["width"], mon["height"])
                    shot = sct.grab(mon)
                    raw = shot.raw
                    shot_size = shot.size
                    img = None
                cx, cy = (65535, 65535) if self.backend == "wayland" else self._pointer()   # portal streams draw the cursor themselves
                if last_jpeg is not None and raw == last_raw:
                    # nothing changed on screen: reuse the last encoding (pointer moves still go out in the header)
                    jpeg = last_jpeg
                    if (cx, cy) == getattr(self, "_last_ptr", None) and time.time() - last_sent < 1.0:
                        dt = time.time() - t0
                        if dt < period:
                            self._stopping.wait(period - dt)
                        continue
                else:
                    if img is None:
                        img = Image.frombytes("RGB", shot_size, raw, "raw", "BGRX")
                    if img.width > self.frame_w:
                        img = img.resize((self.frame_w, int(img.height * self.frame_w / img.width)), Image.BILINEAR, reducing_gap=2.0)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=self.jpeg_q)
                    jpeg = buf.getvalue()
                    last_raw, last_jpeg = raw, jpeg
                self._last_ptr = (cx, cy)
                last_sent = time.time()
                if cx != 65535 and shot_size[0] != self.size[0]:
                    k = shot_size[0] / max(1, self.size[0])
                    cx, cy = int(cx * k), int(cy * k)
                header = struct.pack(">HHHH", max(0,min(cx, 65535)), max(0,min(cy, 65535)), min(shot_size[0], 65535), min(shot_size[1], 65535))
                if self._geometry != (self.size,shot_size):
                    self._geometry=(self.size,shot_size);self.geometry_revision+=1
                self.frame_time=time.time()
                frame = header + jpeg
                metadata=dict(type="frame",revision=self.geometry_revision,at=self.frame_time,width=self.size[0],height=self.size[1])
                self.latest_packet=(frame,metadata)
                self.latest = frame
                self.error = None
                if not self._stopping.is_set() and not self.loop.is_closed() and (self._send_future is None or self._send_future.done()):
                    self._send_future = asyncio.run_coroutine_threadsafe(self._send(frame,metadata), self.loop)
            except Exception as e:
                self.error = str(e)
                self.latest = None
                self._release_capture()
                sct = None
                log("capture error:", e)
                self._stopping.wait(20 if self.backend == "wayland" else 1)   # portals: don't spam consent dialogs
            dt = time.time() - t0
            if dt < period:
                self._stopping.wait(period - dt)

    async def _send(self, frame: bytes, metadata=None):
        async def send_one(ws):
            try:
                if metadata and ws.query_params.get("metadata")=="1":
                    await asyncio.wait_for(ws.send_json(metadata),2)
                await asyncio.wait_for(ws.send_bytes(frame), 2)
            except Exception:
                self.clients.discard(ws)
                try: await asyncio.wait_for(ws.close(code=1013), 1)
                except Exception: pass
        await asyncio.gather(*(send_one(ws) for ws in list(self.clients)))

    def screenshot_jpeg(self) -> bytes:
        import io
        import mss
        from PIL import Image
        if self.backend == "wayland":
            from .wayland import desktop
            img = desktop().image()
            if img is None:
                raise RuntimeError("no frame yet")
        else:
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        from .screenshots import encode
        return encode(img)['jpeg']


def platform_info() -> dict:
    return {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "hostname": platform.node()}
