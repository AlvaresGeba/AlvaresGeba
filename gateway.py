"""
gateway.py

The orchestrator: the only network-facing entry point, and the one that
hands every task to an agent. It runs no model and no tools itself.
Everything else is reachable only on internal docker networks:

    whisper   speech-to-text
    executor  the agent (executor_agent.py): Claude Code + the server tools
    phone     the phone link (phone_service.py)

This process terminates mutual-TLS connections coming from the phone app
over the VPN tunnel and serves everything the app (WatchCommandApp) talks
to:

    POST /command                 typed command -> an agent
    POST /command/audio           watch audio (raw PCM) -> Whisper -> same
    WS   /phone/ws                the phone's control channel, relayed to
                                  the phone service as is
    GET  /elevation, POST /elevation/{request,confirm,deny,revoke}
    GET  /monitor, /agent/tools   cluster health, the agents' tools
    GET  /history/recent, /history/stats
    GET  /logs/recent, /logs/stream (Server-Sent Events)
    GET  /health

Mutual TLS means: the phone must present a client certificate signed by
your own CA, or the TLS handshake fails before any application code runs
at all. A stolen bearer token is not enough to talk to this server - the
attacker would need the phone's private key file too.

A second, plain-HTTP server (INTERNAL_PORT; docker networks + host
loopback) carries the elevation state for the other services:
    GET  /internal/elevation           status (level, time left, pending)
    POST /internal/elevation/request   a request to lift the restrictions;
                                       granted only from the phone, here
It refuses (403) clients from INTERNAL_DENY_SUBNETS (the monitoring
network in docker-compose.yml).

A third, plain-HTTP server (METRICS_PORT) serves only
    GET  /metrics                      Prometheus metrics (scraped by the
                                       prometheus service)

See README.md for the openssl commands to generate:
  ca.pem            - your private certificate authority
  server.pem/.key    - this gateway's identity
  phone-client.pem/.key - the Android app's identity (bundled into the app)
"""

from __future__ import annotations

import asyncio
import ipaddress
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
import websockets
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Histogram, generate_latest
from pydantic import BaseModel

from elevation import Elevation, ElevationError
from history import History
from log_buffer import BufferHandler, LogBuffer, tail_audit_log

BASE_DIR = Path(__file__).parent
CERT_DIR = BASE_DIR / "certs"
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", BASE_DIR / "command_audit.log"))

# Whisper runs as a separate service (stt_service.py, its own container).
STT_URL = os.environ.get("STT_URL", "http://127.0.0.1:9000")
STT_TIMEOUT = float(os.environ.get("STT_TIMEOUT", 60))
INTERNAL_PORT = int(os.environ.get("INTERNAL_PORT", 8001))
METRICS_PORT = int(os.environ.get("METRICS_PORT", 9100))

# The agents tasks are handed to: name -> base URL. Each implements
#   POST /task {"text", "request_id", "device_id"}
#        -> {"short", "detail", "steps", "needs_confirmation"}
#   GET /tools, GET /info
# (see executor_agent.py). For now there is one.
AGENTS = {"executor": os.environ.get("EXECUTOR_URL", "http://127.0.0.1:8000")}
# Claude's own timeout in the agent is 300 s; leave it room to report it.
AGENT_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", 330))

# The phone link (phone_service.py).
PHONE_URL = os.environ.get("PHONE_URL", "http://127.0.0.1:8002")
PHONE_WS_URL = PHONE_URL.replace("http", "ws", 1) + "/phone/ws"

STARTED_AT = time.time()

log_buffer = LogBuffer()
logging.basicConfig(level=logging.INFO)
logging.getLogger().addHandler(BufferHandler(log_buffer))
logger = logging.getLogger("gateway")

elevation = Elevation()

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
# Metrics (served on their own server, METRICS_PORT, GET /metrics)
# ---------------------------------------------------------------------------

# From the request reaching a command handler to its final answer: for voice
# that includes reading the body, Whisper and the agent. Requests refused
# before any processing (empty text, audio too large) are not observed.
# The top bucket is AGENT_TIMEOUT's default.
REQUEST_DURATION = Histogram(
    "geba_request_duration_seconds",
    "Time from a command reaching the orchestrator to its final answer.",
    ["channel", "outcome"],  # channel: text|voice, outcome: ok|error
    buckets=(1, 2, 5, 10, 20, 30, 60, 120, 180, 300, 330),
)


