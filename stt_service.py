"""
stt_service.py

Whisper as a standalone HTTP service, so it can live in its own container
next to the orchestrator (gateway.py). Only reachable on the internal
docker network - the gateway is the single network-facing entry point.

    POST /transcribe   body: raw PCM, 16 kHz, s16le, mono (application/octet-stream)
                       -> {"text": "..."}; 400 for unusable audio, 413 if too big
    GET  /health       -> {"status": "ok"} once the model is loaded

Run locally: uvicorn stt_service:app --host 127.0.0.1 --port 9000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, HTTPException, Request

from stt import AudioError, BYTES_PER_SAMPLE, SAMPLE_RATE, get_transcriber

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stt_service")

# 10 s of 16 kHz / 16-bit mono PCM = 320 000 bytes.
MAX_VOICE_BYTES = 10 * SAMPLE_RATE * BYTES_PER_SAMPLE


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Load Whisper before accepting connections: /health stays unreachable
    # until the model is in memory, so docker only marks us healthy then.
    await anyio.to_thread.run_sync(get_transcriber().warm_up)
    yield


app = FastAPI(title="stt", lifespan=lifespan)


@app.post("/transcribe")
async def transcribe(request: Request):
    pcm = await request.body()
    if len(pcm) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")

    try:
        # Whisper is CPU-bound and blocking: keep it off the event loop.
        text = await anyio.to_thread.run_sync(get_transcriber().transcribe, pcm)
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"text": text}


@app.get("/health")
async def health():
    return {"status": "ok"}
