FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# curl: Claude Code installer. No ssh/procps here: commands run in the
# executor service, not in this container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-orchestrator.txt .
RUN pip install --no-cache-dir -r requirements-orchestrator.txt

# Claude Code CLI - command_router.py runs it headless (`claude -p`).
# The native build is a single file: move it out of /root so the non-root
# container user (your uid, see docker-compose.yml) can run it.
ARG CLAUDE_CODE_VERSION=2.1.296
ENV DISABLE_AUTOUPDATER=1
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
 && cp -L /root/.local/bin/claude /usr/local/bin/claude \
 && rm -rf /root/.local/bin/claude /root/.local/share/claude \
 && claude --version

# Home for whatever uid the container runs as. Your ~/.claude (the login you
# use in the terminal) is mounted at /home/app/.claude.
RUN mkdir -m 1777 /home/app
ENV HOME=/home/app

COPY gateway.py command_router.py ./

# certs/ is mounted at runtime (see docker-compose.yml) - never baked into
# the image.
EXPOSE 8443
# ~/.claude.json lives outside the mounted ~/.claude; without it Claude Code
# prints a "configuration file not found" warning on every run.
CMD ["sh", "-c", "[ -f ~/.claude.json ] || echo '{}' > ~/.claude.json; exec python gateway.py"]
