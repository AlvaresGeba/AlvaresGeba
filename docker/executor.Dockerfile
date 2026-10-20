FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# ssh: commands run on the host (and other cluster nodes) over SSH.
# procps: uptime / free (whitelist, get_system_status).
# curl: Claude Code installer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client procps curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-executor.txt .
RUN pip install --no-cache-dir -r requirements-executor.txt

# Claude Code CLI - command_router.py runs it headless (`claude -p`).
# The native build is a single file: move it out of /root so the non-root
# container user can run it.
ARG CLAUDE_CODE_VERSION=2.1.296
ENV DISABLE_AUTOUPDATER=1
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
 && cp -L /root/.local/bin/claude /usr/local/bin/claude \
 && rm -rf /root/.local/bin/claude /root/.local/share/claude \
 && claude --version

# The uid the container runs as (yours, see docker-compose.yml) gets a
# name and a home: ssh refuses to run without a passwd entry, and your
# ~/.claude (the login you use in the terminal) is mounted in that home.
ARG HOST_UID=1000
RUN useradd --uid "${HOST_UID}" --no-user-group --create-home executor
ENV HOME=/home/executor

COPY executor_agent.py command_router.py mcp_command_server.py node_metrics.py ./

# scripts/, commands_whitelist.md, the audit log and ssh/ are mounted at
# runtime (see docker-compose.yml) - they never get baked into the image.
ENV AGENT_HOST=0.0.0.0 AGENT_PORT=8000
EXPOSE 8000
# ~/.claude.json lives outside the mounted ~/.claude; without it Claude Code
# prints a "configuration file not found" warning on every run.
CMD ["sh", "-c", "[ -f ~/.claude.json ] || echo '{}' > ~/.claude.json; exec python executor_agent.py"]
