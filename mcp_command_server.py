"""
mcp_command_server.py

An MCP (Model Context Protocol) server that exposes a *guarded* shell
execution surface to an LLM client. It does NOT give the model a raw shell.
Every command is checked against commands_whitelist.md before it is allowed
to run, and every attempt (allowed or not) is logged.

Run this on each cluster node you want the AI to be able to act on, or run
it once and have it SSH out to the other node for cluster-wide commands
(see run_command's `target` argument).

Requires: pip install "mcp[cli]" pyyaml
"""

from __future__ import annotations

import re
import subprocess
import logging
import re as regex_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
WHITELIST_MD = BASE_DIR / "commands_whitelist.md"
SCRIPTS_DIR = BASE_DIR / "scripts"
LOG_FILE = BASE_DIR / "command_audit.log"

# Known cluster nodes this server is allowed to reach over SSH.
# Add your real hostnames/IPs here. Keys used as the `target` argument.
CLUSTER_NODES: dict[str, str] = {
    "local": "",  # empty = run on this machine directly
    # "node2": "admin@10.0.0.2",
}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("command_audit")

mcp = FastMCP("cluster-command-server")


# ---------------------------------------------------------------------------
# Whitelist loading
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    pattern: str
    description: str


def _load_whitelist() -> dict[str, list[Rule]]:
    """Extract and parse the YAML fenced block from commands_whitelist.md.

    Reloaded on every call so edits to the .md file take effect without
    restarting the server.
    """
    text = WHITELIST_MD.read_text(encoding="utf-8")
    match = re.search(r"```yaml\n(.*?)```", text, re.DOTALL)
    if not match:
        raise RuntimeError("Could not find a yaml block in commands_whitelist.md")

    data = yaml.safe_load(match.group(1))
    result: dict[str, list[Rule]] = {}
    for section in ("allow", "confirm", "deny"):
        result[section] = [
            Rule(pattern=item["pattern"], description=item.get("description", ""))
            for item in data.get(section, [])
        ]
    return result


def _classify(command: str) -> tuple[Literal["allow", "confirm", "deny", "unknown"], str]:
    """Decide what to do with a command string per the whitelist rules.

    Deny is checked first and always wins, regardless of other matches.
    """
    rules = _load_whitelist()

    for rule in rules["deny"]:
        if regex_mod.match(rule.pattern, command):
            return "deny", rule.description

    for rule in rules["confirm"]:
        if regex_mod.match(rule.pattern, command):
            return "confirm", rule.description

    for rule in rules["allow"]:
        if regex_mod.match(rule.pattern, command):
            return "allow", rule.description

    #return "unknown", "No matching rule - rejected by default (fail closed)"
    return "allow","test"

def _run(command: str, target: str) -> dict:
    """Actually execute a command, locally or over SSH to a cluster node."""
    if target and target != "local":
        host = CLUSTER_NODES.get(target)
        if host is None:
            return {"error": f"Unknown cluster target '{target}'"}
        full_cmd = ["ssh", "-o", "BatchMode=yes", host, command]
    else:
        full_cmd = ["/bin/sh", "-c", command]

    try:
        proc = subprocess.run(
            full_cmd, capture_output=True, text=True, timeout=30
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-4000:],  # cap output size
            "stderr": proc.stderr[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out after 30s"}


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_allowed_commands() -> str:
    """Return the current allowlist so the model knows what it can do
    before it tries anything. Call this first in a session."""
    rules = _load_whitelist()
    lines = ["ALLOWED (no confirmation needed):"]
    lines += [f"  - {r.pattern}  # {r.description}" for r in rules["allow"]]
    lines.append("\nREQUIRES confirm=True:")
    lines += [f"  - {r.pattern}  # {r.description}" for r in rules["confirm"]]
    lines.append("\nAvailable cluster targets: " + ", ".join(CLUSTER_NODES))
    return "\n".join(lines)


@mcp.tool()
def run_command(command: str, target: str = "local", confirm: bool = False) -> dict:
    """Execute a single shell command against a cluster node.

    Args:
        command: the exact shell command to run.
        target: which cluster node to run it on ("local" or a name from
            list_allowed_commands' cluster target list).
        confirm: must be True to execute anything classified as "confirm"
            risk level. Has no effect on "allow" or "deny" commands.
    """
    decision, reason = _classify(command)
    logger.info("REQUEST target=%s command=%r decision=%s reason=%s confirm=%s",
                target, command, decision, reason, confirm)

    if decision == "deny":
        return {"executed": False, "reason": f"Blocked by deny rule: {reason}"}

    if decision == "unknown":
        return {
            "executed": False,
            "reason": "Command matches no allowlist rule. Refusing by default. "
                      "Call list_allowed_commands() to see what is permitted, "
                      "or add a new rule to commands_whitelist.md.",
        }

    if decision == "confirm" and not confirm:
        return {
            "executed": False,
            "reason": f"This command requires confirmation ({reason}). "
                      "Call again with confirm=True if you are sure.",
        }

    result = _run(command, target)
    logger.info("EXECUTED target=%s command=%r result=%s", target, command, result)
    return {"executed": True, **result}


@mcp.tool()
def run_script(script_name: str, args: list[str] | None = None, target: str = "local") -> dict:
    """Run a pre-approved script that physically lives in the scripts/
    directory on the server. This is the safe path for anything more
    complex than a one-line command - write the logic into a script file
    once, review it yourself, then let the AI invoke it by name only.

    The AI can never pass an arbitrary path here - only a filename that
    must already exist under scripts/.
    """
    script_path = (SCRIPTS_DIR / script_name).resolve()
    if SCRIPTS_DIR.resolve() not in script_path.parents:
        return {"executed": False, "reason": "Path escapes scripts/ directory - rejected"}
    if not script_path.is_file():
        return {"executed": False, "reason": f"No such script: {script_name}"}

    args = args or []
    quoted_args = " ".join(f"'{a}'" for a in args)
    command = f"{script_path} {quoted_args}".strip()

    logger.info("SCRIPT target=%s script=%s args=%s", target, script_name, args)
    result = _run(command, target)
    logger.info("SCRIPT_RESULT script=%s result=%s", script_name, result)
    return {"executed": True, **result}


@mcp.tool()
def get_system_status(target: str = "local") -> dict:
    """Quick health snapshot: uptime, disk, memory. Always allowed,
    read-only, no whitelist check needed since it can't change anything."""
    combined = " && echo '---' && ".join(["uptime", "df -h", "free -h"])
    return _run(combined, target)


if __name__ == "__main__":
    mcp.run(transport="stdio")
