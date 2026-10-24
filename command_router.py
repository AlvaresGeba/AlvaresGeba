"""
command_router.py

The executor agent's brain: runs Claude Code on one task the orchestrator
handed over (executor_agent.py) and turns its output into the answer.

Flow:
    text  ->  Claude Code (`claude -p`, headless, in the executor container)
          ->  MCP tools:
                cluster - mcp_command_server.py (stdio, started by Claude in
                          this container): shell commands checked against
                          commands_whitelist.md, metrics, elevation requests
                phone   - the phone service (phone_service.py, its own
                          container, over HTTP): actions on the phone
          ->  one short line for the watch + the full answer and every tool
              call for the phone app

Claude gets no shell and none of Claude Code's built-in tools (Bash, file
editing, ...) - the only tools it sees are those two MCP servers, and
mcp_command_server.py re-validates every command against
commands_whitelist.md regardless of what Claude asked for.

Requires the Claude Code CLI (`claude`) on PATH, logged in the same way as
in the terminal (`claude`, then /login) - no API key needed.
"""

from __future__ import annotations

import json
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")  # empty = CLI default
CLAUDE_TIMEOUT = float(os.environ.get("CLAUDE_TIMEOUT", 300))

# The cluster tools: started by Claude as a child process, per task.
COMMAND_SERVER = str(Path(__file__).with_name("mcp_command_server.py"))
# The phone tools (phone_service.py, its own container).
PHONE_MCP_URL = os.environ.get("PHONE_MCP_URL", "http://127.0.0.1:8002/mcp")

# Tools show up to Claude as mcp__<server>__<tool>.
MCP_SERVERS = ("cluster", "phone")


def mcp_config(device_id: str = "") -> dict:
    """The cluster server is told which device the task came from, so
    request_elevation knows who asked."""
    return {
        "mcpServers": {
            "cluster": {"type": "stdio", "command": sys.executable, "args": [COMMAND_SERVER],
                        "env": {"REQUEST_DEVICE_ID": device_id}},
            "phone": {"type": "http", "url": PHONE_MCP_URL},
        }
    }


SYSTEM_PROMPT = """\
You control a Debian Linux server cluster and the user's Android phone
through a restricted set of tools. You do not have a real shell.

"cluster" MCP server (the servers):
- list_allowed_commands() -> shows what commands/targets are permitted
- run_command(command, target="local", confirm=false) -> runs one shell command
- run_script(script_name, args=[], target="local") -> runs a pre-approved script
- get_system_status(target="local") -> read-only health snapshot
- get_metrics(include_remote=true) -> structured CPU/memory/disk per node
- elevation_status, request_elevation

"phone" MCP server (the user's phone):
- phone_status, phone_wake, phone_unlock, phone_keep_awake, read_screen,
  tap, type_text, scroll, press_key, list_apps, open_app, open_url,
  set_alarm, set_timer, flashlight, volume, do_not_disturb, media,
  notifications, send_sms, call

target "local" is the server itself (the host, not a container). Never put
sudo in commands: the server uses root itself where it is allowed.
Always call list_allowed_commands first if you are unsure what is permitted.
To operate an app on the phone: open_app, then read_screen, then tap /
type_text using node ids from the latest read_screen. The UI tools unlock
a locked phone themselves; if one fails because of the lock, call
phone_status - the error says why, and then the user must unlock by hand.

If the user's request is destructive (restart, stop, reboot, sending an SMS,
calling) set confirm to true ONLY if the user's phrasing clearly confirmed
intent (e.g. "yes, restart it", "confirmed"). Otherwise leave confirm false
and let the server ask for confirmation - do not guess consent.

Call request_elevation only when the user explicitly asks to lift the
restrictions. It does not lift anything by itself: the user has to approve
it in the phone app - say so and quote the code.

Reply format. The FIRST line is shown on a small watch screen: one short
line (max ~60 characters) starting with exactly one of:
  OK: <result summary>        - it worked
  REJECTED: <reason>          - the server or phone refused
  ERROR: <what went wrong>    - anything else
After that you may add a blank line and a fuller answer (plain text, no
markdown) - the phone app shows it in full. Always reply in English, even
when the user speaks another language (non-ASCII text breaks the watch
reply labels).
"""