def reply_outcome(reply: dict) -> str:
    """ok|error for the histogram: error when the pipeline failed (STT or
    the agent raised, or the agent answered ERROR:). A REJECTED: answer is
    the whitelist working as intended, so it counts as ok."""
    failed = reply.get("error") or str(reply.get("result", "")).startswith("ERROR")
    return "error" if failed else "ok"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def pick_agent(text: str) -> str:
    """Which agent gets the task. One agent so far - routing goes here when
    there are more."""
    return "executor"


async def dispatch(text: str, request_id: str, device_id: str) -> dict:
    """Hand the task to an agent and wait for its answer:
    {"short", "detail", "steps", "needs_confirmation"}."""
    agent = pick_agent(text)
    async with httpx.AsyncClient(timeout=AGENT_TIMEOUT) as client:
        response = await client.post(f"{AGENTS[agent]}/task", json={
            "text": text, "request_id": request_id, "device_id": device_id})
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise RuntimeError(f"{agent}: {detail or response.status_code}")
    return response.json()


class CommandRequest(BaseModel):
    text: str
    # Optional client-side identifier for audit logs (e.g. "watch-01")
    device_id: str = "unknown"


async def run_pipeline(text: str, device_id: str, channel: str) -> dict:
    """One command through an agent; the reply in the app's
    CommandResponse shape, recorded in the history either way."""
    request_id = secrets.token_hex(8)
    started = time.time()
    level = elevation.level()
    error = None
    try:
        outcome = await dispatch(text, request_id, device_id)
        short, detail = outcome["short"], outcome["detail"]
        steps, needs_confirmation = outcome.get("steps", []), outcome.get("needs_confirmation", False)
    except Exception as exc:  # noqa: BLE001 - we want to report any failure to the client
        logger.exception("Command pipeline failed")
        error = str(exc)
        short, detail, steps, needs_confirmation = f"ERROR: {error[:80]}", error, [], False

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

    started = time.perf_counter()
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Empty command text")

    outcome = "error"
    try:
        reply = await run_pipeline(payload.text.strip(), payload.device_id, "text")
        outcome = reply_outcome(reply)
        return reply
    finally:
        REQUEST_DURATION.labels(channel="text", outcome=outcome).observe(time.perf_counter() - started)


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
    started = time.perf_counter()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")

    pcm = await request.body()
    if len(pcm) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")

    # From here on every exit is observed; anything raised is an error.
    outcome = "error"
    try:
        reply = await _voice_pipeline(pcm, device_id, lang)
        outcome = reply_outcome(reply)
        return reply
    finally:
        REQUEST_DURATION.labels(channel="voice", outcome=outcome).observe(time.perf_counter() - started)


