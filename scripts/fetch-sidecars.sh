#!/usr/bin/env bash
#
# fetch-sidecars.sh — fetch the two Privacy Lodge sidecar binaries for local dev.
#
#   tuwunel    — Matrix homeserver, extracted from the upstream OCI image
#   tor        — C-tor daemon, copied from the system install (or apt-installed)
#   turnserver — coturn for 1:1 voice (OPTIONAL — box runs fine without it)
#
# Binaries land in $PRIVACY_LODGE_BIN_DIR, defaulting to the app's runtime bin dir:
#   $HOME/.local/share/ai.tournesol.privacylodge/bin
#
# Usage:
#   ./scripts/fetch-sidecars.sh              # fetch both (idempotent)
#   ./scripts/fetch-sidecars.sh --force      # re-fetch even if present
#   ./scripts/fetch-sidecars.sh --uninstall  # remove both binaries
#
set -euo pipefail

BIN_DIR="${PRIVACY_LODGE_BIN_DIR:-$HOME/.local/share/ai.tournesol.privacylodge/bin}"
TUWUNEL_IMAGE="ghcr.io/matrix-construct/tuwunel:latest"
LIVEKIT_IMAGE="livekit/livekit-server:v1.13.1"
LKJWT_IMAGE="ghcr.io/element-hq/lk-jwt-service:0.2.0"

# tor is PINNED to a current release, not copied from the build machine's system tor.
# Shipping "whatever tor the build box had" meant a box built on a stale distro shipped an
# EOL tor — and EOL tor (0.4.8.x, dead on the network after 2026-09-01) can't federate, so
# peers had to hand-upgrade tor to connect. We fetch the Tor Expert Bundle (the Tor Project's
# official standalone tor) at a pinned version, and REFUSE to ship anything below TOR_MIN.
# Bump TOR_EB_VER from https://www.torproject.org/download/tor/ (it names the bundled tor).
TOR_EB_VER="15.0.18"   # Tor Expert Bundle (Tor Browser) version → bundles tor 0.4.9.11
TOR_MIN="0.4.9.5"      # hard floor: below this is EOL/too-old and won't stay on the network

# ---------------------------------------------------------------- colors ----
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m' C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m' C_RED=$'\033[31m' C_BLUE=$'\033[34m' C_BOLD=$'\033[1m'
else
  C_RESET='' C_GREEN='' C_YELLOW='' C_RED='' C_BLUE='' C_BOLD=''
fi
info() { printf '%s==>%s %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s ok %s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()  { printf '%s err%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

# ----------------------------------------------------------------- flags ----
FORCE=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --force)     FORCE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) err "unknown flag: $arg (try --help)"; exit 2 ;;
  esac
done

# ------------------------------------------------------------- uninstall ----
if [[ "$UNINSTALL" == 1 ]]; then
  info "Removing sidecar binaries from $BIN_DIR"
  for bin in tuwunel tor turnserver caddy livekit-server lk-jwt-service; do
    if [[ -e "$BIN_DIR/$bin" ]]; then
      rm -f "$BIN_DIR/$bin"
      ok "removed $BIN_DIR/$bin"
    else
      warn "$BIN_DIR/$bin not present — nothing to do"
    fi
  done
  if [[ -d "$BIN_DIR/tor-libs" ]]; then
    rm -rf "$BIN_DIR/tor-libs"
    ok "removed $BIN_DIR/tor-libs"
  fi
  rmdir "$BIN_DIR" 2>/dev/null && ok "removed empty dir $BIN_DIR" || true
  exit 0
fi

mkdir -p "$BIN_DIR"
info "Sidecar bin dir: ${C_BOLD}$BIN_DIR${C_RESET}"
if [[ -z "${PRIVACY_LODGE_BIN_DIR:-}" ]]; then
  info "Override with:   export PRIVACY_LODGE_BIN_DIR=\"$BIN_DIR\""
