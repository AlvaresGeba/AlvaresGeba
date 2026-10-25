# AlvaresGeba

The gateway: speech-to-text, orchestrator, executor agent and the phone's
MCP tools. The architecture overview is the header comment of
`docker-compose.yml`.

Tests: `.venv/bin/python -m pytest -q tests`

## Monitoring (Prometheus and Grafana)

Prometheus scrapes the orchestrator's metrics server (`orchestrator:9100`,
config in `monitoring/prometheus.yml`). Grafana shows that data; it talks
only to Prometheus and cannot reach the orchestrator.

1. Set the Grafana admin password in `.env` (compose refuses to start
   without it):

   ```
   GRAFANA_ADMIN_PASSWORD=<a long random password>
   ```

   Grafana reads it only when its database is first created. To change it
   later, use the UI or
   `docker compose exec grafana grafana cli admin reset-admin-password <new>`;
   editing `.env` alone does nothing.

2. Start both:

   ```
   docker compose up -d grafana prometheus
   ```

3. Both UIs listen on host loopback only (Grafana `127.0.0.1:3000`,
   Prometheus `127.0.0.1:9090`). From your machine:

   ```
   ssh -L 3000:127.0.0.1:3000 <host>
   ```

   then open http://localhost:3000 and log in as `admin`.

Dashboards and the datasource are provisioned from git
(`monitoring/grafana/provisioning`, `monitoring/grafana/dashboards`, mounted
read-only). UI edits cannot be saved: edit `monitoring/grafana/dashboards/*.json`
instead (Grafana picks up changes within 30 s). The "Geba gateway" dashboard
shows request latency (p50/p95) and rate by channel and outcome, the error
ratio, and the orchestrator's memory and CPU.
