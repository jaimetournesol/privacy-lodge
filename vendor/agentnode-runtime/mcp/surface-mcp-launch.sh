#!/bin/bash
# Standard role "surface": tools from this node's running hub/workspace.
set -e
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true; fi
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
: "${SURFACE_DIR:?Surface directory missing from managed MCP config}"
export SURFACE_TOOLS_JSON="${SURFACE_STATE_DIR:-$SURFACE_DIR}/.surface-hub/tools.json"
[ -r "$SURFACE_TOOLS_JSON" ] || { echo "Surface tool manifest missing; start the Surface hub first" >&2; exit 1; }
exec node "$SURFACE_DIR/server/mcp-stdio.mjs"