fi

# verify <path> — true if the binary runs and reports a version
verify() { "$1" --version >/dev/null 2>&1; }

# tor ships with its own libevent/libssl/libcrypto (the Expert Bundle links them from the
# system, with NO rpath — checked with readelf). On a host that happens to have them, the
# bare binary works; on a bare CI runner it dies loading libevent, which read as "failed
# verify (unknown)", fell back to apt tor 0.4.8.10, and tripped the EOL floor — every
# Release run since v0.1.8. So the bundle's libs land in $BIN_DIR/tor-libs and every run
# of tor — here and in the supervisor — points LD_LIBRARY_PATH at them. Harmless when the
# dir is absent (system-tor path): the loader just falls through to the system libs.
TOR_LIBS="$BIN_DIR/tor-libs"
tor_verify() { LD_LIBRARY_PATH="$TOR_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$1" --version >/dev/null 2>&1; }

# tor_ver <path>  → the dotted tor version ("0.4.9.11") or empty.
tor_ver() { LD_LIBRARY_PATH="$TOR_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$1" --version 2>/dev/null | head -n1 | grep -oE '[0-9]+(\.[0-9]+){3}' | head -n1; }
# ver_ge A B  → success if version A >= version B (dotted numeric, via sort -V).
ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

FAILURES=0

# --------------------------------------------------------------- tuwunel ----
fetch_tuwunel() {
  local dest="$BIN_DIR/tuwunel"

  if [[ "$FORCE" == 0 && -x "$dest" ]] && verify "$dest"; then
    ok "tuwunel already present ($("$dest" --version 2>/dev/null | head -n1)) — skipping"
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    err "docker is required to extract tuwunel from $TUWUNEL_IMAGE"
    return 1
  fi

  info "Pulling $TUWUNEL_IMAGE"
  docker pull --quiet "$TUWUNEL_IMAGE" >/dev/null || { err "docker pull failed"; return 1; }

  info "Extracting /usr/bin/tuwunel from image"
  local cid
  cid="$(docker create "$TUWUNEL_IMAGE")" || { err "docker create failed"; return 1; }
  if ! docker cp "$cid:/usr/bin/tuwunel" "$dest"; then
    docker rm -f "$cid" >/dev/null 2>&1 || true
    err "failed to copy tuwunel out of the image"
    return 1
  fi
  docker rm -f "$cid" >/dev/null

  chmod 0755 "$dest"
  if verify "$dest"; then
    ok "tuwunel installed: $("$dest" --version 2>/dev/null | head -n1)"
  else
    err "tuwunel was copied but '$dest --version' failed (wrong arch or missing libs?)"
    return 1
  fi
}

