"""
command_router.py

Bridges natural-language text (already transcribed from voice by the
whisper service) to the guarded MCP command server, with Claude in between.

Flow:
    text  ->  Claude Code (`claude -p`, headless)
          ->  MCP tools of mcp_command_server.py  ->  short result line

Claude gets no shell and none of Claude Code's built-in tools (Bash, file
editing, ...) - the only tools it sees are the ones mcp_command_server.py
exposes. That server is the only thing that actually touches the shell, and
it re-validates everything against commands_whitelist.md regardless of
what Claude asked for.

Requires the Claude Code CLI (`claude`) on PATH, logged in the same way as
in the terminal (`claude`, then /login) - no API key needed.
Requires: pip install "mcp[cli]"
"""

from __future__ import annotations

import json
import asyncio
import os
import sys
from pathlib import Path

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")  # empty = CLI default
CLAUDE_TIMEOUT = float(os.environ.get("CLAUDE_TIMEOUT", 300))

SERVER_SCRIPT = Path(__file__).parent / "mcp_command_server.py"

# The MCP server Claude is given - started by Claude Code over stdio.
# Its tools show up to Claude as mcp__cluster__<tool>.
MCP_CONFIG = {
    "mcpServers": {
        "cluster": {"command": sys.executable, "args": [str(SERVER_SCRIPT)]},
    }
}

SYSTEM_PROMPT = """\
You control a Debian Linux server cluster through a restricted set of tools.
You do not have a real shell - you can only call the tools of the "cluster"
MCP server:
- list_allowed_commands() -> shows what commands/targets are permitted
- run_command(command, target="local", confirm=false) -> runs one shell command
- run_script(script_name, args=[], target="local") -> runs a pre-approved script
- get_system_status(target="local") -> read-only health snapshot

Always call list_allowed_commands first if you are unsure what is permitted.

If the user's request is destructive (restart, stop, reboot, etc.) set
confirm to true ONLY if the user's phrasing clearly confirmed intent
(e.g. "yes, restart it", "confirmed"). Otherwise leave confirm false and
let the server ask for confirmation - do not guess consent.

Your final answer is shown on a small watch screen. Reply with ONE short
line and nothing else, starting with exactly one of:
  OK: <result summary>        - the command ran
  REJECTED: <reason>          - the server refused to run it
  ERROR: <what went wrong>    - anything else
"""


def build_claude_command() -> list[str]:
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--system-prompt", SYSTEM_PROMPT,
        "--mcp-config", json.dumps(MCP_CONFIG),
        "--strict-mcp-config",
        "--tools", "",                    # no built-in tools: no Bash, no file access
        "--allowedTools", "mcp__cluster",  # MCP tools run without a permission prompt
        "--no-session-persistence",
        # Skip ~/.claude/settings.json: your personal hooks and plugins are
        # not part of this pipeline.
        "--setting-sources", "project",
    ]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    return cmd


async def handle_text_command(user_text: str) -> str:
    """Full pipeline entry point: raw text in, human-readable result out.
    This is what gateway.py calls for each incoming request."""
    proc = await asyncio.create_subprocess_exec(
        *build_claude_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # The command goes in on stdin, never as an argv flag.
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(user_text.encode()), timeout=CLAUDE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Claude did not answer within {CLAUDE_TIMEOUT:.0f}s")

    try:
        reply = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"claude exited with {proc.returncode}: {stderr.decode(errors='replace')[-500:]}"
        )
    if proc.returncode != 0 or reply.get("is_error"):
        raise RuntimeError(f"Claude failed: {reply.get('result') or reply.get('subtype')}")

    return reply["result"].strip()


if __name__ == "__main__":
    # Quick manual test: python3 command_router.py "check disk space on local"
    text = " ".join(sys.argv[1:]) or "show system status"
    print(asyncio.run(handle_text_command(text)))
