#!/usr/bin/env bash
# Launch one Privacy Lodge desktop box GUI as an isolated instance.
# Usage: start-box.sh <name> <port-offset>
set -u
NAME="$1"; OFFSET="$2"
# Derive from this script's location so the checkout can live anywhere.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${PRIVACY_LODGE_BIN:-$REPO/src-tauri/target/release/privacy-lodge}"
SHARED_BIN="${PRIVACY_LODGE_BIN_DIR:-$HOME/.local/share/ai.tournesol.privacylodge/bin}"
ROOT=/tmp/ppbox/$NAME
[ -x "$BIN" ] || { echo "[start-box] no binary at $BIN — build it: pnpm tauri build --no-bundle" >&2; exit 1; }
mkdir -p "$ROOT"
export XDG_DATA_HOME="$ROOT/share"
export XDG_CONFIG_HOME="$ROOT/config"
export XDG_CACHE_HOME="$ROOT/cache"
mkdir -p "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME"
export PRIVACY_LODGE_BIN_DIR="$SHARED_BIN"
export PRIVACY_LODGE_PORT_OFFSET="$OFFSET"
# GUI session
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DISPLAY="${DISPLAY:-:0}"
export GDK_BACKEND=x11
# mutter regenerates this filename on every login — glob it rather than pinning one.
if [ -z "${XAUTHORITY:-}" ]; then
  for f in "$XDG_RUNTIME_DIR"/.mutter-Xwaylandauth.*; do
    [ -e "$f" ] && export XAUTHORITY="$f" && break
  done
fi
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export WEBKIT_DISABLE_DMABUF_RENDERER=1   # avoid GPU/dmabuf issues over Xwayland
echo "[start-box] $NAME offset=$OFFSET data=$XDG_DATA_HOME"
exec "$BIN" >"$ROOT/box.log" 2>&1
