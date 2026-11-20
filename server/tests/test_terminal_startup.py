from __future__ import annotations

import asyncio
import os
import select
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import uvloop
except ImportError:
    uvloop = None

from devwisp.terminal import TerminalSession, _run_tmux


@unittest.skipUnless(os.name == 'posix' and shutil.which('tmux'), 'requires POSIX tmux')
class TerminalStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='devwisp-terminal-test-')
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {'TMUX_TMPDIR': str(self.root)})
        self.environment.start()
        self.sessions = []

    async def asyncTearDown(self):
        for session in self.sessions:
            await session.close()
        # This socket lives only inside our temporary directory.
        await _run_tmux('kill-server', check=False)
        self.environment.stop()
        self.directory.cleanup()

    async def create(self, thread_id):
        session = await TerminalSession.create(self.root, thread_id=thread_id)
        self.sessions.append(session)
        return session

    async def expect_output(self, session, marker):
        output = b''
        async with asyncio.timeout(5):
            while marker.encode() not in output:
                if select.select([session._master_fd], [], [], 0)[0]:
                    try:
                        part = os.read(session._master_fd, 65536)
                    except OSError:
                        part = b''
                    if not part:
                        self.fail(f'terminal exited before output: {output!r}')
                    output += part
                await asyncio.sleep(.02)
        self.assertNotIn(b'error connecting', output)
        return output

    async def test_deepseek_cold_server_starts_and_executes(self):
        self.assertEqual(await _run_tmux('list-sessions', check=False), '')
        session = await self.create('deepseek:cold-start')
        await session.write("printf '__%s__\\n' DEEPSEEK_TERMINAL_OK\n")
        await self.expect_output(session, '__DEEPSEEK_TERMINAL_OK__')
        self.assertTrue(session.persistent)
        self.assertIsNone(session._process.returncode)

    async def test_concurrent_engines_start_from_missing_socket(self):
        sessions = await asyncio.gather(*(self.create(thread) for thread in ['deepseek:one', 'claude:two', 'codex-three']))
        for index, session in enumerate(sessions):
            await session.write(f"printf '__%s__\\n' ENGINE_{index}\n")
            await self.expect_output(session, f'__ENGINE_{index}__')
        self.assertEqual(len({s.tmux_session_name for s in sessions}), 3)

    async def test_reconnect_preserves_shell_state_and_session(self):
        first = await self.create('deepseek:reconnect')
        await first.write("export DEVWISP_TEST_STATE=RETAINED; printf '__%s__\\n' STATE_SET\n")
        await self.expect_output(first, '__STATE_SET__')
        await first.close()
        second = await self.create('deepseek:reconnect')
        self.assertEqual(first.tmux_session_name, second.tmux_session_name)
        await second.write("printf '__%s__\\n' \"$DEVWISP_TEST_STATE\"\n")
        await self.expect_output(second, '__RETAINED__')


@unittest.skipUnless(os.name == 'posix' and shutil.which('tmux') and uvloop is not None,
                     'requires POSIX tmux and production Uvicorn uvloop')
class TerminalUvloopStartupTests(TerminalStartupTests):
    @classmethod
    def setUpClass(cls):
        # Python 3.12's IsolatedAsyncioTestCase uses the event-loop policy.
        cls.previous_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop_policy(cls.previous_policy)


class TmuxCommandFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_has_actionable_message(self):
        with patch('devwisp.terminal.shutil.which', return_value='/usr/bin/tmux'), patch(
            'devwisp.terminal.subprocess.run', side_effect=subprocess.TimeoutExpired('tmux', 5),
        ):
            with self.assertRaisesRegex(RuntimeError, 'tmux 命令超时'):
                await _run_tmux('list-sessions')

    async def test_nonzero_exit_retains_stderr_and_unchecked_probe(self):
        result = subprocess.CompletedProcess(['tmux'], 1, b'', b'no server running\n')
        with patch('devwisp.terminal.shutil.which', return_value='/usr/bin/tmux'), patch(
            'devwisp.terminal.subprocess.run', return_value=result,
        ):
            with self.assertRaisesRegex(RuntimeError, 'no server running'):
                await _run_tmux('list-sessions')
            self.assertEqual(await _run_tmux('has-session', check=False), '')
