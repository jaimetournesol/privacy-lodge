# Privacy Lodge agent — Hermes Agent + Hermes WebUI, built from source, pinned together.
#
# WHY WE BUILD THIS OURSELVES rather than pulling the upstream images:
#
#   1. The WebUI imports hermes-agent internals directly (its own README warns that version
#      skew causes import/behaviour drift, and its compose file shares an agent-source volume
#      between the two containers). Two independently-tagged `:latest` images therefore
#      GUARANTEE drift. One image built from two pinned commits makes that impossible.
#   2. Size: `nousresearch/hermes-agent:latest` is ~3.9 GB. The dependency set we actually
#      need resolves to ~380 MB, which matters when a box runs on a Pi or a NAS.
#   3. Supply chain: the WebUI can execute shell commands. Vendoring a third-party `:latest`
#      with that capability into a privacy appliance is not something to do implicitly — we
#      pin the exact commits we reviewed.
#
# The container shares the BOX's network namespace (`--network container:privacy-lodge-box`),
# so it reaches tuwunel on 127.0.0.1:8118 and tor's SOCKS on 127.0.0.1:9150 with no new
# port exposure, and the WebUI's 127.0.0.1:8787 lands on the box's loopback where tor can
# map a (client-authorised) onion port onto it. Nothing here publishes a port itself.
FROM python:3.11-slim-trixie AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      git build-essential pkg-config libolm-dev ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Pinned revisions — bump BOTH together, never one alone (see reason 1 above).
# Full 40-char SHAs are required: `git fetch --depth 1 <sha>` rejects an abbreviation.
ARG HERMES_AGENT_REPO=https://github.com/NousResearch/hermes-agent.git
ARG HERMES_AGENT_REF=45c15d33e5ed7f42c6b4778ab8d34b40ad27cc86
ARG HERMES_WEBUI_REPO=https://github.com/nesquena/hermes-webui.git
ARG HERMES_WEBUI_REF=320789ae596a3963d726d90f6c7f3bc86f7f2d6d

WORKDIR /opt/hermes
RUN git init agent && cd agent \
    && git remote add origin "$HERMES_AGENT_REPO" \
    && git fetch --depth 1 origin "$HERMES_AGENT_REF" \
    && git checkout FETCH_HEAD
RUN git init webui && cd webui \
    && git remote add origin "$HERMES_WEBUI_REPO" \
    && git fetch --depth 1 origin "$HERMES_WEBUI_REF" \
    && git checkout FETCH_HEAD

# ── Patch: don't probe the endpoint on every keystroke ───────────────────────────────────
# Upstream's base-URL field runs `oninput="...;_scheduleOnboardingProbe()"`, which fires a
# probe 400ms after each keystroke. The probe calls _setOnboardingProbeState(), which calls
# _renderOnboardingBody() — and that REBUILDS the <input> you are typing into. The element
# is destroyed and replaced, so focus is lost; on Android the keyboard closes with it, which
# makes the field effectively untypable on a phone (and fires connection attempts the user
# never asked for).
#
# We keep both deliberate triggers: onblur (you've finished typing) and the explicit "Test
# connection" button. Only the per-keystroke probe goes.
#
# `grep -q` first so a future HERMES_WEBUI_REF bump FAILS THE BUILD rather than silently
# shipping without the patch — a sed that matches nothing exits 0 and would look fine.
RUN cd webui/static \
    && grep -q 'this.value;_scheduleOnboardingProbe()' onboarding.js \
    && sed -i 's/this\.value;_scheduleOnboardingProbe()/this.value/' onboarding.js \
    && ! grep -q 'this.value;_scheduleOnboardingProbe()' onboarding.js \
    && echo "patched: base-URL field no longer probes on every keystroke"

# ── Patch: give every PROFILE its own Matrix crypto store ────────────────────────────────
# Upstream resolves the Matrix store path into module constants at import time, so all
# profiles served by `gateway.multiplex_profiles` share the FIRST profile's olm account.
# A box runs one agent per profile, each its own Matrix account — so agent #2 loads agent
# #1's device keys, fails /keys/query for its own mxid, and _connect() returns False with
# nothing logged at ERROR. The agent exists, has a room, shows up in the app, and never
# answers. Rationale and the exact upstream lines are in the patch script's docstring.
#
# The script is fail-loud by design: it exits non-zero (failing the build) if the lines it
# replaces are not found exactly once, so a HERMES_AGENT_REF bump cannot silently ship
# unpatched. If upstream fixes this, delete the patch file and this COPY/RUN pair.
# The second patch is the same defect one layer down: upstream made MATRIX_RECOVERY_KEY
# scope-aware (their #69090) but missed MATRIX_RECOVERY_KEY_OUTPUT_FILE, so a secondary
# agent is told "not configured" and never bootstraps cross-signing — leaving its device
# unsigned, which is what wedges a phone's send queue.
COPY patches/ /opt/hermes/patches/
RUN ADAPTER=/opt/hermes/agent/plugins/platforms/matrix/adapter.py \
    && python3 /opt/hermes/patches/matrix-per-profile-store.py "$ADAPTER" \
    && python3 /opt/hermes/patches/matrix-per-profile-recovery-output.py "$ADAPTER" \
    && python3 /opt/hermes/patches/matrix-media-to-workspace.py "$ADAPTER" \
    && BASE=/opt/hermes/agent/gateway/platforms/base.py \
    && python3 /opt/hermes/patches/media-allow-dirs-per-profile.py "$BASE" \
    && grep -q 'class _PerProfilePath:' "$ADAPTER" \
    && grep -q 'def _scoped_recovery_key_output_file' "$ADAPTER" \
    && grep -q 'def _pp_deliver_to_workspace' "$ADAPTER" \
    && grep -q 'def _pp_profile_working_root' "$BASE"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# The venv lives OUTSIDE the agent's own checkout on purpose: hermes-agent's README warns
