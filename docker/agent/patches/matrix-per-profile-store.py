#!/usr/bin/env python3
"""Patch hermes-agent so each PROFILE gets its own Matrix crypto store.

THE BUG (upstream, plugins/platforms/matrix/adapter.py)
-------------------------------------------------------
    # Store directory for E2EE keys and sync state.
    # Uses get_hermes_home() so each profile gets its own Matrix store.
    _STORE_DIR = _get_hermes_dir("platforms/matrix/store", "matrix/store")
    _CRYPTO_DB_PATH = _STORE_DIR / "crypto.db"

The comment states the intent; the code does the opposite. `_get_hermes_dir()` reads
HERMES_HOME, and `_profile_runtime_scope()` does rebind HERMES_HOME per profile — but
these are MODULE CONSTANTS, evaluated once at import, long before any profile scope is
entered. Every profile therefore shares whichever home was current at import: the
default profile's.

WHY IT MATTERS HERE
-------------------
A Privacy Lodge box runs several agents as `gateway.multiplex_profiles` profiles, each a
separate Matrix account on the box's own homeserver. With a shared crypto.db, agent #2
loads agent #1's olm account, `/keys/query` for its own mxid disagrees with the local
identity keys, and `_connect()` returns False. `_start_one_profile_adapters` counts zero
connected adapters and logs nothing at ERROR — so the agent is created, appears in the
app, has a room, and silently never answers.

THE FIX
-------
Resolve the paths lazily, on each use, instead of once at import. Every call site
(`.mkdir()`, `/ "crypto_store.pickle"`, `str()`, an f-string, and two `%s` log args) is
satisfied by a small proxy.

This runs at image build time and is deliberately fail-loud: if a `HERMES_AGENT_REF`
bump changes these lines, the build FAILS rather than silently shipping unpatched.
Re-check upstream on every bump — if they fix it, delete this patch and the COPY/RUN
pair in docker/agent/Dockerfile.
"""

import sys
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else
              "/opt/hermes/agent/plugins/platforms/matrix/adapter.py")

OLD = (
    '_STORE_DIR = _get_hermes_dir("platforms/matrix/store", "matrix/store")\n'
    '_CRYPTO_DB_PATH = _STORE_DIR / "crypto.db"\n'
)

NEW = '''class _PerProfilePath:
    """A Path that re-resolves against the CURRENT profile's HERMES_HOME on every use.

    Privacy Lodge patch — see docker/agent/patches/matrix-per-profile-store.py.
    Upstream resolved these paths at import time, so every multiplexed profile shared
    the first profile's olm account. The comment above says each profile gets its own
    store; this makes that true.
    """

    __slots__ = ("_resolve",)

    def __init__(self, resolve):
        object.__setattr__(self, "_resolve", resolve)

    # Non-dunder attribute access (.mkdir, .exists, .parent, ...) goes to a fresh Path.
    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    # Dunders bypass __getattr__, so the ones actually used are forwarded explicitly.
    def __truediv__(self, other):
        return self._resolve() / other

    def __rtruediv__(self, other):
        return other / self._resolve()

    def __fspath__(self):
        return self._resolve().__fspath__()

    def __str__(self):
        return str(self._resolve())

    def __repr__(self):
        return repr(self._resolve())

    def __eq__(self, other):
        return self._resolve() == other

    def __hash__(self):
        return hash(self._resolve())


def _matrix_store_dir():
    return _get_hermes_dir("platforms/matrix/store", "matrix/store")


_STORE_DIR = _PerProfilePath(_matrix_store_dir)
_CRYPTO_DB_PATH = _PerProfilePath(lambda: _matrix_store_dir() / "crypto.db")
'''

MARKER = "class _PerProfilePath:"


def main() -> int:
    src = TARGET.read_text()

    if MARKER in src:
        print(f"matrix-per-profile-store: already applied to {TARGET}")
        return 0

    if src.count(OLD) != 1:
        print(
            "matrix-per-profile-store: FAILED — the upstream lines this patch replaces\n"
            f"were not found exactly once in {TARGET}.\n"
            "hermes-agent has changed. Re-read the module-level _STORE_DIR /\n"
            "_CRYPTO_DB_PATH block: if upstream now resolves them per profile, drop this\n"
            "patch; otherwise update OLD/NEW to match.",
            file=sys.stderr,
        )
        return 1

    patched = src.replace(OLD, NEW)

    # Fail here rather than at container start.
    compile(patched, str(TARGET), "exec")

    TARGET.write_text(patched)
    print(f"patched: {TARGET} — Matrix crypto store now resolves per profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