# ------------------------------------------------------------------- tor ----
# Ship a PINNED, current tor (Expert Bundle), never "whatever the build box had". A too-old
# tor silently breaks federation (EOL tor is dropped from the network), which is invisible
# until a peer can't connect — so we also enforce TOR_MIN and refuse to ship below it.
fetch_tor() {
  local dest="$BIN_DIR/tor"

  if [[ "$FORCE" == 0 && -x "$dest" ]] && tor_verify "$dest"; then
    local cur; cur="$(tor_ver "$dest")"
    if [[ -n "$cur" ]] && ver_ge "$cur" "$TOR_MIN"; then
      ok "tor already present ($cur ≥ $TOR_MIN) — skipping"
      return 0
    fi
    warn "present tor (${cur:-unknown}) is below floor $TOR_MIN — refetching a current one"
  fi

  # 1) Preferred: the pinned Tor Expert Bundle — deterministic + current on every build host.
  #    Prebuilt only for these arches; anything else falls through to system tor.
  local eb_arch=""
  case "$(uname -m)" in
    x86_64|amd64)  eb_arch="linux-x86_64" ;;
    aarch64|arm64) eb_arch="linux-aarch64" ;;
    i686|i386)     eb_arch="linux-i686" ;;
  esac
  if [[ -n "$eb_arch" ]] && command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1; then
    local url="https://archive.torproject.org/tor-package-archive/torbrowser/${TOR_EB_VER}/tor-expert-bundle-${eb_arch}-${TOR_EB_VER}.tar.gz"
    local tmp; tmp="$(mktemp -d)"
    info "Fetching pinned Tor Expert Bundle ${TOR_EB_VER} (${eb_arch})"
    if curl -fsSL "$url" -o "$tmp/teb.tgz" && tar -xzf "$tmp/teb.tgz" -C "$tmp" tor 2>/dev/null && [[ -x "$tmp/tor/tor" ]]; then
      cp "$tmp/tor/tor" "$dest" && chmod 0755 "$dest"
      # The libs tor was built against travel WITH it (see TOR_LIBS above). Copy only the
      # shared objects — the bundle's tor/ dir also holds pluggable transports and geoip
      # data we don't ship.
      mkdir -p "$TOR_LIBS"
      cp "$tmp"/tor/*.so* "$TOR_LIBS"/ 2>/dev/null || true
      rm -rf "$tmp"
      local v; v="$(tor_ver "$dest")"
      if tor_verify "$dest" && [[ -n "$v" ]] && ver_ge "$v" "$TOR_MIN"; then
        ok "tor installed from Expert Bundle: $v (libs in tor-libs/)"
        return 0
      fi
      warn "Expert-Bundle tor failed verify/floor (${v:-unknown}) — trying system tor"
    else
      rm -rf "$tmp"
      warn "Expert Bundle download failed (offline? arch not built?) — trying system tor"
    fi
  fi

  # 2) Fallback: system tor / apt — accepted ONLY if it meets the floor.
  find_system_tor() {
    command -v tor 2>/dev/null && return 0
    local p
    for p in /usr/sbin/tor /usr/local/sbin/tor /usr/bin/tor; do
      [[ -x "$p" ]] && { echo "$p"; return 0; }
    done
    return 1
  }

  local sys_tor=""
  if sys_tor="$(find_system_tor)"; then
    info "Copying system tor from $sys_tor"
  elif command -v apt-get >/dev/null 2>&1; then
    warn "No system tor found — installing via apt (this will prompt for sudo)"
    sudo apt-get install -y tor || { err "apt-get install tor failed"; return 1; }
    sys_tor="$(find_system_tor)" || { err "tor installed but binary not found"; return 1; }
  else
    err "No pinned bundle and no system tor available."
    err "Install the Tor Expert Bundle manually:"
    err "  1. Download for your platform: https://www.torproject.org/download/tor/"
    err "  2. Extract and copy the 'tor' binary to: $dest"
    err "  3. chmod 0755 $dest"
    return 1
  fi

  cp "$sys_tor" "$dest" || { err "failed to copy $sys_tor"; return 1; }
  chmod 0755 "$dest"
  # A system tor was built against system libs — stale Expert-Bundle libs in tor-libs
  # would shadow them at runtime. Clear the dir so LD_LIBRARY_PATH points at nothing.
  rm -rf "$TOR_LIBS"
  if ! tor_verify "$dest"; then
    err "tor was copied but '$dest --version' failed"
    rm -f "$dest"; return 1
  fi
  local v; v="$(tor_ver "$dest")"
  if [[ -z "$v" ]] || ! ver_ge "$v" "$TOR_MIN"; then
    err "system tor is ${v:-unparseable} — below the floor $TOR_MIN and cannot federate."
    err "  0.4.8.x and older are EOL and dropped from the Tor network (after 2026-09-01)."
    err "  Fix: install a current tor from the Tor Project apt repo (deb.torproject.org),"
    err "  or let the pinned Expert Bundle download succeed (needs network + curl/tar)."
    rm -f "$dest"; return 1
  fi
  ok "tor installed: $v"
}

# ------------------------------------------------------------- turnserver ----
# Optional: 1:1 voice. A missing turnserver leaves the box fully functional
# (chat + federation), just without calls — so this never fails the script.
fetch_turn() {
  local dest="$BIN_DIR/turnserver"

  if [[ "$FORCE" == 0 && -x "$dest" ]] && verify "$dest"; then
    ok "turnserver already present ($("$dest" --version 2>/dev/null | head -n1)) — skipping"
    return 0
  fi

  local sys_turn=""
  for p in "$(command -v turnserver 2>/dev/null || true)" /usr/bin/turnserver /usr/sbin/turnserver /usr/local/bin/turnserver; do
    [[ -n "$p" && -x "$p" ]] && { sys_turn="$p"; break; }
  done

  if [[ -z "$sys_turn" ]] && command -v apt-get >/dev/null 2>&1; then
    warn "No system coturn found — installing via apt (prompts for sudo)"
    sudo apt-get install -y coturn || { warn "apt-get install coturn failed — voice will be unavailable"; return 1; }
    for p in /usr/bin/turnserver /usr/sbin/turnserver; do [[ -x "$p" ]] && { sys_turn="$p"; break; }; done
  fi

  if [[ -z "$sys_turn" ]]; then
    warn "No coturn available — the box will run WITHOUT 1:1 voice."
    warn "Install it later with: sudo apt-get install coturn   (then re-run this script)"
    return 1
  fi

  cp "$sys_turn" "$dest" || { warn "failed to copy $sys_turn — voice unavailable"; return 1; }
  chmod 0755 "$dest"
  if verify "$dest"; then
    ok "turnserver installed: $("$dest" --version 2>/dev/null | head -n1)"
  else
    warn "turnserver copied but '$dest --version' failed — voice may not work"
    return 1
  fi
}

# --------------------------------------------------------------- caddy ------
# The federation fed-proxy: TLS-terminates inbound federation + enforces the
# paired-peer allowlist. Without it the box runs (chat works) but cannot accept
# inbound federation — so it's strongly recommended, not strictly required.
fetch_caddy() {
  local dest="$BIN_DIR/caddy"
  if [[ "$FORCE" == 0 && -x "$dest" ]] && verify "$dest"; then
    ok "caddy already present ($("$dest" version 2>/dev/null | head -n1)) — skipping"
    return 0
  fi
  local sys=""
  for p in "$(command -v caddy 2>/dev/null || true)" /usr/bin/caddy /usr/local/bin/caddy; do
    [[ -n "$p" && -x "$p" ]] && { sys="$p"; break; }
  done
  if [[ -n "$sys" ]]; then
    cp "$sys" "$dest" && chmod 0755 "$dest" && { ok "caddy installed: $("$dest" version 2>/dev/null | head -n1)"; return 0; }
  fi
  # No system caddy: extract from the official image (same trick as tuwunel).
  if command -v docker >/dev/null 2>&1; then
    info "Extracting caddy from caddy:2.8-alpine"
    docker pull --quiet caddy:2.8-alpine >/dev/null 2>&1 || true
    local cid
    cid="$(docker create caddy:2.8-alpine 2>/dev/null)" || { warn "couldn't get caddy — inbound federation will be off"; return 1; }
    docker cp "$cid:/usr/bin/caddy" "$dest" >/dev/null 2>&1
    docker rm -f "$cid" >/dev/null 2>&1
    [[ -x "$dest" ]] && chmod 0755 "$dest" && verify "$dest" && { ok "caddy installed: $("$dest" version 2>/dev/null | head -n1)"; return 0; }
  fi
  warn "No caddy available — inbound federation (pairing) will be OFF until installed."
  return 1
}

# --------------------------------------------------------------- livekit ----
# Optional: group calls (Element Call / LiveKit SFU). A missing livekit-server
# leaves the box fully functional (chat + federation + 1:1 voice), just without
# group calls — so this never fails the script. Extracted from the upstream
# image with the same docker create/cp trick as tuwunel/caddy.
fetch_livekit() {
  local dest="$BIN_DIR/livekit-server"
  if [[ "$FORCE" == 0 && -x "$dest" ]] && verify "$dest"; then
    ok "livekit-server already present ($("$dest" --version 2>/dev/null | head -n1)) — skipping"
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    warn "docker required to extract livekit-server from $LIVEKIT_IMAGE — group calls unavailable"
    return 1
  fi
  info "Extracting /livekit-server from $LIVEKIT_IMAGE"
  docker pull --quiet "$LIVEKIT_IMAGE" >/dev/null 2>&1 || { warn "docker pull $LIVEKIT_IMAGE failed — group calls unavailable"; return 1; }
  local cid
  cid="$(docker create "$LIVEKIT_IMAGE" 2>/dev/null)" || { warn "docker create $LIVEKIT_IMAGE failed — group calls unavailable"; return 1; }
  docker cp "$cid:/livekit-server" "$dest" >/dev/null 2>&1
  docker rm -f "$cid" >/dev/null 2>&1
  [[ -x "$dest" ]] && chmod 0755 "$dest" && verify "$dest" && { ok "livekit-server installed: $("$dest" --version 2>/dev/null | head -n1)"; return 0; }
  warn "couldn't extract livekit-server — group calls will be OFF until installed"
  return 1
}

# ---------------------------------------------------------------- lk-jwt ----
# Optional: the token service paired with livekit-server. Validates a caller's
# Matrix OpenID token and mints a LiveKit JWT. Like livekit, never fails the
# script. The image has no shell/version flag, so we don't `verify` it — a
# present, executable file is enough.
fetch_lkjwt() {
  local dest="$BIN_DIR/lk-jwt-service"
  if [[ "$FORCE" == 0 && -x "$dest" ]]; then
    ok "lk-jwt-service already present — skipping"
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    warn "docker required to extract lk-jwt-service from $LKJWT_IMAGE — group calls unavailable"
    return 1
  fi
  info "Extracting /lk-jwt-service from $LKJWT_IMAGE"
  docker pull --quiet "$LKJWT_IMAGE" >/dev/null 2>&1 || { warn "docker pull $LKJWT_IMAGE failed — group calls unavailable"; return 1; }
  local cid
  cid="$(docker create "$LKJWT_IMAGE" 2>/dev/null)" || { warn "docker create $LKJWT_IMAGE failed — group calls unavailable"; return 1; }
  docker cp "$cid:/lk-jwt-service" "$dest" >/dev/null 2>&1
  docker rm -f "$cid" >/dev/null 2>&1
  [[ -x "$dest" ]] && chmod 0755 "$dest" && { ok "lk-jwt-service installed"; return 0; }
  warn "couldn't extract lk-jwt-service — group calls will be OFF until installed"
  return 1
}

fetch_tuwunel || FAILURES=$((FAILURES + 1))
fetch_tor     || FAILURES=$((FAILURES + 1))
fetch_turn    || warn "voice (coturn) not installed — this is OPTIONAL, box still works"
fetch_caddy   || warn "caddy (fed-proxy) not installed — pairing/federation will be unavailable"
fetch_livekit || warn "livekit-server not installed — group calls (Element Call) OPTIONAL, box still works"
fetch_lkjwt   || warn "lk-jwt-service not installed — group calls (Element Call) OPTIONAL, box still works"

echo
if [[ "$FAILURES" -gt 0 ]]; then
  err "$FAILURES sidecar(s) failed — see messages above"
  exit 1
fi
ok "${C_BOLD}All sidecars ready in $BIN_DIR${C_RESET}"
info "If you use a custom dir, make sure the app sees it:"
printf '    export PRIVACY_LODGE_BIN_DIR="%s"\n' "$BIN_DIR"
