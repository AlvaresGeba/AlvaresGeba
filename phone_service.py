"""
phone_service.py

The phone link, in its own container: nothing here but the connection to
the phone and the tools that drive it.

    WS   /phone/ws   the phone's control channel (phone_hub.PhoneHub). The
                     app connects to the orchestrator's mTLS /phone/ws,
                     which relays it here unchanged.
    POST /mcp        the "phone" MCP server, for Claude in the executor
    GET  /phones     connected phones (Server screen)
    GET  /health

Reachable only on an internal docker network shared with the orchestrator
and the executor; the Host header must be PHONE_ALLOWED_HOSTS
(DNS-rebinding protection).

send_sms / call ask the orchestrator's elevation level (ELEVATION_URL);
unreachable = "normal".
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

import uvicorn
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from phone_hub import PhoneHub, build_phone_mcp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("phone_service")

ELEVATION_URL = os.environ.get("ELEVATION_URL", "")
ALLOWED_HOSTS = [h for h in os.environ.get("PHONE_ALLOWED_HOSTS", "").split(",") if h]


def elevation_level() -> str:
    if not ELEVATION_URL:
        return "normal"
    try:
        with urllib.request.urlopen(ELEVATION_URL, timeout=2) as resp:
            return json.load(resp).get("level", "normal")
    except Exception:  # noqa: BLE001 - unreachable orchestrator = no elevation
        logger.warning("Elevation lookup failed - treating as normal")
        return "normal"


hub = PhoneHub()
phone_mcp = build_phone_mcp(hub, elevation_level, ALLOWED_HOSTS)


async def _phones(_request):
    return JSONResponse(hub.links())


async def _health(_request):
    return JSONResponse({"status": "ok", "connected": hub.connected()})


def build_app():
    app = phone_mcp.streamable_http_app()  # /mcp, with its session manager's lifespan
    app.router.routes += [
        WebSocketRoute("/phone/ws", hub.serve),
        Route("/phones", _phones),
        Route("/health", _health),
    ]
    return app


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("PHONE_HOST", "127.0.0.1"),
                port=int(os.environ.get("PHONE_PORT", 8002)))