@dataclass
class CommandOutcome:
    short: str
    detail: str
    steps: list[dict] = field(default_factory=list)
    needs_confirmation: bool = False


def build_claude_command(device_id: str = "") -> list[str]:
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json", "--verbose",
        "--system-prompt", SYSTEM_PROMPT,
        "--mcp-config", json.dumps(mcp_config(device_id)),
        "--strict-mcp-config",
        "--tools", "",                    # no built-in tools: no Bash, no file access
        # MCP tools run without a permission prompt
        "--allowedTools", ",".join(f"mcp__{s}" for s in MCP_SERVERS),
        "--no-session-persistence",
        # Skip ~/.claude/settings.json: your personal hooks and plugins are
        # not part of this pipeline.
        "--setting-sources", "project",
    ]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    return cmd


# ---------------------------------------------------------------------------
# stream-json -> steps
# ---------------------------------------------------------------------------

def _tool_name(name: str) -> str:
    """mcp__cluster__run_command -> cluster.run_command"""
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return f"{server}.{tool}" if tool else server
    return name


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return json.dumps(content, ensure_ascii=False)


def _judge(text: str, is_error: bool) -> tuple[bool, bool]:
    """(ok, needs_confirmation) for one tool result."""
    if is_error:
        return False, False
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return True, False
    if not isinstance(data, dict):
        return True, False
    needs = bool(data.get("needs_confirmation")) or \
        "requires confirmation" in str(data.get("reason", ""))
    ok = data.get("executed", True) is not False and data.get("ok", True) is not False \
        and "error" not in data
    return ok, needs


def parse_stream(lines: list[str]) -> tuple[dict | None, list[dict], bool]:
    """Claude Code's stream-json output -> (final result event, steps,
    any step needed confirmation)."""
    calls: dict[str, dict] = {}
    steps: list[dict] = []
    needs_confirmation = False
    final = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "result" or (kind is None and "result" in event):
            final = event
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "tool_use":
                step = {"tool": _tool_name(block.get("name", "?")),
                        "args": block.get("input") or {}, "ok": True, "result": ""}
                calls[block.get("id", "")] = step
                steps.append(step)
            elif kind == "user" and block.get("type") == "tool_result":
                step = calls.get(block.get("tool_use_id", ""))
                if step is None:
                    continue
                text = _result_text(block.get("content"))
                step["ok"], needs = _judge(text, bool(block.get("is_error")))
                step["result"] = text[:2000]
                needs_confirmation = needs_confirmation or needs
    return final, steps, needs_confirmation


def split_reply(text: str) -> tuple[str, str]:
    """First non-empty line for the watch, the whole text for the phone."""
    text = text.strip()
    for line in text.splitlines():
        if line.strip():
            return line.strip(), text
    return "", text


async def handle_text_command(user_text: str, device_id: str = "") -> CommandOutcome:
    """Full pipeline entry point: raw text in, result out.
    This is what executor_agent.py calls for each task."""
    proc = await asyncio.create_subprocess_exec(
        *build_claude_command(device_id),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,  # read_screen results make long lines
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

    reply, steps, needs_confirmation = parse_stream(stdout.decode(errors="replace").splitlines())
    if reply is None:
        raise RuntimeError(
            f"claude exited with {proc.returncode}: {stderr.decode(errors='replace')[-500:]}"
        )
    if proc.returncode != 0 or reply.get("is_error"):
        raise RuntimeError(f"Claude failed: {reply.get('result') or reply.get('subtype')}")

    short, detail = split_reply(reply.get("result") or "")
    return CommandOutcome(short or "OK", detail or short, steps, needs_confirmation)


if __name__ == "__main__":
    # Quick manual test: python3 command_router.py "check disk space on local"
    text = " ".join(sys.argv[1:]) or "show system status"
    print(asyncio.run(handle_text_command(text)))
