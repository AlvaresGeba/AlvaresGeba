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
from command_router import MCP_CONFIG, handle_text_command

SERVER_SCRIPT = Path(mcp_command_server.__file__)


class McpConfigTests(unittest.TestCase):
    def test_config_points_claude_at_executor_over_http(self) -> None:
        self.assertEqual(
            MCP_CONFIG["mcpServers"]["cluster"],
            {"type": "http", "url": command_router.EXECUTOR_URL},
        )

    def test_server_exposes_cluster_tools(self) -> None:
        async def list_tools() -> set[str]:
            params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return {tool.name for tool in (await session.list_tools()).tools}

        self.assertEqual(
            anyio.run(list_tools),
            {"list_allowed_commands", "run_command", "run_script", "get_system_status"},
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

        self.assertEqual(fake.run("--покажи диск"), "OK: 12% used")

        argv = fake.call["argv"]
        self.assertEqual(fake.call["stdin"], "--покажи диск")
        self.assertNotIn("--покажи диск", argv)
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__cluster")
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


if __name__ == "__main__":
    unittest.main()
