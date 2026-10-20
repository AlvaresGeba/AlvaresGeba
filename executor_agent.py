"""
executor_agent.py

The executor agent: the orchestrator hands it a task, Claude Code in this
container works it out with the tools it has here, the answer goes back.

    POST /task      {"text", "request_id", "device_id"}
                    -> {"short", "detail", "steps", "needs_confirmation"}
    GET  /tools     every tool Claude has here: cluster + phone
    GET  /metrics   structured health of the cluster nodes (Server screen)
    GET  /info      model and MCP servers
    GET  /health

The same contract is what any future agent implements, so the orchestrator
can hand tasks to it the same way (see AGENTS in gateway.py).

Only the orchestrator can reach this (internal docker network). The shell
is never touched from here directly: commands go through
mcp_command_server.py, which Claude starts per task.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict

import uvicorn
from fastapi import FastAPI, HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

import command_router
import mcp_command_server
from command_router import handle_text_command

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("executor_agent")

app = FastAPI(title="executor-agent")


class Task(BaseModel):
    text: str
    request_id: str = ""
    device_id: str = "unknown"


@app.post("/task")
async def run_task(task: Task):
    logger.info("Task %s from device=%s: %r", task.request_id, task.device_id, task.text)
    try:
        outcome = await handle_text_command(task.text, task.device_id)
    except Exception as exc:  # noqa: BLE001 - the orchestrator reports it to the phone
        logger.exception("Task %s failed", task.request_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("Task %s done: %r", task.request_id, outcome.short)
    return asdict(outcome)


async def phone_tools() -> list:
    async with streamablehttp_client(command_router.PHONE_MCP_URL, timeout=5,
                                     sse_read_timeout=10) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools


def _describe(server: str, tools) -> list[dict]:
    return [{"server": server, "name": t.name, "description": (t.description or "").strip()}
            for t in tools]


@app.get("/tools")
async def tools():
    listed = _describe("cluster", await mcp_command_server.mcp.list_tools())
    try:
        listed += _describe("phone", await asyncio.wait_for(phone_tools(), timeout=8))
    except Exception as exc:  # noqa: BLE001 - phone service down: show what is left
        logger.warning("Phone tools unavailable: %s", exc)
    return {"tools": listed}


@app.get("/metrics")
async def metrics(remote: bool = True):
    return await asyncio.to_thread(mcp_command_server.get_metrics, remote)


@app.get("/info")
async def info():
    return {"model": command_router.CLAUDE_MODEL or "default",
            "mcp_servers": list(command_router.MCP_SERVERS)}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("AGENT_HOST", "127.0.0.1"),
                port=int(os.environ.get("AGENT_PORT", 8000)))
