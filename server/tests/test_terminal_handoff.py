from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from devwisp.terminal_handoff import (
    TERMINAL_PROBE_PAUSED, TerminalHandoffBroker, TerminalHandoffError,
)


class TerminalHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.broker = TerminalHandoffBroker(Path(self.directory.name) / "terminal.sock")
        self.thread = "sample-thread"
        self.context = "sample-context"
        self.broker.bind_context(self.context, self.thread)
        self.tmux = AsyncMock(side_effect=self.respond)
        self.patch = patch("devwisp.terminal_handoff._run_tmux", self.tmux)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.snapshot = "%7\t123\tssh\n"
        await self.broker.set_enabled(self.thread, True)
        self.tmux.reset_mock()

    async def respond(self, *args, **kwargs):
        if "#{pane_id}" in args[-1]:
            return self.snapshot
        if args[0] == "capture-pane":
            return "remote prompt"
        return ""

    async def request(self, action="read_terminal", arguments=None):
        return await self.broker._dispatch({"context": self.context, "action": action, "arguments": arguments or {}})

    async def test_probe_failure_preserves_grant_and_recovers_without_rehandoff(self):
        for error in (RuntimeError("tmux timed out"), OSError("temporary failure")):
            with self.subTest(error=type(error).__name__):
                self.tmux.side_effect = error
                status = await self.broker.status(self.thread)
                self.assertTrue(status["enabled"])
                self.assertEqual(status["reason"], TERMINAL_PROBE_PAUSED)
                self.assertTrue(self.broker.protected_session_names())
        self.tmux.side_effect = self.respond
        self.assertEqual(await self.broker.status(self.thread), {"supported": True, "enabled": True})
        self.assertEqual((await self.request())["screen"], "remote prompt")

    async def test_reaper_probe_failure_does_not_revoke_or_drop_manual_input_guard(self):
        self.broker._manual_input_until[self.thread] = float("inf")
        self.tmux.side_effect = RuntimeError("temporary failure")
        await self.broker.refresh_enabled_sessions()
        self.assertTrue(self.broker.is_enabled(self.thread))
        self.assertEqual(self.broker._manual_input_until[self.thread], float("inf"))

    async def test_unreadable_probe_never_sends_command(self):
        self.tmux.side_effect = RuntimeError("temporary failure")
        with self.assertRaises(TerminalHandoffError):
            await self.request("send_command", {"command": "pwd", "wait_ms": 0})
        self.assertFalse(any(call.args[0] == "send-keys" for call in self.tmux.await_args_list))
        self.assertTrue(self.broker.is_enabled(self.thread))

    async def test_empty_or_malformed_probe_is_temporary(self):
        for snapshot in ("", "ssh", "%7\tnot-a-pid\tssh", "%7\t0\tssh"):
            self.snapshot = snapshot
            self.assertTrue((await self.broker.status(self.thread))["enabled"])
        self.snapshot = "%7\t123\tssh"
        self.assertNotIn("reason", await self.broker.status(self.thread))

    async def test_ssh_exit_revokes_and_explains_reason(self):
        self.snapshot = "%7\t123\tbash"
        status = await self.broker.status(self.thread)
        self.assertFalse(status["enabled"])
        self.assertIn("SSH", status["reason"])
        self.assertFalse(self.broker.protected_session_names())
        with self.assertRaises(TerminalHandoffError):
            await self.request("send_command", {"command": "pwd"})

    async def test_replacement_pane_or_server_cannot_inherit_grant(self):
        for snapshot in ("%8\t123\tssh", "%7\t456\tssh"):
            self.snapshot = snapshot
            self.assertFalse((await self.broker.status(self.thread))["enabled"])
            await self.broker.set_enabled(self.thread, True)
            self.assertTrue(self.broker.is_enabled(self.thread))

    async def test_command_targets_the_verified_pane(self):
        await self.request("send_command", {"command": "pwd", "wait_ms": 0})
        sends = [call.args for call in self.tmux.await_args_list if call.args[0] == "send-keys"]
        self.assertEqual(sends, [("send-keys", "-t", "%7", "-l", "--", "pwd"), ("send-keys", "-t", "%7", "Enter")])

    async def test_explicit_revoke_remains_effective_after_probe_recovers(self):
        self.tmux.side_effect = RuntimeError("temporary failure")
        await self.broker.status(self.thread)
        await self.broker.set_enabled(self.thread, False)
        self.tmux.side_effect = self.respond
        self.assertFalse((await self.broker.status(self.thread))["enabled"])
        with self.assertRaises(TerminalHandoffError):
            await self.request()

    async def test_inflight_status_does_not_overwrite_a_new_explicit_grant(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.snapshot
        async def delayed_probe(*args, **kwargs):
            if "#{pane_id}" in args[-1]:
                entered.set()
                await release.wait()
                return original.replace("ssh", "bash")
            return ""
        self.tmux.side_effect = delayed_probe
        status = asyncio.create_task(self.broker.status(self.thread))
        await entered.wait()
        grant = asyncio.create_task(self.broker.set_enabled(self.thread, True))
        await asyncio.sleep(0)
        self.assertFalse(grant.done())
        self.tmux.side_effect = self.respond
        release.set()
        self.assertFalse((await status)["enabled"])
        self.assertTrue((await grant)["enabled"])
