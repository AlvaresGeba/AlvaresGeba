"""
mcp_command_server.py

An MCP (Model Context Protocol) server that exposes a *guarded* shell
execution surface to an LLM client. It does NOT give the model a raw shell.
Every command is checked against commands_whitelist.md before it is allowed
to run, and every attempt (allowed or not) is logged.

How strict the check is depends on the elevation level, which only the
phone can raise (see elevation.py in the orchestrator); it is read from the
orchestrator before every command:
    normal        allow runs, confirm needs confirm=true, the rest is refused
    elevated      confirm rules run without confirm=true
    unrestricted  commands that match no rule run too
deny rules - and anything touching the audit log - are refused on every level.

Run this on each cluster node you want the AI to be able to act on, or run
it once and have it SSH out to the other node for cluster-wide commands
(see run_command's `target` argument).

In docker it is its own service (executor) and serves MCP over streamable
HTTP to the orchestrator: MCP_TRANSPORT=streamable-http. Without that it
speaks stdio, for local testing.

Requires: pip install "mcp[cli]" pyyaml
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import logging
import urllib.request
import re as regex_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import node_metrics

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
WHITELIST_MD = BASE_DIR / "commands_whitelist.md"
SCRIPTS_DIR = BASE_DIR / "scripts"
LOG_FILE = BASE_DIR / "command_audit.log"

# Read-only view of the orchestrator's elevation state. Unset or
# unreachable = "normal" (fail closed).
ELEVATION_URL = os.environ.get("ELEVATION_URL", "")

# Units shown on the app's Server screen (systemd nodes only).
MONITOR_UNITS = [u for u in os.environ.get("MONITOR_UNITS", "").split(",") if u]
# In docker, the host's /etc/hostname is mounted here so the node is
# reported by its real name, not the container id.
HOST_HOSTNAME_FILE = Path(os.environ.get("HOST_HOSTNAME_FILE", "/host/hostname"))

# Known cluster nodes this server is allowed to reach over SSH.
# Add your real hostnames/IPs here. Keys used as the `target` argument.
CLUSTER_NODES: dict[str, str] = {
    "local": "",  # empty = run on this machine directly
    # "node2": "admin@10.0.0.2",
}

# Only our own audit records go to the file, not the MCP library's
# per-request chatter.
_audit_handler = logging.FileHandler(LOG_FILE)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger = logging.getLogger("command_audit")
logger.addHandler(_audit_handler)
logger.setLevel(logging.INFO)

# HTTP mode only (see __main__). MCP_ALLOWED_HOSTS is the Host header the
# orchestrator uses (e.g. "executor:*"); anything else is refused, which
# keeps DNS-rebinding protection on even though we listen on 0.0.0.0.
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", 8000))
MCP_ALLOWED_HOSTS = [h for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h]

mcp = FastMCP(
    "cluster-command-server",
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=MCP_ALLOWED_HOSTS + ["127.0.0.1:*", "localhost:*"],
    ),
)


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


def _elevation_level() -> str:
    if not ELEVATION_URL:
        return "normal"
    try:
        with urllib.request.urlopen(ELEVATION_URL, timeout=2) as resp:
            level = json.load(resp).get("level", "normal")
    except Exception:  # noqa: BLE001 - unreachable orchestrator = no elevation
        logger.warning("Elevation lookup failed - treating as normal")
        return "normal"
    return level if level in ("normal", "elevated", "unrestricted") else "normal"


def _classify(command: str, level: str = "normal") -> tuple[Literal["allow", "confirm", "deny", "unknown"], str]:
    """Decide what to do with a command string per the whitelist rules.

    Deny is checked first and always wins, regardless of other matches and
    of the elevation level.
    """
    if "command_audit" in command:
        return "deny", "The audit log is off limits on every level"

    rules = _load_whitelist()

    for rule in rules["deny"]:
        if regex_mod.match(rule.pattern, command):
            return "deny", rule.description

    for rule in rules["confirm"]:
        if regex_mod.match(rule.pattern, command):
            if level != "normal":
                return "allow", f"{rule.description} (restrictions lifted: {level})"
            return "confirm", rule.description

    for rule in rules["allow"]:
        if regex_mod.match(rule.pattern, command):
            return "allow", rule.description

    if level == "unrestricted":
        return "allow", "No matching rule - allowed because restrictions are lifted (unrestricted)"
    return "unknown", "No matching rule - rejected by default (fail closed)"

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
    level = _elevation_level()
    decision, reason = _classify(command, level)
    logger.info("REQUEST target=%s command=%r decision=%s reason=%s confirm=%s level=%s",
                target, command, decision, reason, confirm, level)

    if decision == "deny":
        return {"executed": False, "reason": f"Blocked by deny rule: {reason}"}

    if decision == "unknown":
        return {
            "executed": False,
            "reason": "Command matches no allowlist rule. Refusing by default. "
                      "Call list_allowed_commands() to see what is permitted, "
                      "add a new rule to commands_whitelist.md, or ask the user "
                      "to lift the restrictions from the phone.",
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


def _local_metrics() -> dict:
    in_docker = Path("/.dockerenv").exists()
    hostname = None
    if HOST_HOSTNAME_FILE.is_file():
        hostname = HOST_HOSTNAME_FILE.read_text().strip() or None
    return node_metrics.collect(
        node="local",
        hostname=hostname,
        # In a container /proc/mounts is the container's. statvfs on a
        # bind-mounted file reports the filesystem it lives on: the
        # hostname file sits on the host's root, "/" on docker's storage,
        # scripts/ on the project's disk. Network counters would be the
        # container's own - not worth showing.
        disk_roots={"/": str(HOST_HOSTNAME_FILE), "docker": "/", "project": str(SCRIPTS_DIR)}
        if in_docker else None,
        net=not in_docker,
        units=MONITOR_UNITS,
    )


def _remote_metrics(name: str, host: str) -> dict:
    script = Path(node_metrics.__file__).read_text()
    env = f"NODE_NAME={name} MONITOR_UNITS={','.join(MONITOR_UNITS)}"
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, f"{env} python3 -"],
            input=script, capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[-300:] or f"exit {proc.returncode}")
        return json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001 - a dead node is a result, not an error
        return {"node": name, "ok": False, "error": str(exc), "hostname": host}


@mcp.tool()
def get_metrics(include_remote: bool = True) -> list[dict]:
    """Structured health of every cluster node: CPU %, load, memory, disks,
    uptime, temperature, network, docker containers, watched systemd units.
    Read-only, no whitelist check needed."""
    try:
        nodes = [_local_metrics()]
    except Exception as exc:  # noqa: BLE001
        nodes = [{"node": "local", "ok": False, "error": str(exc)}]
    if include_remote:
        nodes += [_remote_metrics(name, host) for name, host in CLUSTER_NODES.items()
                  if name != "local" and host]
    return nodes


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
