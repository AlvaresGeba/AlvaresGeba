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

import os
import ssl
import logging
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

from command_router import handle_text_command

BASE_DIR = Path(__file__).parent
CERT_DIR = BASE_DIR / "certs"

# Whisper runs as a separate service (stt_service.py, its own container).
STT_URL = os.environ.get("STT_URL", "http://127.0.0.1:9000")
STT_TIMEOUT = float(os.environ.get("STT_TIMEOUT", 60))

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


# 10 s of 16 kHz / 16-bit mono PCM = 320 000 bytes. The watch records at most
# 8 s; refuse anything much bigger before reading it into memory.
MAX_VOICE_BYTES = 10 * 16_000 * 2


class AudioRejected(Exception):
    """The STT service refused the audio (too short, silent, ...)."""


async def transcribe_audio(pcm: bytes) -> str:
    """Send raw PCM to the STT service and return the recognised text."""
    async with httpx.AsyncClient(timeout=STT_TIMEOUT) as client:
        response = await client.post(
            f"{STT_URL}/transcribe",
            content=pcm,
            headers={"content-type": "application/octet-stream"},
        )
    if response.status_code in (400, 413):
        raise AudioRejected(response.json().get("detail", response.text))
    response.raise_for_status()
    return response.json()["text"]


@app.post("/voice")
async def receive_voice(request: Request, device_id: str = "unknown"):
    """Voice entry point. The body is the watch's audio exactly as the BLE
    protocol delivers it: raw PCM, 16 kHz, signed 16-bit little-endian, mono,
    no WAV header (send it as application/octet-stream). The server turns it
    into text with Whisper and feeds that text into the same pipeline as
    /command."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")

    pcm = await request.body()
    if len(pcm) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")

    try:
        text = await transcribe_audio(pcm)
    except AudioRejected as exc:
        logger.info("Rejected audio from device=%s: %s", device_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Speech recognition failed")
        raise HTTPException(status_code=500, detail=f"STT failed: {exc}") from exc

    logger.info("Voice from device=%s text=%r", device_id, text)
    if not text:
        return {"text": "", "result": "ERROR: speech not recognized"}

    try:
        result = await handle_text_command(text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Command pipeline failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"text": text, "result": result}


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
    # In docker this is 0.0.0.0 and the VPN-only binding is done by the
    # port mapping in docker-compose.yml instead.
    uvicorn.run(
        app,
        host=os.environ.get("GATEWAY_HOST", "100.99.129.107"),  # your VPN-side IP
        port=int(os.environ.get("GATEWAY_PORT", 8443)),
        ssl_certfile=str(CERT_DIR / "server.pem"),
        ssl_keyfile=str(CERT_DIR / "server.key"),
        ssl_ca_certs=str(CERT_DIR / "ca.pem"),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