async def _voice_pipeline(pcm: bytes, device_id: str, lang: str | None) -> dict:
    """Whisper, then the same pipeline as /command."""
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
    """Relays the phone's socket to the phone service, message for message.
    TLS and the client certificate end here; the protocol is the phone
    service's business."""
    await ws.accept()
    try:
        async with websockets.connect(PHONE_WS_URL, open_timeout=5) as upstream:
            async def phone_to_service():
                while True:
                    await upstream.send(await ws.receive_text())

            async def service_to_phone():
                async for message in upstream:
                    await ws.send_text(message if isinstance(message, str) else message.decode())

            tasks = [asyncio.create_task(phone_to_service()), asyncio.create_task(service_to_phone())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            close_code = upstream.close_code
    except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
        logger.warning("Phone service unreachable: %s", exc)
        close_code = 1011
    try:
        await ws.close(code=close_code or 1000)
    except (RuntimeError, WebSocketDisconnect):
        pass  # the phone is already gone


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
    return {"ok": True, "message": f"{challenge.level} for {challenge.ttl_s // 60} min"}


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

async def service_get(url: str, timeout: float = 12):
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def _or_error(what: str, coro, fallback):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - show the failure on the screen, not a 500
        logger.warning("%s unavailable: %s", what, exc)
        return fallback(exc)


@app.get("/monitor")
async def monitor(remote: bool = True):
    executor = AGENTS["executor"]
    nodes, phones, info = await asyncio.gather(
        _or_error("Metrics", service_get(f"{executor}/metrics?remote={str(remote).lower()}"),
                  lambda exc: [{"node": "local", "ok": False, "error": f"executor: {exc}"}]),
        _or_error("Phone list", service_get(f"{PHONE_URL}/phones", timeout=5), lambda exc: {}),
        _or_error("Agent info", service_get(f"{executor}/info", timeout=5),
                  lambda exc: {"model": "unknown", "mcp_servers": []}),
    )
    return {
        "nodes": nodes,
        "gateway": {
            "uptime_s": int(time.time() - STARTED_AT),
            "phones": phones,
            "model": info.get("model", "unknown"),
            "mcp_servers": info.get("mcp_servers", []),
            "agents": list(AGENTS),
        },
        "collected_at": time.time(),
    }


@app.get("/agent/tools")
async def agent_tools():
    tools = []
    for name, url in AGENTS.items():
        listed = await _or_error(f"{name} tools", service_get(f"{url}/tools"), lambda exc: {})
        tools += listed.get("tools", [])
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
# Internal server (plain HTTP: docker networks + host loopback)
# ---------------------------------------------------------------------------

class ElevationAsk(BaseModel):
    level: str
    minutes: int
    reason: str = ""
    device_id: str = "unknown"


def parse_subnets(spec: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """"172.31.250.0/28,10.0.0.0/8" -> networks. Empty = none. A malformed
    entry raises: better not to start than to silently deny nothing."""
    return [ipaddress.ip_network(part.strip(), strict=False)
            for part in spec.split(",") if part.strip()]


def client_denied(host: str | None, denied: list) -> bool:
    if not denied or not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # not an IP (e.g. TestClient's "testclient")
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return any(address in net for net in denied)


def build_metrics_app() -> FastAPI:
    """Only GET /metrics, for prometheus. Everything else is a 404."""
    metrics_app = FastAPI(title="orchestrator-metrics", docs_url=None,
                          redoc_url=None, openapi_url=None)

    @metrics_app.get("/metrics")
    async def metrics():
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return metrics_app


def build_internal_app() -> FastAPI:
    """Elevation for the other services: anyone here may read it or ask for
    it; only the phone (the mTLS API) can grant it."""
    internal = FastAPI(title="orchestrator-internal")
    denied = parse_subnets(os.environ.get("INTERNAL_DENY_SUBNETS", ""))

    @internal.middleware("http")
    async def refuse_denied_subnets(request: Request, call_next):
        # Port 8001 listens on every network of the container; the
        # monitoring network (prometheus) must not reach it.
        host = request.client.host if request.client else None
        if client_denied(host, denied):
            logger.warning("Internal server: refused %s (INTERNAL_DENY_SUBNETS)", host)
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        return await call_next(request)

    @internal.get("/internal/elevation")
    async def internal_elevation():
        return elevation.status()

    @internal.post("/internal/elevation/request")
    async def internal_elevation_request(body: ElevationAsk):
        try:
            challenge = elevation.request(body.level, body.minutes * 60, body.reason, body.device_id)
        except ElevationError as exc:
            return {"ok": False, "error": str(exc)}
        logger.warning("Elevation requested: level=%s ttl=%ss device=%s code=%s reason=%r",
                       challenge.level, challenge.ttl_s, body.device_id, challenge.code, body.reason)
        return {"ok": True, "pending": True, "code": challenge.code,
                "message": "Waiting for approval in the phone app (expires in 2 min)."}

    return internal


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
    metrics = uvicorn.Server(uvicorn.Config(
        build_metrics_app(),
        host=os.environ.get("METRICS_HOST", "127.0.0.1"),
        port=METRICS_PORT,
        lifespan="off",
    ))
    servers = [public, internal, metrics]
    # Each server installs its own signal handlers, so a stop signal may
    # reach only one of them: when any ends, end the others too.
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for server in servers:
        server.should_exit = True
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(serve())
