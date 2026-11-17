from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from devwisp.claude_code import CLAUDE_PERMISSION_SERVICE_ERROR, CLAUDE_ROOT_BYPASS_ERROR, ClaudeCodeServer
from devwisp.permission_prompt import MCP_SERVER_NAME, PermissionPromptBroker


@unittest.skipUnless(os.name == "posix", "requires an executable fake Claude CLI")
class ClaudeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def run_cli(self, body: str, *, prompt: str = "hello", permission_service: bool = False) -> tuple[dict, list, list]:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            cli = workspace / "fake-claude"
            cli.write_text(
                "#!/usr/bin/env python3\nimport sys,json\n"
                "open('args.json','w').write(json.dumps(sys.argv[1:]))\n" + body,
            )
            cli.chmod(0o755)
            events = []

            async def emit(event):
                events.append(event)

            server = ClaudeCodeServer(str(cli), workspace, workspace / "claude.sqlite3", emit)
            if permission_service:
                server.enable_permission_prompt(PermissionPromptBroker(workspace / "permission.sock", emit))
            try:
                thread = await server.create_thread()
                await server.start_turn(thread["id"], prompt, permission_mode="bypassPermissions")
                await asyncio.wait_for(asyncio.gather(*tuple(server._tasks.values())), 5)
                history = await server.read_thread(thread["id"])
                return history, events, json.loads((workspace / "args.json").read_text())
            finally:
                await server.close()

    async def test_failed_required_permission_service_reports_actionable_error(self) -> None:
        history, events, _ = await self.run_cli(
            "sys.stdin.readline()\n"
            f"print(json.dumps({{'type':'system','subtype':'init','mcp_servers':[{{'name':{MCP_SERVER_NAME!r},'status':'failed'}}]}}),flush=True)\n"
            "sys.stdin.read()\n",
            permission_service=True,
        )
        self.assertEqual(history["turns"][-1]["status"], "failed")
        self.assertEqual([item["text"] for item in history["turns"][-1]["items"] if item["type"] == "agentMessage"], [CLAUDE_PERMISSION_SERVICE_ERROR])

    async def test_optional_mcp_failure_does_not_interrupt_healthy_permission_service(self) -> None:
        history, _, _ = await self.run_cli(
            "sys.stdin.readline()\n"
            f"print(json.dumps({{'type':'system','subtype':'init','mcp_servers':[{{'name':{MCP_SERVER_NAME!r},'status':'connected'}},{{'name':'optional','status':'failed'}}]}}),flush=True)\n"
            "print(json.dumps({'type':'result','result':'OK','is_error':False}),flush=True)\n"
            "sys.stdin.read()\n",
            permission_service=True,
        )
        self.assertEqual(history["turns"][-1]["status"], "completed")

    async def test_root_bypass_failure_ends_turn_and_explains_native_restriction(self) -> None:
        history, events, args = await self.run_cli(
            "sys.stdin.readline()\n"
            "sys.stderr.write('sensitive-placeholder\\n' * 100000)\n"
            "sys.stderr.write('--dangerously-skip-permissions cannot be used with root/sudo privileges for security reasons\\n')\n"
            "sys.exit(1)\n",
        )
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--permission-mode", args)
        self.assertEqual(history["status"]["type"], "idle")
        self.assertEqual(history["turns"][-1]["status"], "failed")
        replies = [item["text"] for item in history["turns"][-1]["items"] if item["type"] == "agentMessage"]
        self.assertEqual(replies, [CLAUDE_ROOT_BYPASS_ERROR])
        self.assertNotIn("sensitive-placeholder", json.dumps(events))

    async def test_unknown_stderr_is_not_exposed_as_chat_content(self) -> None:
        history, events, _ = await self.run_cli(
            "sys.stdin.readline()\n"
            "sys.stderr.write('sensitive-placeholder: unrecognized failure\\n')\n"
            "sys.exit(1)\n",
        )
        replies = [item["text"] for item in history["turns"][-1]["items"] if item["type"] == "agentMessage"]
        self.assertEqual(replies, ["Claude Code CLI 启动失败，请检查本机 CLI 参数与配置。"])
        self.assertNotIn("sensitive-placeholder", json.dumps(events))

    async def test_supported_bypass_startup_still_returns_normal_reply(self) -> None:
        history, _, _ = await self.run_cli(
            "sys.stdin.readline()\n"
            "print(json.dumps({'type':'system','subtype':'init'}),flush=True)\n"
            "print(json.dumps({'type':'assistant','message':{'id':'reply','content':[{'type':'text','text':'OK'}]}}),flush=True)\n"
            "print(json.dumps({'type':'result','result':'OK','is_error':False}),flush=True)\n"
            "sys.stdin.read()\n",
        )
        self.assertEqual(history["turns"][-1]["status"], "completed")
        replies = [item["text"] for item in history["turns"][-1]["items"] if item["type"] == "agentMessage"]
        self.assertEqual(replies, ["OK"])

    async def test_early_cli_exit_during_input_still_shows_root_restriction(self) -> None:
        history, _, _ = await self.run_cli(
            "sys.stderr.write('--dangerously-skip-permissions cannot be used with root/sudo privileges for security reasons\\n')\n"
            "sys.exit(1)\n",
            prompt="long prompt " * 100000,
        )
        replies = [item["text"] for item in history["turns"][-1]["items"] if item["type"] == "agentMessage"]
        self.assertEqual(replies, [CLAUDE_ROOT_BYPASS_ERROR])
        self.assertEqual(history["turns"][-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
