#!/usr/bin/env bash
# Stage the (host-built) box binary + sidecars next to the Dockerfile, then build the image.
# Stage-1 approach: reuse prebuilt artifacts rather than compiling inside Docker (fast to
# iterate). A shipping image would build both in a multi-stage Dockerfile instead.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# Sidecars staged by the app. Rename (0.2.0): the app-data dir moved; a machine that ran the
# older app still has them under the old name — prefer the new dir, fall back to the old.
BIN_SRC="${PRIVACY_LODGE_BIN_DIR:-${PUREPRIVACY_BIN_DIR:-}}"
if [ -z "$BIN_SRC" ]; then
  for d in "$HOME/.local/share/ai.tournesol.privacylodge/bin" "$HOME/.local/share/ai.tournesol.pureprivacy/bin"; do
    [ -d "$d" ] && { BIN_SRC="$d"; break; }
  done
  [ -n "$BIN_SRC" ] || BIN_SRC="$HOME/.local/share/ai.tournesol.privacylodge/bin"
fi
# Relative to THIS checkout, not a hardcoded developer path (the previous default pointed at a
# clone that no longer exists). Override with APP_BIN=… for a binary built elsewhere.
APP_BIN="${APP_BIN:-$HERE/../src-tauri/target/release/privacy-lodge}"
IMG="${IMAGE:-privacy-lodge-box:dev}"

[ -x "$APP_BIN" ] || { echo "no box binary at $APP_BIN — build it first (pnpm tauri build)"; exit 1; }

rm -rf "$HERE/bin" "$HERE/privacy-lodge"
mkdir -p "$HERE/bin"
# turnserver is NOT staged — the image installs coturn via apt (correct libs) and symlinks
# it into the bin dir, so calls work without shipping the host's DB-linked turnserver.
for b in tor tuwunel caddy livekit-server lk-jwt-service; do
  if [ -f "$BIN_SRC/$b" ]; then cp "$BIN_SRC/$b" "$HERE/bin/$b"
  else echo "warn: sidecar '$b' missing at $BIN_SRC (box will run without it)"; fi
done
# The Expert-Bundle tor has no rpath and needs ITS libs, not whichever libevent the image's
# base happens to ship. The supervisor sets LD_LIBRARY_PATH to <bin>/tor-libs when present.
if [ -d "$BIN_SRC/tor-libs" ]; then cp -r "$BIN_SRC/tor-libs" "$HERE/bin/tor-libs"; fi
# pl-crypt: `pl-box backup --encrypt` runs it FROM this image, so an installed pl-box can
# seal/open bundles without a Rust toolchain on the host. Built alongside the app binary.
# Hard failure, not a warning: the published image is built and pushed BY HAND from this
# script, and an image without pl-crypt makes `backup --encrypt` and `restore <x.enc>` fail
# on every Hub install with "executable file not found" — discovered only when it's needed.
PL_CRYPT_BIN="$(dirname "$APP_BIN")/pl-crypt"
[ -x "$PL_CRYPT_BIN" ] || { echo "no pl-crypt at $PL_CRYPT_BIN — build it first: (cd src-tauri && cargo build --release --bin pl-crypt)"; exit 1; }
cp "$PL_CRYPT_BIN" "$HERE/bin/pl-crypt"
cp "$APP_BIN" "$HERE/privacy-lodge"

echo "staged $(du -sh "$HERE/bin" | cut -f1) sidecars + $(du -h "$HERE/privacy-lodge" | cut -f1) binary"
docker build -t "$IMG" "$HERE"
echo "✓ built $IMG"
echo
echo "Run it (reached only via its .onion — no ports to publish):"
echo "  ./pl-box up        # then: ./pl-box qr   (scan it in the phone app)"
echo "See ./pl-box help for status / logs / backup / restore."