# that a venv inside the tree the agent operates on can be wiped by a relative-path command
# the agent runs against its own source, killing the runtime mid-session.
ENV UV_NO_CONFIG=1
RUN uv venv /opt/hermes/venv --python 3.11
ENV VIRTUAL_ENV=/opt/hermes/venv
# `.[all]` (no dev extras in a shipping image) plus the Matrix stack, which `[all]` does NOT
# pull: the box IS a Matrix homeserver, so this is the transport the agents actually use.
# mautrix's SOCKS support (aiohttp-socks, rdns) is what lets it resolve a .onion at all.
RUN cd /opt/hermes/agent \
    && uv pip install -e ".[all]" \
    && uv pip install 'mautrix[encryption]' aiohttp-socks asyncpg aiosqlite Markdown \
    && uv pip install -r /opt/hermes/webui/requirements.txt


FROM python:3.11-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive
# Runtime libs + the tools that make the agent able to actually DO things. libolm3 is
# mautrix's E2EE backend; git/ripgrep/ffmpeg are what its tools reach for constantly.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libolm3 git ripgrep ffmpeg curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# ── Toolchain ───────────────────────────────────────────────────────────────────────────
# The base above is what the AGENT RUNTIME needs. This block is what the agent needs to do
# actual work: compile something, build a dependency from source, inspect a database, poke
# at a PDF, run a node script. Without it the agent hits "gcc: not found" mid-task and has
# no way out — `sudo apt install` works (see the shim) but is lost on the next container
# recreate, so the agent silently re-does it forever.
#
# Costs roughly +700 MB. That is a deliberate trade against the size argument at the top of
# this file: a box that only relays chat does not need it, a box you actually work from
# does. Build with `--build-arg PL_DEV_TOOLS=0` for the lean image.
ARG PL_DEV_TOOLS=1
RUN if [ "$PL_DEV_TOOLS" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        `# compilers and build systems` \
        build-essential pkg-config cmake autoconf automake libtool m4 bison flex patch \
        `# headers the common "pip install X" / "./configure" builds actually ask for` \
        libssl-dev zlib1g-dev libffi-dev libbz2-dev liblzma-dev libreadline-dev \
        libsqlite3-dev libxml2-dev libxslt1-dev libjpeg-dev libpng-dev libfreetype6-dev \
        libyaml-dev libpq-dev libcurl4-openssl-dev libolm-dev \
        `# node, because a large share of real-world tooling is npm-shaped` \
        nodejs npm \
        `# process + network visibility — the agent had no ps, no free, no ss` \
        procps psmisc htop iproute2 iputils-ping dnsutils netcat-openbsd socat \
        `# archives and transfer` \
        unzip zip xz-utils bzip2 p7zip-full wget rsync openssh-client \
        `# everyday shell` \
        jq less nano vim-tiny file tree diffutils gnupg moreutils bc time \
        `# data + documents` \
        sqlite3 postgresql-client poppler-utils imagemagick \
        `# writing shell without a linter is how the entrypoint bugs happened` \
        shellcheck \
        fd-find bat \
      && rm -rf /var/lib/apt/lists/* \
      && ln -sf "$(command -v fdfind)" /usr/local/bin/fd \
      && ln -sf "$(command -v batcat)" /usr/local/bin/bat \
      && echo "dev toolchain installed"; \
    else echo "dev toolchain skipped (PL_DEV_TOOLS=$PL_DEV_TOOLS)"; fi

# ── Don't let the agent pip-install into its own runtime ─────────────────────────────────
# `python`, `pip` and friends resolve to /opt/hermes/venv (PATH puts it first) — that is
# HERMES ITSELF. `pip install` there can upgrade a dependency out from under the running
# agent and take the gateway down mid-conversation, with no obvious cause.
#
# So: ship `uv` in the runtime too (the build stage already used it) and give the agent a
# one-word way to get a scratch environment. `mkvenv` makes one under the workspace volume,
# where it also survives container recreates. The system interpreter stays reachable at its
# real path for anything that needs it.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN printf '%s\n' \
    '#!/bin/sh' \
    '# mkvenv [name] — scratch venv under the workspace. NEVER pip-install into' \
    '# /opt/hermes/venv: that is the running agent. See docker/agent/Dockerfile.' \
    'set -eu' \
    'name="${1:-venv}"' \
    'dir="${HERMES_WORKSPACE:-/data/workspace}/.venvs/$name"' \
    '# --seed is load-bearing: without pip INSIDE the venv, `pip` after activation falls' \
    '# through PATH to /opt/hermes/venv/bin/pip and installs into the running agent.' \
    'uv venv --seed "$dir" --python /usr/local/bin/python3' \
    'echo "$dir"' \
    'echo "activate with: . $dir/bin/activate"' \
    > /usr/local/bin/mkvenv \
    && chmod 0755 /usr/local/bin/mkvenv \
    && [ -x /usr/local/bin/uv ] \
    && echo "mkvenv installed"

# ── sudo ────────────────────────────────────────────────────────────────────────────────
# The agent already runs as uid 0 INSIDE this container, so it has full authority over its
# own environment — apt, pip, /usr/local/bin, all of it. What it lacks is the *command*:
# Hermes assumes sudo exists (84 of its files mention it; it even auto-rewrites bare sudo
# calls and has a SUDO_PASSWORD setting), so `sudo apt install x` fails with "sudo: not
# found" even though the privilege was never in question. That's a confusing failure to
# debug from a chat window.
#
# A shim, NOT the real thing. Installing sudo would add a setuid-root binary and a password
# path to an image whose only user is already root — pure attack surface for zero gain.
# This drops sudo's own flags and runs the command, which is exactly what real sudo would
# do here.
#
# To be explicit about the boundary: this grants nothing on the HOST. The container has no
# docker.sock, no host mounts, no added capabilities and is not privileged, and it must stay
# that way — any of those would make root-in-container mean root-on-your-machine.
RUN printf '%s\n' \
    '#!/bin/sh' \
    '# sudo shim — we are already uid 0. See docker/agent/Dockerfile for why.' \
    'while [ $# -gt 0 ]; do' \
    '  case "$1" in' \
    '    -u|--user|-g|--group|-p|--prompt|-C|--close-from|-r|--role|-t|--type) shift 2 ;;' \
    '    --) shift; break ;;' \
    '    -[!-]*|--*) shift ;;' \
    '    *) break ;;' \
    '  esac' \
    'done' \
    '[ $# -eq 0 ] && exec /bin/sh -l' \
    'exec "$@"' \
    > /usr/local/bin/sudo \
    && chmod 0755 /usr/local/bin/sudo \
    && /usr/local/bin/sudo -n true \
    && [ "$(/usr/local/bin/sudo -u root id -u)" = "0" ] \
    && echo "sudo shim installed"

COPY --from=build /opt/hermes /opt/hermes
COPY entrypoint.sh /opt/hermes/entrypoint.sh
# pl-config-set edits a profile's config.yaml WITHOUT reformatting it. `hermes config set`
# reformats and drops every comment (measured: a 61-line profile config came back as 25), and
# the entrypoint touches that file on every boot — so a lossy writer erodes the owner's
# settings one restart at a time. See the script's docstring.
COPY bin/pl-config-set /usr/local/bin/pl-config-set
# pl-auth-daemon runs a device-code sign-in (Codex et al) for an owner who is holding a PHONE,
# not a shell: it drives the blocking `hermes auth add` on a pty and publishes the user code
# to the box over /handoff. See the script's docstring for why a pty is mandatory.
COPY bin/pl-auth-daemon /usr/local/bin/pl-auth-daemon
RUN chmod +x /opt/hermes/entrypoint.sh /usr/local/bin/pl-config-set /usr/local/bin/pl-auth-daemon \
    && pl-config-set 2>&1 | grep -q "pl-config-set" \
    && python3 -c "import ast,sys; ast.parse(open('/usr/local/bin/pl-auth-daemon').read())" \
    && echo "pl-config-set + pl-auth-daemon installed"

# Everything stateful lives under /data so ONE volume carries it: config, the agent's
# profiles (= the agents themselves), sessions, memories, skills, and ~/.hermes/.env which
# holds the model API keys. Treat this volume as secret material — it is not covered by
# `pl-box backup` today, which tars only the box's own /data.
ENV HERMES_HOME=/data/hermes \
    HERMES_WEBUI_AGENT_DIR=/opt/hermes/agent \
    HERMES_WEBUI_PYTHON=/opt/hermes/venv/bin/python \
    HERMES_WEBUI_STATE_DIR=/data/hermes/webui \
    HERMES_WEBUI_HOST=127.0.0.1 \
    HERMES_WEBUI_PORT=8787 \
    HERMES_WORKSPACE=/data/workspace \
    PATH=/opt/hermes/venv/bin:$PATH

VOLUME /data

ENTRYPOINT ["/opt/hermes/entrypoint.sh"]
