FROM node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553
LABEL ai.tournesol.privacylodge.runtime-contract="2"
ARG CODEX_VERSION=0.154.0
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    AGENTNODE_HOME=/data/agentnode CODEX_HOME=/data/agentnode/codex LODGE_MODE=1 HOME=/data
WORKDIR /opt/agentnode
COPY docker/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock && pip freeze > /opt/python-dependencies.txt \
    && npm install -g @openai/codex@${CODEX_VERSION}
COPY . .
RUN cd vendor/surface && npm ci --no-audit --no-fund \
    && cd /opt/agentnode && python scripts/install_surface_presentation.py vendor/surface --build
RUN chmod +x docker/entrypoint.sh \
    && dpkg-query -W > /opt/os-dependencies.txt \
    && codex --version > /opt/codex-version.txt \
    && node --version > /opt/node-version.txt
ENTRYPOINT ["/opt/agentnode/docker/entrypoint.sh"]
