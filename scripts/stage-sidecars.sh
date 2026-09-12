#!/usr/bin/env bash
# stage-sidecars.sh — put the box's sidecar binaries where `tauri build` will BUNDLE them,
# so an installer (.deb/.rpm/.AppImage) is a working box with no manual fetch step.
#
# They land in src-tauri/sidecars/, which tauri.conf.json ships as a resource; at runtime
# supervisor::bin_dir() prefers <resources>/sidecars when it contains tuwunel.
#
#   ./scripts/stage-sidecars.sh
#
# PLATFORM REALITY (checked 2026-07-26): the homeserver, tuwunel, publishes **linux-gnu
# builds only** — no Windows, no macOS. lk-jwt-service is likewise Linux-only. So only Linux
# installers can carry a complete box today. On other platforms this script stages whatever
# exists and exits 0; the build still succeeds, and the box falls back to its app-data bin dir
# (the manual `fetch-sidecars.sh` route) — it simply won't be one-click.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/src-tauri/sidecars"
mkdir -p "$OUT"

case "$(uname -s)" in
  Linux) ;;
  *)
    echo "note: $(uname -s) — tuwunel/lk-jwt have no upstream build for this platform;"
    echo "      shipping the app without bundled sidecars (see script header)."
    exit 0
    ;;
esac

# Reuse the proven fetcher, pointed at our staging dir instead of the user's runtime bin dir.
PRIVACY_LODGE_BIN_DIR="$OUT" "$HERE/scripts/fetch-sidecars.sh" "$@"

# Self-hosted Element Call (feature J). Serving this from the box is what makes a call
# box-only; without it the phone would have to fetch the bundle from call.element.io — a
# third party on the clearnet, which contradicts our own privacy policy.
EC_VER="${EC_VER:-0.22.0}"
EC_DIR="$OUT/element-call"
if [ ! -f "$EC_DIR/index.html" ]; then
  echo "==> Fetching Element Call $EC_VER (AGPL-3.0) to serve from the box"
  tmp="$(mktemp -d)"
  if curl -fsSL -o "$tmp/ec.tar.gz" \
      "https://github.com/element-hq/element-call/releases/download/v$EC_VER/element-call-$EC_VER.tar.gz"; then
    mkdir -p "$EC_DIR"
    tar -xzf "$tmp/ec.tar.gz" -C "$EC_DIR" --strip-components=1 2>/dev/null \
      || tar -xzf "$tmp/ec.tar.gz" -C "$EC_DIR"
    [ -f "$EC_DIR/index.html" ] && echo " ok  element-call $EC_VER staged" \
      || echo "warn: element-call extracted but no index.html — group calls will fall back"
  else
    echo "warn: couldn't fetch element-call — group calls will have no bundle to serve"
  fi
  rm -rf "$tmp"
fi

# Ship the licence terms WITH the binaries they cover — required by Apache-2.0 §4 and BSD,
# and by AGPL for Element Call / lk-jwt (whose source offer lives in THIRD-PARTY-LICENSES.md).
cp -f "$HERE/THIRD-PARTY-LICENSES.md" "$OUT/" 2>/dev/null || true
mkdir -p "$OUT/licenses" && cp -f "$HERE/licenses/"*.txt "$OUT/licenses/" 2>/dev/null || true
cp -f "$HERE/LICENSE" "$OUT/LICENSE-Privacy Lodge.txt" 2>/dev/null || true

echo
echo "staged into $OUT:"
missing=0
for b in tor tuwunel caddy livekit-server lk-jwt-service; do
  if [ -f "$OUT/$b" ]; then
    chmod +x "$OUT/$b"
    printf '  %-16s %s\n' "$b" "$(du -h "$OUT/$b" | cut -f1)"
  else
    printf '  %-16s MISSING\n' "$b"
    # tuwunel is the homeserver: without it the bundle is not a box at all.
    [ "$b" = "tuwunel" ] && missing=1
  fi
done
if [ "$missing" = 1 ]; then
  echo "error: tuwunel missing — the bundle would install a non-working box" >&2
  exit 1
fi
