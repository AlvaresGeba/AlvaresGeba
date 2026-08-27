"""
command_router.py

Bridges natural-language text (already transcribed from voice by a separate
speech-to-text service) to the guarded MCP command server.

Flow:
    text  ->  local LLM (Ollama)  ->  structured tool call (JSON)
          ->  MCP client  ->  mcp_command_server.py  ->  result text

The LLM never gets shell access directly - it only ever produces a JSON
object naming an MCP tool and its arguments. mcp_command_server.py is the
only thing that actually touches the shell, and it re-validates everything
against commands_whitelist.md regardless of what the LLM asked for.

Requires: pip install "mcp[cli]" httpx
Requires a running Ollama instance (https://ollama.com) with a small
instruction-tuned model, e.g.:  ollama pull qwen2.5:7b-instruct
"""

from __future__ import annotations

import json
import asyncio
import sys
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:7b-instruct"

SERVER_SCRIPT = Path(__file__).parent / "mcp_command_server.py"

SYSTEM_PROMPT = """\
You control a Debian Linux server cluster through a restricted set of tools.
You do not have a real shell - you can only call the tools listed below.
Always call list_allowed_commands first if you are unsure what is permitted.

Respond with ONLY a JSON object, no other text, in this exact shape:
{"tool": "<tool_name>", "args": {<tool arguments>}}

Available tools:
- list_allowed_commands() -> shows what commands/targets are permitted
- run_command(command, target="local", confirm=false) -> runs one shell command
- run_script(script_name, args=[], target="local") -> runs a pre-approved script
- get_system_status(target="local") -> read-only health snapshot

If the user's request is destructive (restart, stop, reboot, etc.) set
confirm to true ONLY if the user's phrasing clearly confirmed intent
(e.g. "yes, restart it", "confirmed"). Otherwise leave confirm false and
let the server ask for confirmation - do not guess consent.
"""


async def ask_llm_for_tool_call(user_text: str) -> dict:
    """Send the transcribed command to the local LLM and get back a
    structured tool call. Falls back to a safe no-op on parse failure."""
    async with httpx.AsyncClient(timeout=3000000) as client:
        response = await client.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "stream": False,
                "format": "json",
            },
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]

    try:
        parsed = json.loads(content)
        assert "tool" in parsed
        return parsed
    except (json.JSONDecodeError, AssertionError, KeyError):
        return {"tool": "list_allowed_commands", "args": {}}


def _format_for_watch(tool_name: str, raw_result: dict) -> str:
    """Turn the MCP tool's raw dict into a short "OK: ..." / "REJECTED: ..."
    line the watch firmware can color-code and fit on a small screen."""
    if isinstance(raw_result, dict) and raw_result.get("executed") is False:
        return f"REJECTED: {raw_result.get('reason', 'unknown reason')}"

    if isinstance(raw_result, dict) and "error" in raw_result:
        return f"ERROR: {raw_result['error']}"

    if isinstance(raw_result, dict) and raw_result.get("executed"):
        exit_code = raw_result.get("exit_code", 0)
        if exit_code == 0:
            stdout = (raw_result.get("stdout") or "").strip()
            summary = stdout.splitlines()[0] if stdout else "done"
            return f"OK: {summary}"
        return f"OK (exit {exit_code}): {raw_result.get('stderr', '')[:80]}"

    # list_allowed_commands / get_system_status etc. return plain text
    return f"OK: {str(raw_result)}"


async def execute_tool_call(tool_call: dict) -> str:
    """Open an MCP session to the command server and invoke the chosen tool,
    then format the result for display on the watch."""
    server_params = StdioServerParameters(
        command=sys.executable, args=[str(SERVER_SCRIPT)]
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                tool_call["tool"], arguments=tool_call.get("args", {})
            )
            return _format_for_watch(tool_call["tool"], result.content)


async def handle_text_command(user_text: str) -> str:
    """Full pipeline entry point: raw text in, human-readable result out.
    This is what gateway.py calls for each incoming request."""
    tool_call = await ask_llm_for_tool_call(user_text)
    return await execute_tool_call(tool_call)


if __name__ == "__main__":
    # Quick manual test: python3 command_router.py "check disk space on local"
    import sys

    text = " ".join(sys.argv[1:]) or "show system status"
    print(asyncio.run(handle_text_command(text)))
