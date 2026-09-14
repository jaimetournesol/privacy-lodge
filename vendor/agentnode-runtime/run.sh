#!/bin/bash
# agentnode service entry: X11 env on Linux, venv python, serve http+https.
cd "$(dirname "$0")"
if [ "$(uname)" != "Darwin" ]; then
  export DISPLAY="${DISPLAY:-:0}"
  [ -z "$XAUTHORITY" ] && [ -f "/run/user/$(id -u)/gdm/Xauthority" ] && export XAUTHORITY="/run/user/$(id -u)/gdm/Xauthority"
fi
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONUNBUFFERED=1
exec ./venv/bin/python -m agentnode serve
