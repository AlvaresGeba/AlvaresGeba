FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# ssh: run_command/run_script can target other cluster nodes over SSH.
# procps: uptime / free (whitelist, get_system_status).
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client procps \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-executor.txt .
RUN pip install --no-cache-dir -r requirements-executor.txt

COPY mcp_command_server.py ./

# scripts/, commands_whitelist.md and the audit log are mounted at runtime
# (see docker-compose.yml) - they never get baked into the image.
ENV MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000
EXPOSE 8000
CMD ["python", "mcp_command_server.py"]
