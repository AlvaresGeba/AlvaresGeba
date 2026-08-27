"""
gateway.py

The only network-facing entry point. Everything else (MCP server, LLM
router) only listens on localhost. This process terminates mutual-TLS
connections coming from the phone app over the VPN tunnel and forwards
validated text commands into command_router.py.

Mutual TLS means: the phone must present a client certificate signed by
your own CA, or the TLS handshake fails before any application code runs
at all. A stolen bearer token is not enough to talk to this server - the
attacker would need the phone's private key file too.

See README.md for the openssl commands to generate:
  ca.pem            - your private certificate authority
  server.pem/.key    - this gateway's identity
  phone-client.pem/.key - the Android app's identity (bundled into the app)

Requires: pip install fastapi uvicorn[standard]
"""

from __future__ import annotations

import ssl
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

from command_router import handle_text_command

BASE_DIR = Path(__file__).parent
CERT_DIR = BASE_DIR / "certs"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")

app = FastAPI(title="cluster-command-gateway")


class CommandRequest(BaseModel):
    text: str
    # Optional client-side identifier for audit logs (e.g. "watch-01")
    device_id: str = "unknown"


@app.post("/command")
async def receive_command(payload: CommandRequest, request: Request):
    """Single endpoint the phone app calls. The client certificate has
    already been verified by the TLS layer before this code even runs -
    request.scope gives you access to the peer cert if you want to log
    which device connected."""
    logger.info("Command from device=%s text=%r", payload.device_id, payload.text)

    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Empty command text")

    try:
        result = await handle_text_command(payload.text)
    except Exception as exc:  # noqa: BLE001 - we want to report any failure to the client
        logger.exception("Command pipeline failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"result": result}


@app.get("/health")
async def health():
    """Unauthenticated-content but still behind mTLS - lets the phone app
    confirm the tunnel + cert are working before sending a real command."""
    return {"status": "ok"}


def build_ssl_context() -> ssl.SSLContext:
    """Require and verify a client certificate signed by our own CA."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certfile=str(CERT_DIR / "server.pem"),
        keyfile=str(CERT_DIR / "server.key"),
    )
    context.load_verify_locations(cafile=str(CERT_DIR / "ca.pem"))
    context.verify_mode = ssl.CERT_REQUIRED
    return context


if __name__ == "__main__":
    # Bind only to the VPN interface's address in production, not 0.0.0.0 -
    # e.g. your WireGuard/OpenVPN tunnel IP, so the gateway is unreachable
    # from anywhere outside the VPN even before TLS is considered.
    uvicorn.run(
        app,
        host="100.99.129.107",  # replace with your actual VPN-side IP
        port=8443,
        ssl_certfile=str(CERT_DIR / "server.pem"),
        ssl_keyfile=str(CERT_DIR / "server.key"),
        ssl_ca_certs=str(CERT_DIR / "ca.pem"),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
