#!/usr/bin/env python3
"""Patch hermes-agent so MATRIX_RECOVERY_KEY_OUTPUT_FILE honours the profile scope.

THE BUG (upstream, plugins/platforms/matrix/adapter.py)
-------------------------------------------------------
Upstream already knows this class of bug exists. `_scoped_recovery_key()` reads
MATRIX_RECOVERY_KEY through the scope-aware `get_secret()` precisely because, under
`gateway.multiplex_profiles`, `os.environ` carries the DEFAULT profile's value while the
secret scope carries the profile actually being started (their issue #69090).

Its sibling variable was missed. Both readers of MATRIX_RECOVERY_KEY_OUTPUT_FILE —
`_write_matrix_recovery_key_output_file()` and `_get_matrix_recovery_key_output_target()`
— still call bare `os.getenv`.

WHY IT MATTERS HERE
-------------------
The box's entrypoint deliberately unsets MATRIX_RECOVERY_KEY_OUTPUT_FILE in the process
environment once the default agent's key file exists (Hermes refuses to overwrite that
path, so offering it again is an error). Secondary agents carry their own output path in
their profile `.env` — which the bare `os.getenv` never sees. Result:

    Matrix: cross-signing keys are missing, but automatic bootstrap is skipped
    because MATRIX_RECOVERY_KEY_OUTPUT_FILE is not configured.

So a second agent comes online but is never cross-signed. Its device shows as "not
verified by its owner", and a client with strict device verification can wedge its send
queue rather than encrypt to an unsigned device — which is the failure mode this box
already had to fix once for the default agent.

THE FIX
-------
Resolve it the same way upstream resolves its sibling: through `get_secret()`, falling
back to `os.getenv` on an unscoped read (the default-profile startup path, where
`os.environ` IS that profile's own value).

Fail-loud by design — see the sibling patch matrix-per-profile-store.py.
"""

import sys
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else
              "/opt/hermes/agent/plugins/platforms/matrix/adapter.py")

# Appears twice: the writer and the target-resolver. Both need the same treatment.
OLD = 'output_file = os.getenv("MATRIX_RECOVERY_KEY_OUTPUT_FILE", "").strip()'
NEW = "output_file = _scoped_recovery_key_output_file()"

# Anchor the helper next to the sibling upstream already ships, so the two stay together.
ANCHOR = '''def _scoped_recovery_key() -> str:'''

HELPER = '''def _scoped_recovery_key_output_file() -> str:
    """Resolve MATRIX_RECOVERY_KEY_OUTPUT_FILE honoring the active profile's secret scope.

    Privacy Lodge patch — see docker/agent/patches/matrix-per-profile-recovery-output.py.
    Exactly the fix upstream applied to MATRIX_RECOVERY_KEY in _scoped_recovery_key()
    below (#69090); this variable was missed. Without it, a multiplexed profile whose
    output path lives in its own .env is told "not configured" and never bootstraps
    cross-signing.
    """
    try:
        return (get_secret("MATRIX_RECOVERY_KEY_OUTPUT_FILE") or "").strip()
    except UnscopedSecretError:
        return os.getenv("MATRIX_RECOVERY_KEY_OUTPUT_FILE", "").strip()


'''

MARKER = "def _scoped_recovery_key_output_file()"

EXPECTED_OCCURRENCES = 2


def main() -> int:
    src = TARGET.read_text()

    if MARKER in src:
        print(f"matrix-per-profile-recovery-output: already applied to {TARGET}")
        return 0

    found = src.count(OLD)
    if found != EXPECTED_OCCURRENCES or src.count(ANCHOR) != 1:
        print(
            "matrix-per-profile-recovery-output: FAILED — expected "
            f"{EXPECTED_OCCURRENCES} bare os.getenv reads of "
            f"MATRIX_RECOVERY_KEY_OUTPUT_FILE and one _scoped_recovery_key definition in\n"
            f"{TARGET}, found {found} and {src.count(ANCHOR)}.\n"
            "hermes-agent has changed. If upstream now resolves this through the secret\n"
            "scope, drop this patch; otherwise update OLD/ANCHOR to match.",
            file=sys.stderr,
        )
        return 1

    patched = src.replace(OLD, NEW).replace(ANCHOR, HELPER + ANCHOR, 1)

    # Fail here rather than at container start.
    compile(patched, str(TARGET), "exec")

    TARGET.write_text(patched)
    print(f"patched: {TARGET} — recovery-key output file now resolves per profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
