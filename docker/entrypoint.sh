#!/usr/bin/env bash
# Boot a Privacy Lodge box inside the container: a virtual display + session bus for the
# webkit-linked binary, then the app's headless path.
#
# Two ways to set up (feature A):
#  - INTERACTIVE (default, no PL_PASS): the box serves a one-page web setup on
#    http://127.0.0.1:<setup-port>/ (published to the host by docker-compose). You open it
#    in a browser, choose a username + password, and scan the QR with the phone app. The
#    setup page shuts down automatically once your phone signs in.
#  - NON-INTERACTIVE (PL_PASS set): provision straight from env (CI / scripted / testbed) and
#    print the phone-connect QR to the logs — the original behaviour, unchanged.
set -euo pipefail

# `docker run <box-image> <command…>` runs THAT command — a shell to look around, pl-crypt to
# seal a backup — instead of booting a box around it. The standard container contract; without
# it every argument was silently swallowed and a whole box (Xvfb, tor, tuwunel) came up
# instead. No arguments (compose passes none) = boot the box, exactly as before.
if [ "$#" -gt 0 ]; then exec "$@"; fi

# Rename (0.2.0): compose now passes PL_*; an older compose file still passes PP_*. Accept both.
PL_USER="${PL_USER:-${PP_USER:-}}"
PL_PASS="${PL_PASS:-${PP_PASS:-}}"
PL_BOX="${PL_BOX:-${PP_BOX:-}}"
PL_SECRETS_KEY="${PL_SECRETS_KEY:-${PP_SECRETS_KEY:-}}"

# No baked-in default account name — in the interactive flow the username comes from the
# web setup form; in the scripted flow the operator must state it explicitly.
PL_USER="${PL_USER:-}"
PL_BOX="${PL_BOX:-mybox}"
# Matches config.rs SETUP_PORT (+ PRIVACY_LODGE_PORT_OFFSET, which is 0 in the container).
SETUP_PORT="${PRIVACY_LODGE_SETUP_PORT:-8470}"

# INTERACTIVE unless an admin password is baked in via env.
INTERACTIVE=1
[ -n "${PL_PASS:-}" ] && INTERACTIVE=0

# Scripted provisioning needs an explicit username (there is no default).
if [ "$INTERACTIVE" = "0" ] && [ -z "$PL_USER" ]; then
  echo "[entrypoint] PL_PASS is set but PL_USER is not." >&2
  echo "[entrypoint] Set PL_USER to the account name your phone signs in with," >&2
  echo "[entrypoint] or leave PL_PASS unset to choose both in the browser setup page." >&2
  exit 1
fi

# The container owns its data dir. Named volumes are already root-owned (no-op); this fixes
# a bind-mount from a host user — tor refuses a hidden-service dir it doesn't own (exit 1).
chown -R "$(id -u):$(id -g)" /data 2>/dev/null || true
# Stable secrets key so secrets.json (AES-GCM) decrypts across restarts. Provide your own
# in production; the fallback keeps a single container reproducible but is NOT secret.
PL_SECRETS_KEY="${PL_SECRETS_KEY:-pureprivacy-docker-default-key-change-me=}"

# Virtual X display (the GUI renders here, unseen) + a session D-Bus webkit needs.
# `docker restart` reuses the container FS, so clear any stale X lock first — otherwise
# the second boot can't claim :99 and GTK init fails.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
Xvfb :99 -screen 0 1024x768x16 -nolisten tcp >/tmp/xvfb.log 2>&1 &
for i in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.2; done
[ -e /tmp/.X11-unix/X99 ] || { echo "[entrypoint] Xvfb failed to start:"; cat /tmp/xvfb.log; exit 1; }
eval "$(dbus-launch --sh-syntax)"
export DBUS_SESSION_BUS_ADDRESS DBUS_SESSION_BUS_PID

if [ "$INTERACTIVE" = "1" ] && [ ! -f /data/box.json ]; then
  # First run, interactive: point the user at the web setup page.
  echo "======================================================================"
  echo "  Privacy Lodge — finish setup in your browser:"
  echo ""
  echo "        http://127.0.0.1:${SETUP_PORT}/"
  echo ""
  echo "  Choose a username + password there, then scan the QR with the"
  echo "  Privacy Bolt app. This page closes itself once your phone signs in."
  echo "======================================================================"
else
  # Non-interactive (or a restart): print the phone-connect QR once the box has an onion.
  (
    onion=""
    for i in $(seq 1 300); do
      if [ -f /data/box.json ]; then
        onion="$(sed -n 's/.*"onion"[^"]*"\([a-z2-7]*\.onion\)".*/\1/p' /data/box.json 2>/dev/null | head -1)"
        [ -n "$onion" ] && break
      fi
      sleep 1
    done
    if [ -n "$onion" ]; then
      echo "======================================================================"
      echo "  Privacy Lodge box is up.  User: @${PL_USER}:${onion}"
      echo "  Scan this in the Privacy Bolt app to sign in (then type your password):"
      echo "======================================================================"
      # A LOGIN/setup handoff (privacybolt://connect?...) — the app pre-fills onion + user on
      # its sign-in screen. NOT the contact-pairing form (privacy-lodge:@user:onion), which a
      # not-yet-signed-in phone can't use — it just bounces back to login.
      qrencode -t ANSIUTF8 "privacybolt://connect?hs=${onion}&user=${PL_USER}" 2>/dev/null \
        || echo "  sign in manually with:  box @${PL_USER}:${onion}  + your password"
      echo "======================================================================"
    else
      echo "[entrypoint] box.json/onion not seen yet — check logs above for setup progress."
    fi
  ) &
fi

# Hand off to the box (AUTOSTART=1: resume a provisioned box, else serve web setup / env-provision).
if [ "$INTERACTIVE" = "1" ]; then
  # Interactive: no baked-in creds → the box serves the web setup page. Bind it to the
  # container (0.0.0.0) so docker's port publish (to the HOST's 127.0.0.1 only) can reach it;
  # a loopback bind inside the container would be unreachable via port publishing.
  exec env \
    PRIVACY_LODGE_AUTOSTART=1 \
    PRIVACY_LODGE_SETUP_BIND=0.0.0.0 \
    PRIVACY_LODGE_SECRETS_KEY="$PL_SECRETS_KEY" \
    /opt/privacy-lodge/privacy-lodge
else
  exec env \
    PRIVACY_LODGE_AUTOSTART=1 \
    PRIVACY_LODGE_PROVISION_USER="$PL_USER" \
    PRIVACY_LODGE_PROVISION_PASS="$PL_PASS" \
    PRIVACY_LODGE_PROVISION_BOX="$PL_BOX" \
    PRIVACY_LODGE_SECRETS_KEY="$PL_SECRETS_KEY" \
    /opt/privacy-lodge/privacy-lodge
fi
