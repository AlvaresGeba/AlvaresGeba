"""
gateway.py

The only network-facing entry point. Everything else (whisper, the
executor's MCP server) is reachable only on internal docker networks. This
process terminates mutual-TLS connections coming from the phone app over
the VPN tunnel and serves everything the app (WatchCommandApp) talks to:

    POST /command                 typed command -> Claude -> tools
    POST /command/audio           watch audio (raw PCM) -> Whisper -> same
    WS   /phone/ws                the phone's control channel (phone_hub.py)
    GET  /elevation, POST /elevation/{request,confirm,deny,revoke}
    GET  /monitor, /agent/tools   cluster health, the tools Claude has
    GET  /history/recent, /history/stats
    GET  /logs/recent, /logs/stream (Server-Sent Events)
    GET  /health

Mutual TLS means: the phone must present a client certificate signed by
your own CA, or the TLS handshake fails before any application code runs
at all. A stolen bearer token is not enough to talk to this server - the
attacker would need the phone's private key file too.

A second, plain-HTTP server (INTERNAL_PORT, not published) carries:
    /mcp                  the "phone" MCP server - loopback only, for the
                          Claude Code process running in this container
    /internal/elevation   the current elevation level, read-only, for the
                          executor

See README.md for the openssl commands to generate:
  ca.pem            - your private certificate authority
  server.pem/.key    - this gateway's identity
  phone-client.pem/.key - the Android app's identity (bundled into the app)
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import ssl
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel
from starlette.routing import Route

import command_router
from command_router import handle_text_command
from elevation import Elevation, ElevationError
from history import History
from log_buffer import BufferHandler, LogBuffer, tail_audit_log
from phone_hub import PhoneHub, build_phone_mcp

BASE_DIR = Path(__file__).parent
CERT_DIR = BASE_DIR / "certs"
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", BASE_DIR / "command_audit.log"))

# Whisper runs as a separate service (stt_service.py, its own container).
STT_URL = os.environ.get("STT_URL", "http://127.0.0.1:9000")
STT_TIMEOUT = float(os.environ.get("STT_TIMEOUT", 60))
INTERNAL_PORT = int(os.environ.get("INTERNAL_PORT", 8001))

STARTED_AT = time.time()

log_buffer = LogBuffer()
logging.basicConfig(level=logging.INFO)
logging.getLogger().addHandler(BufferHandler(log_buffer))
logger = logging.getLogger("gateway")

elevation = Elevation()
phones = PhoneHub()
# request id -> {"device_id": ...} while a command is running; read by the
# phone MCP server (X-Request-Id header, see command_router.mcp_config).
active_requests: dict[str, dict] = {}
phone_mcp = build_phone_mcp(phones, elevation, active_requests)

_history: History | None = None


def history() -> History:
    global _history
    if _history is None:
        _history = History(DATA_DIR / "history.db")
    return _history


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log_buffer.bind_loop(asyncio.get_running_loop())
    tail = asyncio.create_task(tail_audit_log(log_buffer, AUDIT_LOG))
    try:
        yield
    finally:
        tail.cancel()


app = FastAPI(title="cluster-command-gateway", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

class CommandRequest(BaseModel):
    text: str
    # Optional client-side identifier for audit logs (e.g. "watch-01")
    device_id: str = "unknown"


async def run_pipeline(text: str, device_id: str, channel: str) -> dict:
    """Claude + tools for one command; the reply in the app's
    CommandResponse shape, recorded in the history either way."""
    request_id = secrets.token_hex(8)
    started = time.time()
    level = elevation.level()
    active_requests[request_id] = {"device_id": device_id}
    error = None
    try:
        outcome = await handle_text_command(text, request_id)
        short, detail = outcome.short, outcome.detail
        steps, needs_confirmation = outcome.steps, outcome.needs_confirmation
    except Exception as exc:  # noqa: BLE001 - we want to report any failure to the client
        logger.exception("Command pipeline failed")
        error = str(exc)
        short, detail, steps, needs_confirmation = f"ERROR: {error[:80]}", error, [], False
    finally:
        active_requests.pop(request_id, None)

    took_ms = int((time.time() - started) * 1000)
    ok = error is None and not short.startswith(("REJECTED", "ERROR"))
    try:
        history().record(id=request_id, started_at=started, channel=channel, device_id=device_id,
                         transcript=text, result_short=short, detail=detail, ok=ok, steps=steps,
                         elevation=level, took_ms=took_ms, error=error)
    except Exception:  # noqa: BLE001 - losing a history row must not lose the answer
        logger.exception("Could not record history")

    logger.info("Done device=%s channel=%s ok=%s took=%dms result=%r",
                device_id, channel, ok, took_ms, short)
    return {
        "id": request_id,
        "transcript": text,
        "result": short,
        "detail": detail,
        "steps": steps,
        "needs_confirmation": needs_confirmation,
        "error": error,
        "took_ms": took_ms,
    }


@app.post("/command")
async def receive_command(payload: CommandRequest):
    """Typed command. The client certificate has already been verified by
    the TLS layer before this code even runs."""
    logger.info("Command from device=%s text=%r", payload.device_id, payload.text)

    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Empty command text")

    return await run_pipeline(payload.text.strip(), payload.device_id, "text")


# 10 s of 16 kHz / 16-bit mono PCM = 320 000 bytes. The watch records at most
# 8 s; refuse anything much bigger before reading it into memory.
MAX_VOICE_BYTES = 10 * 16_000 * 2


class AudioRejected(Exception):
    """The STT service refused the audio (too short, silent, ...)."""


async def transcribe_audio(pcm: bytes, language: str | None = None) -> str:
    """Send raw PCM to the STT service and return the recognised text.
    language: a Whisper language code, "auto" to detect, None for the
    service's default."""
    params = {"language": language} if language else None
    async with httpx.AsyncClient(timeout=STT_TIMEOUT) as client:
        response = await client.post(
            f"{STT_URL}/transcribe",
            content=pcm,
            params=params,
            headers={"content-type": "application/octet-stream"},
        )
    if response.status_code in (400, 413):
        raise AudioRejected(response.json().get("detail", response.text))
    response.raise_for_status()
    return response.json()["text"]


