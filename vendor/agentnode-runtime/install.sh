#!/bin/bash
# Bootstrap Python, provision bundled Surface and configure this node.
# ./install.sh --role control --backend claude --model opus --services
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
[ -d venv ] || "${PYTHON:-python3}" -m venv venv
./venv/bin/python -m pip install -q -r requirements.txt
args=("$@")
if [ -n "${SURFACE_DIR:-}" ]; then args+=(--surface-dir "$SURFACE_DIR"); fi
exec ./venv/bin/python -m agentnode setup --install-deps "${args[@]}"
