#!/bin/sh
set -eu
runtime_uid="${LODGE_RUNTIME_UID:-1000}"
runtime_gid="${LODGE_RUNTIME_GID:-1000}"
case "$runtime_uid:$runtime_gid" in *[!0-9:]*|:*|*:) echo 'Invalid runtime ownership' >&2; exit 1;; esac
if [ "$runtime_uid" -eq 0 ]; then echo 'Agents must run as an unprivileged user' >&2; exit 1; fi
mkdir -p /data/agentnode
chown "$runtime_uid:$runtime_gid" /data /data/agentnode
if [ "${LODGE_ROLE:-control}" = control ]; then
  mkdir -p /handoff/agentnode
  chown "$runtime_uid:$runtime_gid" /handoff/agentnode
else
  chown "$runtime_uid:$runtime_gid" /peer
fi
exec gosu "$runtime_uid:$runtime_gid" python -m agentnode.lodge_runtime