@app.post("/command/audio")
@app.post("/voice")
async def receive_voice(request: Request, device_id: str = "unknown", lang: str | None = None):
    """Voice entry point. The body is the watch's audio exactly as the BLE
    protocol delivers it: raw PCM, 16 kHz, signed 16-bit little-endian, mono,
    no WAV header (send it as application/octet-stream). The server turns it
    into text with Whisper and feeds that text into the same pipeline as
    /command. No `lang` = let Whisper detect the language (the app leaves
    it out when "auto" is chosen)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")

    pcm = await request.body()
    if len(pcm) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")

    try:
        text = await transcribe_audio(pcm, lang or "auto")
    except AudioRejected as exc:
        logger.info("Rejected audio from device=%s: %s", device_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Speech recognition failed")
        raise HTTPException(status_code=500, detail=f"STT failed: {exc}") from exc

    logger.info("Voice from device=%s text=%r", device_id, text)
    if not text:
        return {"transcript": "", "result": "ERROR: speech not recognized", "detail": "",
                "steps": [], "needs_confirmation": False, "error": "speech not recognized",
                "took_ms": 0}

    return await run_pipeline(text, device_id, "voice")


# ---------------------------------------------------------------------------
# Phone control channel
# ---------------------------------------------------------------------------

@app.websocket("/phone/ws")
async def phone_socket(ws: WebSocket):
    await phones.serve(ws)


# ---------------------------------------------------------------------------
# Elevation - granted only from the phone (this mTLS API)
# ---------------------------------------------------------------------------

class ElevationId(BaseModel):
    id: str
    pin: str | None = None


class ElevationLift(BaseModel):
    level: str = "elevated"
    ttl_s: int = 0
    reason: str = ""
    device_id: str = "unknown"


@app.get("/elevation")
async def elevation_status():
    return elevation.status()


@app.post("/elevation/confirm")
async def elevation_confirm(body: ElevationId):
    try:
        challenge = elevation.confirm(body.id, body.pin, confirmed_by="phone")
    except ElevationError as exc:
        logger.warning("Elevation confirm refused: %s", exc)
        return {"ok": False, "error": str(exc)}
    logger.warning("Restrictions lifted: level=%s for %ss (code %s, asked by %s)",
                   challenge.level, challenge.ttl_s, challenge.code, challenge.device_id)
    return {"ok": True, "message": f"{challenge.level} на {challenge.ttl_s // 60} мин"}


@app.post("/elevation/deny")
async def elevation_deny(body: ElevationId):
    elevation.deny(body.id)
    logger.info("Elevation request %s denied", body.id)
    return {"ok": True}


@app.post("/elevation/request")
async def elevation_lift(body: ElevationLift):
    try:
        level, ttl_s = elevation.lift(body.level, body.ttl_s, body.reason, body.device_id)
    except ElevationError as exc:
        return {"ok": False, "error": str(exc)}
    logger.warning("Restrictions lifted from the app: level=%s for %ss device=%s reason=%r",
                   level, ttl_s, body.device_id, body.reason)
    return {"ok": True, "level": level, "ttl_s": ttl_s}


@app.post("/elevation/revoke")
async def elevation_revoke():
    elevation.revoke()
    logger.warning("Restrictions restored (revoked from the app)")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

async def executor_session_call(fn):
    async with streamablehttp_client(command_router.EXECUTOR_URL, timeout=10,
                                     sse_read_timeout=30) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _tool_payload(result):
    if result.structuredContent is not None:
        data = result.structuredContent
        return data.get("result", data) if isinstance(data, dict) else data
    text = "".join(getattr(c, "text", "") for c in result.content)
    return json.loads(text)


@app.get("/monitor")
async def monitor(remote: bool = True):
    try:
        result = await asyncio.wait_for(executor_session_call(
            lambda s: s.call_tool("get_metrics", {"include_remote": remote})), timeout=11)
        if result.isError:
            raise RuntimeError(_tool_payload(result) if result.content else "get_metrics failed")
        nodes = _tool_payload(result)
    except Exception as exc:  # noqa: BLE001 - show the failure on the screen, not a 500
        logger.warning("Metrics unavailable: %s", exc)
        nodes = [{"node": "local", "ok": False, "error": f"executor: {exc}"}]
    return {
        "nodes": nodes,
        "gateway": {
            "uptime_s": int(time.time() - STARTED_AT),
            "phones": phones.links(),
            "model": command_router.CLAUDE_MODEL or "default",
            "mcp_servers": list(command_router.MCP_SERVERS),
        },
        "collected_at": time.time(),
    }


@app.get("/agent/tools")
async def agent_tools():
    tools = [{"server": "phone", "name": t.name, "description": (t.description or "").strip()}
             for t in await phone_mcp.list_tools()]
    try:
        listed = await asyncio.wait_for(executor_session_call(lambda s: s.list_tools()), timeout=10)
        tools = [{"server": "cluster", "name": t.name, "description": (t.description or "").strip()}
                 for t in listed.tools] + tools
    except Exception as exc:  # noqa: BLE001
        logger.warning("Executor tools unavailable: %s", exc)
    return {"tools": tools}


# ---------------------------------------------------------------------------
# History and logs
# ---------------------------------------------------------------------------

@app.get("/history/recent")
async def history_recent(limit: int = 50, q: str = "", failures: bool = False):
    return {"requests": history().recent(limit, q, failures)}


@app.get("/history/stats")
async def history_stats(hours: int = 24):
    return history().stats(hours)


@app.get("/history/{request_id}")
async def history_one(request_id: str):
    entry = history().get(request_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such request")
    return entry


@app.get("/logs/recent")
async def logs_recent(limit: int = 300, level: str = "DEBUG", q: str = ""):
    return {"entries": log_buffer.recent(limit, level, q)}


@app.get("/logs/stream")
async def logs_stream(level: str = "DEBUG", q: str = "", backlog: int = 150):
    sub = log_buffer.subscribe(level, q)
    backlog_entries = log_buffer.recent(backlog, level, q) if backlog > 0 else []

    async def events():
        try:
            last_seq = 0
            for entry in backlog_entries:
                last_seq = entry["seq"]
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
            while True:
                try:
                    entry = await asyncio.wait_for(sub.queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if entry["seq"] <= last_seq:
                    continue  # already sent in the backlog
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        finally:
            log_buffer.unsubscribe(sub)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/health")
async def health():
    """Unauthenticated-content but still behind mTLS - lets the phone app
    confirm the tunnel + cert are working before sending a real command."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Internal server (plain HTTP, not published)
