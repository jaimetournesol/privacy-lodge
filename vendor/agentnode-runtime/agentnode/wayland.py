"""Wayland (GNOME) host backend: screen frames + pointer/keyboard through the
xdg-desktop-portal RemoteDesktop/ScreenCast portals and PipeWire.

Needs the system python's gi (GLib/Gio/Gst). First use pops a consent dialog on
that machine; with persist_mode=2 the portal hands back a restore token that
is saved in ~/.agentnode/wayland-token so later starts are silent.
"""
import os
import random
import threading
import time
from pathlib import Path

from .wayland_restore import repair_selection

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

TOKEN_FILE = Path(os.environ.get("AGENTNODE_HOME", Path.home() / ".agentnode")) / "wayland-token"
BUS = "org.freedesktop.portal.Desktop"
OBJ = "/org/freedesktop/portal/desktop"
BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}


class WaylandDesktop:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.session = None
        self.pipe = None
        self.closed_sub = None
        self.stream_node = None
        self.size = (0, 0)
        self.frame = None            # latest RGB bytes
        self.frame_size = (0, 0)
        self.error = None
        self.ready = threading.Event()
        self.loop = GLib.MainLoop()
        threading.Thread(target=self.loop.run, daemon=True).start()
        self.rd = Gio.DBusProxy.new_sync(self.bus, Gio.DBusProxyFlags.NONE, None, BUS, OBJ, "org.freedesktop.portal.RemoteDesktop", None)
        self.sc = Gio.DBusProxy.new_sync(self.bus, Gio.DBusProxyFlags.NONE, None, BUS, OBJ, "org.freedesktop.portal.ScreenCast", None)

    # ---- portal plumbing ----
    def _request(self, proxy, method, args, opts):
        """Call a portal method and wait for its Response signal."""
        tok = "an%d" % random.randint(0, 1 << 30)
        opts = dict(opts, handle_token=GLib.Variant("s", tok))
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{tok}"
        result = {}
        done = threading.Event()

        def on_resp(conn, sender, obj, iface, sig, params):
            result["code"], result["data"] = params.unpack()
            done.set()
        sub = self.bus.signal_subscribe(BUS, "org.freedesktop.portal.Request", "Response", path, None, Gio.DBusSignalFlags.NONE, on_resp)
        try:
            proxy.call_sync(method, GLib.Variant.new_tuple(*args, GLib.Variant("a{sv}", opts)), Gio.DBusCallFlags.NONE, 15000, None)
            if not done.wait(180):
                raise TimeoutError(f"{method}: no response (consent dialog not answered?)")
        finally:
            self.bus.signal_unsubscribe(sub)
        if result["code"] != 0:
            raise RuntimeError(f"{method}: portal response {result['code']} (cancelled/denied)")
        return result["data"]

    def start(self):
        pending = TOKEN_FILE.with_name("wayland-token-pending")
        source = pending if pending.exists() else TOKEN_FILE
        tok = source.read_text().strip() if source.exists() else ""
        for attempt in range(2):
            if tok:
                repair_selection(self.bus, tok, TOKEN_FILE.parent)
            data = self._start_session(tok)
            tok = data.get("restore_token") or tok
            # Portal tokens rotate even if GNOME returns no screen. Keep the
            # candidate separately so a retry never falls back to consumed tokens.
            if tok:
                _save_private(pending, tok)
            streams = data.get("streams") or []
            if streams:
                self.stream_node, props = streams[0]
                self.size = tuple(props.get("size", (0, 0)))
                self._start_pipeline()
                if not self.ready.wait(10) or self.error:
                    raise RuntimeError(self.error or "screen capture produced no frame")
                if tok:
                    _save_private(TOKEN_FILE, tok)
                    pending.unlink(missing_ok=True)
                return self.size
            self._close_session()
            if attempt or not tok or not repair_selection(self.bus, tok, TOKEN_FILE.parent):
                raise RuntimeError("no screen from portal; reconnect the selected monitor or select a screen locally")

    def _start_session(self, tok):
        stok = "s%d" % random.randint(0, 1 << 30)
        self._request(self.rd, "CreateSession", [], {"session_handle_token": GLib.Variant("s", stok)})
        self.session = f"/org/freedesktop/portal/desktop/session/{self.sender}/{stok}"
        sess = GLib.Variant("o", self.session)
        self._request(self.rd, "SelectDevices", [sess], {"types": GLib.Variant("u", 3), "persist_mode": GLib.Variant("u", 2),
                                                       **({"restore_token": GLib.Variant("s", tok)} if tok else {})})
        self._request(self.sc, "SelectSources", [sess], {"types": GLib.Variant("u", 1), "multiple": GLib.Variant("b", False),
                                                       "cursor_mode": GLib.Variant("u", 2)})
        data = self._request(self.rd, "Start", [sess, GLib.Variant("s", "")], {})
        self.closed_sub = self.bus.signal_subscribe(
            BUS, "org.freedesktop.portal.Session", "Closed", self.session,
            None, Gio.DBusSignalFlags.NONE, self._on_closed)
        return data

    def _on_closed(self, *args):
        self.error = "screen-sharing session closed"
        self.ready.set()

    def _close_session(self):
        if self.closed_sub is not None:
            self.bus.signal_unsubscribe(self.closed_sub)
            self.closed_sub = None
        if self.session:
            try:
                self.bus.call_sync(BUS, self.session, "org.freedesktop.portal.Session",
                                   "Close", None, None, Gio.DBusCallFlags.NONE, 3000, None)
            except Exception:
                pass
            self.session = None

    def close(self):
        if self.pipe is not None:
            bus = self.pipe.get_bus()
            bus.remove_signal_watch()
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
        if getattr(self, "pw_fd", None) is not None:
            os.close(self.pw_fd)
            self.pw_fd = None
        self._close_session()
        self.loop.quit()
        self.frame = None

    def _on_pipeline_message(self, bus, message):
        if message.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            self.error = "screen capture pipeline stopped"
            self.ready.set()

    def _start_pipeline(self):
        Gst.init(None)
        fd = self.sc.call_with_unix_fd_list_sync("OpenPipeWireRemote", GLib.Variant.new_tuple(GLib.Variant("o", self.session), GLib.Variant("a{sv}", {})),
                                                 Gio.DBusCallFlags.NONE, -1, None, None)
        fdlist = fd[1]
        pw_fd = fdlist.get(fd[0].unpack()[0])
        try:
            self.pipe = Gst.parse_launch(
                f"pipewiresrc fd={pw_fd} path={self.stream_node} do-timestamp=true ! videoconvert ! video/x-raw,format=RGB ! "
                "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false")
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_pipeline_message)
        finally:
            # Keep this descriptor until pipeline shutdown (pipewiresrc uses it).
            self.pw_fd = pw_fd
        sink = self.pipe.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        self.pipe.set_state(Gst.State.PLAYING)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            self.frame = bytes(info.data)
            self.frame_size = (w, h)
            buf.unmap(info)
            self.ready.set()
        return Gst.FlowReturn.OK

    # ---- frames ----
    def image(self):
        """Latest frame as a PIL image, or None."""
        if self.error:
            raise RuntimeError(self.error)
        if self.frame is None:
            return None
        from PIL import Image
        w, h = self.frame_size
        try:
            return Image.frombytes("RGB", (w, h), self.frame, "raw", "RGB", 0, 1)
        except Exception:
            return None

    # ---- input (coordinates in stream pixels) ----
    def _call(self, method, *args):
        self.rd.call_sync(method, GLib.Variant.new_tuple(GLib.Variant("o", self.session), GLib.Variant("a{sv}", {}), *args),
                          Gio.DBusCallFlags.NONE, -1, None)

    def move(self, x, y):
        self._call("NotifyPointerMotionAbsolute", GLib.Variant("u", self.stream_node), GLib.Variant("d", float(x)), GLib.Variant("d", float(y)))

    def click(self, button="left", count=1):
        for _ in range(count):
            self._call("NotifyPointerButton", GLib.Variant("i", BTN[button]), GLib.Variant("u", 1))
            time.sleep(0.03)
            self._call("NotifyPointerButton", GLib.Variant("i", BTN[button]), GLib.Variant("u", 0))
            time.sleep(0.06)

    def scroll(self, amount):
        # NotifyPointerAxisDiscrete: axis 0 = vertical, steps positive = down
        steps = int(-amount)
        for _ in range(abs(steps)):
            self._call("NotifyPointerAxisDiscrete", GLib.Variant("u", 0), GLib.Variant("i", 1 if steps > 0 else -1))
            time.sleep(0.02)

    def keysym(self, sym, state):
        self._call("NotifyKeyboardKeysym", GLib.Variant("i", int(sym)), GLib.Variant("u", state))

    def key(self, combo: str):
        from Xlib import XK  # keysym table only; no X connection needed
        names = {"ctrl": "Control_L", "control": "Control_L", "alt": "Alt_L", "shift": "Shift_L", "super": "Super_L", "win": "Super_L",
                 "cmd": "Super_L", "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "space",
                 "backspace": "BackSpace", "delete": "Delete", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
                 "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next"}
        keys = [k.strip() for k in combo.replace("+", " ").split() if k.strip()]
        syms = []
        for k in keys:
            n = names.get(k.lower(), k)
            sym = XK.string_to_keysym(n) or XK.string_to_keysym(n.capitalize()) or (ord(n) if len(n) == 1 else 0)
            syms.append(sym)
        for s in syms:
            self.keysym(s, 1)
            time.sleep(0.02)
        for s in reversed(syms):
            self.keysym(s, 0)
            time.sleep(0.02)

    def type_text(self, text: str):
        for ch in text:
            if ch == "\n":
                self.key("Return")
                continue
            sym = ord(ch) if ord(ch) < 0x100 else (0x1000000 | ord(ch))
            self.keysym(sym, 1)
            time.sleep(0.012)
            self.keysym(sym, 0)
            time.sleep(0.012)


def _save_private(path, value):
    import tempfile
    import os
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


_desk = None
_lock = threading.Lock()
_retry_after = 0.0


def desktop() -> WaylandDesktop:
    global _desk, _retry_after
    with _lock:
        if _desk is not None and _desk.error:
            _desk.close()
            _desk = None
        if _desk is None:
            if time.monotonic() < _retry_after:
                raise RuntimeError("screen capture recovery waiting before retry")
            d = WaylandDesktop()
            try:
                d.start()
            except Exception:
                d.close()
                _retry_after = time.monotonic() + 20
                raise
            _desk = d
        return _desk
