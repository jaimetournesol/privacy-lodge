#!/usr/bin/env python3
"""Patch hermes-agent so the media-delivery allowlist follows the ACTIVE PROFILE.

THE GAP
-------
`_media_delivery_allowed_roots()` in gateway/platforms/base.py builds the set of directories
a model-emitted `MEDIA:<path>` may be delivered from. It is already half profile-aware — it
calls `_profile_cache_roots()`, which re-resolves through `get_hermes_dir()` and therefore
tracks whichever profile is currently in scope. But the operator-supplied roots come from a
single process-wide environment variable:

    extra_roots = os.environ.get("HERMES_MEDIA_ALLOW_DIRS", "")

Under `gateway.multiplex_profiles` one process serves every agent, so that variable cannot
differ per agent. Allowlisting each agent's working root that way means naming the shared
parent (`/data/workspace`) — which lets any agent attach a file out of any other agent's
working directory. Nothing secret lives there, but it is a boundary we can actually hold, so
we should hold it.

THE FIX
-------
Append the CURRENT profile's working root to the allowed roots, resolved the same way the
cache roots already are. `get_hermes_home()` re-resolves inside a profile scope (proven by the
crypto-store patch), so reading `terminal.cwd` out of that profile's own config.yaml gives the
right directory per agent, with no environment variable involved.

With this, HERMES_MEDIA_ALLOW_DIRS no longer needs to name the shared parent: agent `chris`
can send from /data/workspace/chris and nowhere else.

The environment variable keeps working exactly as before — this only ADDS a root, so an
operator allowlist set for other reasons is untouched.

Fail-loud by design — see the sibling patches.
"""

import sys
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else
              "/opt/hermes/agent/gateway/platforms/base.py")

ANCHOR = '''    roots.extend(_kanban_attachment_roots())
    extra_roots = os.environ.get(MEDIA_DELIVERY_ALLOW_DIRS_ENV, "")
'''

INSERT = '''    roots.extend(_kanban_attachment_roots())
    # Privacy Lodge: the ACTIVE profile's working root, so each agent may deliver from its own
    # directory and no other. See docker/agent/patches/media-allow-dirs-per-profile.py.
    _pp_root = _pp_profile_working_root()
    if _pp_root is not None:
        roots.append(_pp_root)
    extra_roots = os.environ.get(MEDIA_DELIVERY_ALLOW_DIRS_ENV, "")
'''

HELPER_ANCHOR = "def _media_delivery_allowed_roots() -> List[Path]:"

HELPER = '''def _pp_profile_working_root():
    """The working root of the profile currently in scope, or None.

    Resolved through get_hermes_home() rather than os.environ: under
    gateway.multiplex_profiles a single process serves every profile, so the process
    environment holds the DEFAULT profile's value and every agent would be allowed to
    deliver from the first agent's directory.

    terminal.cwd is the same key the entrypoint sets per agent and the same one the gateway
    bridges to TERMINAL_CWD, so this cannot drift from where the agent actually works.
    """
    try:
        from hermes_constants import get_hermes_home
        import yaml

        cfg = yaml.safe_load((Path(get_hermes_home()) / "config.yaml").read_text()) or {}
        cwd = str(((cfg.get("terminal") or {}).get("cwd") or "")).strip()
        if not cwd:
            return None
        root = Path(os.path.expanduser(cwd))
        return root if root.is_absolute() else None
    except Exception:
        return None


'''

MARKER = "_pp_profile_working_root"


def main() -> int:
    src = TARGET.read_text()

    if MARKER in src:
        print(f"media-allow-dirs-per-profile: already applied to {TARGET}")
        return 0

    if src.count(ANCHOR) != 1 or src.count(HELPER_ANCHOR) != 1:
        print(
            "media-allow-dirs-per-profile: FAILED — expected exactly one "
            "_media_delivery_allowed_roots definition and one extra-roots line in "
            f"{TARGET}; found {src.count(HELPER_ANCHOR)} and {src.count(ANCHOR)}.\\n"
            "hermes-agent has changed. If upstream now resolves the allowlist per profile, "
            "drop this patch; otherwise update the anchors.",
            file=sys.stderr,
        )
        return 1

    patched = src.replace(ANCHOR, INSERT, 1)
    patched = patched.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)

    compile(patched, str(TARGET), "exec")

    TARGET.write_text(patched)
    print(f"patched: {TARGET} — media allowlist now follows the active profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
