import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import command_router
import mcp_command_server
from command_router import MCP_CONFIG, handle_text_command, mcp_config, parse_stream, split_reply

SERVER_SCRIPT = Path(mcp_command_server.__file__)


class McpConfigTests(unittest.TestCase):
    def test_config_points_claude_at_executor_and_phone_over_http(self) -> None:
        self.assertEqual(
            MCP_CONFIG["mcpServers"]["cluster"],
            {"type": "http", "url": command_router.EXECUTOR_URL},
        )
        phone = mcp_config("abc")["mcpServers"]["phone"]
        self.assertEqual(phone["url"], command_router.PHONE_MCP_URL)
        self.assertEqual(phone["headers"], {"X-Request-Id": "abc"})

    def test_server_exposes_cluster_tools(self) -> None:
        async def list_tools() -> set[str]:
            params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return {tool.name for tool in (await session.list_tools()).tools}

        self.assertEqual(
            anyio.run(list_tools),
            {"list_allowed_commands", "run_command", "run_script", "get_system_status", "get_metrics"},
        )


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        whitelist = Path(tmp.name) / "commands_whitelist.md"
        whitelist.write_text(textwrap.dedent("""\
            ```yaml
            allow:
              - pattern: '^df -h$'
            confirm:
              - pattern: '^reboot$'
            deny:
              - pattern: '^rm '
            ```
        """))
        patcher = mock.patch.object(mcp_command_server, "WHITELIST_MD", whitelist)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unmatched_command_is_refused(self) -> None:
        self.assertEqual(mcp_command_server._classify("cat /etc/shadow")[0], "unknown")

    def test_rules_still_apply(self) -> None:
        self.assertEqual(mcp_command_server._classify("df -h")[0], "allow")
        self.assertEqual(mcp_command_server._classify("rm -rf /")[0], "deny")
        self.assertEqual(mcp_command_server._classify("reboot")[0], "confirm")

    def test_elevated_skips_confirmation_only(self) -> None:
        classify = mcp_command_server._classify
        self.assertEqual(classify("reboot", "elevated")[0], "allow")
        self.assertEqual(classify("cat /etc/shadow", "elevated")[0], "unknown")

    def test_unrestricted_allows_unmatched_but_not_deny(self) -> None:
        classify = mcp_command_server._classify
        self.assertEqual(classify("cat /etc/os-release", "unrestricted")[0], "allow")
        self.assertEqual(classify("rm -rf /", "unrestricted")[0], "deny")

    def test_audit_log_is_protected_on_every_level(self) -> None:
        for level in ("normal", "elevated", "unrestricted"):
            self.assertEqual(
                mcp_command_server._classify(": > /app/command_audit.log", level)[0], "deny")

    def test_unreachable_orchestrator_means_normal(self) -> None:
        with mock.patch.object(mcp_command_server, "ELEVATION_URL", "http://127.0.0.1:9/x"):
            self.assertEqual(mcp_command_server._elevation_level(), "normal")


class FakeClaude:
    """Writes a stand-in `claude` executable that records how it was called
    and prints `reply` as its JSON result."""

    def __init__(self, reply: dict | None, exit_code: int = 0, stderr: str = "") -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.record = Path(self.dir.name) / "call.json"
        self.bin = Path(self.dir.name) / "claude"
        self.bin.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, sys
            json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}},
                      open({str(self.record)!r}, "w"))
            sys.stderr.write({stderr!r})
            {"" if reply is None else f"print({json.dumps(json.dumps(reply))})"}
            sys.exit({exit_code})
        """))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)

    def run(self, text: str) -> str:
        with mock.patch.object(command_router, "CLAUDE_BIN", str(self.bin)):
            return anyio.run(handle_text_command, text)

    @property
    def call(self) -> dict:
        return json.loads(self.record.read_text())


class HandleTextCommandTests(unittest.TestCase):
    def test_sends_text_on_stdin_and_returns_result(self) -> None:
        fake = FakeClaude({"type": "result", "is_error": False, "result": " OK: 12% used \n"})

        outcome = fake.run("--покажи диск")
        self.assertEqual(outcome.short, "OK: 12% used")
        self.assertEqual(outcome.detail, "OK: 12% used")

        argv = fake.call["argv"]
        self.assertEqual(fake.call["stdin"], "--покажи диск")
        self.assertNotIn("--покажи диск", argv)
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__cluster,mcp__phone")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), MCP_CONFIG)

    def test_claude_error_result_raises(self) -> None:
        fake = FakeClaude({"type": "result", "is_error": True, "result": "Invalid API key"}, exit_code=1)
        with self.assertRaisesRegex(RuntimeError, "Invalid API key"):
            fake.run("статус")

    def test_non_json_output_raises_with_stderr(self) -> None:
        fake = FakeClaude(None, exit_code=1, stderr="not logged in")
        with self.assertRaisesRegex(RuntimeError, "not logged in"):
            fake.run("статус")

    def test_timeout_raises(self) -> None:
        fake = FakeClaude({"result": "OK"})
        fake.bin.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n")
        with mock.patch.object(command_router, "CLAUDE_TIMEOUT", 0.5):
            with self.assertRaisesRegex(RuntimeError, "did not answer"):
                fake.run("статус")


class StreamParsingTests(unittest.TestCase):
    def test_tool_calls_become_steps(self) -> None:
        lines = [json.dumps(e) for e in [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t1", "name": "mcp__cluster__run_command",
                 "input": {"command": "df -h"}},
                {"type": "tool_use", "id": "t2", "name": "mcp__cluster__run_command",
                 "input": {"command": "reboot"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": '{"executed": true, "exit_code": 0}'}]},
                {"type": "tool_result", "tool_use_id": "t2",
                 "content": '{"executed": false, "reason": "This command requires confirmation (x)."}'},
            ]}},
            {"type": "result", "is_error": False, "result": "REJECTED: нужно подтверждение\n\nДиск: ok"},
        ]] + ["not json"]

        final, steps, needs = parse_stream(lines)

        self.assertEqual(final["result"].splitlines()[0], "REJECTED: нужно подтверждение")
        self.assertEqual([s["tool"] for s in steps], ["cluster.run_command"] * 2)
        self.assertEqual([s["ok"] for s in steps], [True, False])
        self.assertEqual(steps[0]["args"], {"command": "df -h"})
        self.assertTrue(needs)

    def test_split_reply(self) -> None:
        self.assertEqual(split_reply("\nOK: up 3 days\n\nLoad 0.1"),
                         ("OK: up 3 days", "OK: up 3 days\n\nLoad 0.1"))


if __name__ == "__main__":
    unittest.main()
