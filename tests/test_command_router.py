import unittest

import anyio

from command_router import execute_tool_call


class ExecuteToolCallTests(unittest.TestCase):
    def test_starts_mcp_server_with_project_environment(self) -> None:
        result = anyio.run(
            execute_tool_call,
            {"tool": "list_allowed_commands", "args": {}}
        )

        self.assertTrue(result.startswith("OK:"), result)


if __name__ == "__main__":
    unittest.main()
