#!/usr/bin/env python3
"""Patch hermes-agent so a file you send over Matrix lands in the agent's WORKING ROOT.

THE BEHAVIOUR WE WANT
---------------------
You send the agent a PDF. You then say "summarise the PDF". The agent should be able to open
`report.pdf` — the thing you named — in the directory it actually works in.

WHAT UPSTREAM DOES
------------------
`_handle_media_message` caches the bytes via `cache_document_from_bytes()` (and the image /
audio equivalents), which write to `$HERMES_HOME/cache/documents/doc_<uuid12>_<name>` and hand
the agent that path. The file is reachable, but it is in a cache directory under a mangled
name, nowhere near where the agent's relative paths resolve.

WHAT THIS PATCH DOES
--------------------
After caching, it also places the file in the profile's working root under its original name
(de-duplicated, never overwriting), and points the agent at THAT path. The cache copy stays,
so anything upstream that reasons about the cache is unaffected. A hard link is used when the
two live on the same filesystem — which they do here, both under /data — so this costs no
extra disk for large files.

WHY THE CALL SITE AND NOT THE CACHE HELPERS
-------------------------------------------
`cache_document_from_bytes` is shared with the MCP tool and other platform adapters. Patching
it would drop MCP resource fetches into the workspace too, which is not what was asked for.
Patching the Matrix call site keeps this to "files the owner sent the agent".

PROFILE AWARENESS
-----------------
The working root is read from the CURRENT profile's config.yaml (`terminal.cwd`, which the
entrypoint sets per agent), resolved through `get_hermes_home()` — which does re-resolve
inside a profile scope. `TERMINAL_CWD` from the environment is only a fallback, because under
`gateway.multiplex_profiles` the process environment holds the DEFAULT profile's value and
would file every agent's uploads into the first agent's directory.

Fail-loud by design — see the sibling patches.
"""

import sys
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else
              "/opt/hermes/agent/plugins/platforms/matrix/adapter.py")

ANCHOR = '''            except Exception as e:
                logger.warning("[Matrix] Failed to cache media: %s", e)
'''

CALL = '''
            # Privacy Lodge: put it where the agent actually works. See
            # docker/agent/patches/matrix-media-to-workspace.py.
            if cached_path:
                try:
                    cached_path = _pp_deliver_to_workspace(cached_path, body)
                except Exception as exc:
                    logger.warning(
                        "[Matrix] could not place media in the working root: %s", exc
                    )
'''

# Must be a MODULE-LEVEL def: _handle_media_message is a method, and inserting plain
# functions above it would land them inside the class body.
HELPER_ANCHOR = "def _sanitize_matrix_html(html: str) -> str:"

HELPER = '''def _pp_workspace_dir():
    """The CURRENT profile's working root, or None.

    Read from the profile's own config.yaml rather than os.environ: under
    gateway.multiplex_profiles the process environment carries the DEFAULT profile's
    TERMINAL_CWD, so every agent's uploads would land in the first agent's directory.
    get_hermes_home() re-resolves inside the active profile scope, so it is the right anchor.
    """
    import os

    try:
        from hermes_constants import get_hermes_home
        import yaml

        cfg_path = Path(get_hermes_home()) / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        cwd = str(((cfg.get("terminal") or {}).get("cwd") or "")).strip()
        if cwd:
            return Path(cwd)
    except Exception:
        pass
    cwd = (os.environ.get("TERMINAL_CWD") or "").strip()
    return Path(cwd) if cwd else None


_PP_CACHE_PREFIX = re.compile(r"^(?:doc|img|image|audio|video)_[0-9a-f]{6,}_")


def _pp_workspace_name(cached_path, body):
    """Pick the filename the OWNER would recognise, sanitised to a bare name."""
    for candidate in (body, Path(cached_path).name):
        name = Path(str(candidate or "").replace("\\x00", "").strip()).name
        name = _PP_CACHE_PREFIX.sub("", name)
        if name and name not in {".", ".."} and "/" not in name:
            return name
    return Path(cached_path).name


def _pp_deliver_to_workspace(cached_path, body):
    """Hard-link (or copy) the cached file into the working root; return the new path.

    Never overwrites: an existing name gets " (2)", " (3)", ... Returns the original cached
    path unchanged if there is no working root or the placement fails, so a failure here can
    only cost the nicer path, never the file.
    """
    import os
    import shutil

    workspace = _pp_workspace_dir()
    if workspace is None:
        return cached_path
    workspace.mkdir(parents=True, exist_ok=True)

    name = _pp_workspace_name(cached_path, body)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    target = workspace / name
    n = 2
    while target.exists():
        target = workspace / (f"{stem} ({n}){dot}{ext}" if dot else f"{stem} ({n})")
        n += 1

    resolved = target.resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise ValueError(f"refusing to write outside the working root: {name!r}")

    try:
        os.link(cached_path, target)          # same volume — no second copy of the bytes
    except OSError:
        shutil.copy2(cached_path, target)
    logger.info("[Matrix] placed incoming file in the working root: %s", target)
    return str(target)


'''

MARKER = "_pp_deliver_to_workspace"


def main() -> int:
    src = TARGET.read_text()

    if MARKER in src:
        print(f"matrix-media-to-workspace: already applied to {TARGET}")
        return 0

    if src.count(ANCHOR) != 1 or src.count(HELPER_ANCHOR) != 1:
        print(
            "matrix-media-to-workspace: FAILED — expected exactly one media-cache except "
            f"block and one _handle_media_message definition in {TARGET}; found "
            f"{src.count(ANCHOR)} and {src.count(HELPER_ANCHOR)}.\n"
            "hermes-agent has changed. Re-read _handle_media_message and update the anchors.",
            file=sys.stderr,
        )
        return 1

    patched = src.replace(ANCHOR, ANCHOR + CALL, 1)
    patched = patched.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)

    compile(patched, str(TARGET), "exec")

    TARGET.write_text(patched)
    print(f"patched: {TARGET} — incoming media now lands in the agent's working root")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