# ---------------------------------------------------------------------------

async def _internal_elevation(_request):
    return JSONResponse({"level": elevation.level()})


def build_internal_app():
    """The phone MCP app plus /internal/elevation. /mcp answers loopback
    only: the executor (and anything else on the docker networks) can read
    the elevation level but never drive the phone."""
    inner = phone_mcp.streamable_http_app()
    inner.router.routes.append(Route("/internal/elevation", _internal_elevation))

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith("/internal/"):
            client = scope.get("client") or ("", 0)
            if client[0] not in ("127.0.0.1", "::1"):
                await JSONResponse({"detail": "loopback only"}, status_code=403)(scope, receive, send)
                return
        await inner(scope, receive, send)

    return guarded


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


async def serve() -> None:
    public = uvicorn.Server(uvicorn.Config(
        app,
        # Bind only to the VPN interface's address in production, not 0.0.0.0 -
        # e.g. your WireGuard/OpenVPN tunnel IP, so the gateway is unreachable
        # from anywhere outside the VPN even before TLS is considered.
        # In docker this is 0.0.0.0 and the VPN-only binding is done by the
        # port mapping in docker-compose.yml instead.
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_PORT", 8443)),
        ssl_certfile=str(CERT_DIR / "server.pem"),
        ssl_keyfile=str(CERT_DIR / "server.key"),
        ssl_ca_certs=str(CERT_DIR / "ca.pem"),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    ))
    internal = uvicorn.Server(uvicorn.Config(
        build_internal_app(),
        host=os.environ.get("INTERNAL_HOST", "127.0.0.1"),
        port=INTERNAL_PORT,
        lifespan="on",
    ))
    # Each server installs its own signal handlers, so a stop signal may
    # reach only one of them: when either ends, end the other too.
    tasks = [asyncio.create_task(public.serve()), asyncio.create_task(internal.serve())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    public.should_exit = internal.should_exit = True
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(serve())
