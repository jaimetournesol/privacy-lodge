#!/bin/bash
set -e
cd "$(dirname "$0")"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# NVM may return nonzero while selecting a version; diagnose through npm below.
if [ -s "$NVM_DIR/nvm.sh" ]; then . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true; fi
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec ./venv/bin/python -m agentnode.surface_hub
