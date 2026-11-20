from __future__ import annotations

import asyncio
import os
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from devwisp.terminal_handoff import TerminalHandoffBroker, TerminalHandoffError


class WindowsTerminalHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.broker = TerminalHandoffBroker(Path(self.directory.name) / 'terminal.sock')
        self.addAsyncCleanup(self.broker.close)
        self.broker.bind_context('context', 'thread')

    async def test_windows_reports_support_instead_of_disabling_the_feature(self):
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.assertEqual(await self.broker.status('thread'), {'supported': True, 'enabled': False})

    async def test_windows_reuses_only_explicitly_attached_terminal(self):
        terminal = AsyncMock()
        terminal.handoff_identity.return_value = ('terminal-instance', 123)
        terminal.capture_screen.return_value = 'server prompt'
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.broker.attach_terminal('thread', terminal)
            await self.broker.set_enabled('thread', True)
            result = await self.broker._dispatch({'context': 'context', 'action': 'send_command',
                                                'arguments': {'command': 'pwd', 'wait_ms': 0}})
            terminal.write.assert_awaited_once_with('pwd\r')
            self.assertEqual(result['screen'], 'server prompt')
            await self.broker.set_enabled('thread', False)
            with self.assertRaises(TerminalHandoffError):
                await self.broker._dispatch({'context': 'context', 'action': 'read_terminal', 'arguments': {}})

    async def test_replacement_and_detach_revoke_previous_grant(self):
        first, second = AsyncMock(), AsyncMock()
        first.handoff_identity.return_value = ('first', 123)
        second.handoff_identity.return_value = ('second', 456)
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.broker.attach_terminal('thread', first)
            await self.broker.set_enabled('thread', True)
            self.broker.attach_terminal('thread', second)
            self.assertFalse(self.broker.is_enabled('thread'))
            self.broker.detach_terminal('thread', first)
            await self.broker.set_enabled('thread', True)
            self.broker.detach_terminal('thread', second)
            self.assertFalse(self.broker.is_enabled('thread'))

    async def test_ssh_exit_revokes_and_does_not_send_into_local_shell(self):
        terminal = AsyncMock()
        terminal.handoff_identity.return_value = ('first', 123)
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.broker.attach_terminal('thread', terminal)
            await self.broker.set_enabled('thread', True)
            terminal.handoff_identity.return_value = None
            with self.assertRaises(TerminalHandoffError):
                await self.broker._dispatch({'context': 'context', 'action': 'send_command',
                                            'arguments': {'command': 'pwd', 'wait_ms': 0}})
            terminal.write.assert_not_awaited()
            self.assertFalse(self.broker.is_enabled('thread'))

    async def test_manual_input_blocks_ai_without_storing_typed_text(self):
        terminal = AsyncMock()
        terminal.handoff_identity.return_value = ('first', 123)
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.broker.attach_terminal('thread', terminal)
            await self.broker.set_enabled('thread', True)
            await self.broker.write_manual_input('thread', 'partial-input', terminal.write)
            terminal.write.reset_mock()
            with patch('devwisp.terminal_handoff.MANUAL_TOOL_WAIT_SECONDS', 0):
                with self.assertRaises(TerminalHandoffError):
                    await self.broker._dispatch({'context': 'context', 'action': 'send_command',
                                                'arguments': {'command': 'pwd', 'wait_ms': 0}})
            terminal.write.assert_not_awaited()
            self.assertEqual(self.broker._manual_input_until['thread'], float('inf'))

    async def test_other_chat_context_cannot_access_granted_terminal(self):
        terminal = AsyncMock()
        terminal.handoff_identity.return_value = ('first', 123)
        self.broker.bind_context('other-context', 'other-thread')
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.broker.attach_terminal('thread', terminal)
            await self.broker.set_enabled('thread', True)
            with self.assertRaises(TerminalHandoffError):
                await self.broker._dispatch({'context': 'other-context', 'action': 'read_terminal', 'arguments': {}})
            terminal.capture_screen.assert_not_awaited()

    async def test_environment_change_revokes_grant_before_next_write(self):
        terminal = AsyncMock()
        terminal.handoff_identity.return_value = ('local', 123)
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.broker.attach_terminal('thread', terminal)
            await self.broker.set_enabled('thread', True)
            terminal.handoff_identity.return_value = ('ssh', 456)
            self.assertFalse((await self.broker.status('thread'))['enabled'])
            terminal.write.assert_not_awaited()

    async def test_ssh_exit_between_initial_probe_and_send_is_rechecked(self):
        terminal = AsyncMock()
        terminal.handoff_identity.return_value = ('ssh', 456)
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.broker.attach_terminal('thread', terminal)
            await self.broker.set_enabled('thread', True)
            terminal.handoff_identity.side_effect = [('ssh', 456), ('local', 123)]
            with self.assertRaises(TerminalHandoffError):
                await self.broker._dispatch({'context': 'context', 'action': 'send_command',
                                            'arguments': {'command': 'pwd', 'wait_ms': 0}})
            terminal.write.assert_not_awaited()
            self.assertFalse(self.broker.is_enabled('thread'))

    async def test_interrupt_uses_the_same_terminal_without_spawning_shell(self):
        terminal = AsyncMock()
        terminal.handoff_identity.return_value = ('local', 123)
        terminal.capture_screen.return_value = 'prompt'
        with patch('devwisp.terminal_handoff.os.name', 'nt'):
            self.broker.attach_terminal('thread', terminal)
            await self.broker.set_enabled('thread', True)
            result = await self.broker._dispatch({'context': 'context', 'action': 'interrupt_command', 'arguments': {}})
            terminal.write.assert_awaited_once_with('\x03')
            self.assertEqual(result['key'], 'Ctrl-C')

    @unittest.skipUnless(os.name == 'nt', 'Windows loopback transport')
    async def test_authenticated_loopback_transport_from_mcp_client(self):
        from devwisp.terminal_handoff import _broker_request
        terminal = AsyncMock()
        terminal.handoff_identity.return_value = ('first', 123)
        terminal.capture_screen.return_value = 'server prompt'
        self.broker.attach_terminal('thread', terminal)
        await self.broker.start()
        await self.broker.set_enabled('thread', True)
        with patch.dict(os.environ, self.broker.mcp_environment()):
            self.assertEqual(await asyncio.to_thread(_broker_request, 'context', 'read_terminal', {}),
                             {'screen': 'server prompt'})
            with patch.dict(os.environ, {'DEVWISP_TERMINAL_TOKEN': 'wrong'}):
                with self.assertRaises(TerminalHandoffError):
                    await asyncio.to_thread(_broker_request, 'context', 'read_terminal', {})


@unittest.skipUnless(os.name == 'nt', 'requires real Windows PTY')
class WindowsTerminalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_tree_distinguishes_local_and_exact_ssh_instance(self):
        from devwisp.terminal import TerminalSession
        terminal = TerminalSession(Path.cwd(), 100, 30)
        terminal._windows_process = Mock(pid=123)
        root, ssh = Mock(), Mock()
        root.pid, ssh.pid = 123, 456
        root.is_running.return_value = ssh.is_running.return_value = True
        root.create_time.return_value, ssh.create_time.return_value = 100.0, 200.0
        root.children.return_value = []
        ssh.name.return_value = 'ssh.exe'
        with patch('psutil.Process', return_value=root):
            local = await terminal.handoff_identity()
            self.assertIn(':local:', local[0])
            root.children.return_value = [ssh]
            remote = await terminal.handoff_identity()
            self.assertIn(':ssh:', remote[0])
            self.assertEqual(remote[1], 456)
            self.assertNotEqual(local, remote)
            ssh.create_time.return_value = 201.0
            self.assertNotEqual(remote, await terminal.handoff_identity())
            root.children.return_value = [ssh, ssh]
            with self.assertRaises(RuntimeError):
                await terminal.handoff_identity()
            root.children.return_value = []
            self.assertEqual(local, await terminal.handoff_identity())

    async def test_codex_injects_authenticated_transport_on_start_and_resume(self):
        from devwisp.codex_app_server import CodexAppServer
        with tempfile.TemporaryDirectory() as directory:
            broker = TerminalHandoffBroker(Path(directory) / 'terminal.sock')
            try:
                await broker.start()
                server = CodexAppServer('codex', Path(directory))
                server.enable_terminal_handoff(broker)
                server.request = AsyncMock(return_value={'thread': {'id': 'thread', 'cwd': directory}})
                server._prepare_runtime_thread = AsyncMock()
                server._runtime_home = server._history_home
                await server.start_thread()
                await server.resume_thread('thread')
                for call in server.request.await_args_list:
                    env = call.args[1]['config']['mcp_servers']['devwisp_terminal']['env']
                    self.assertTrue(env['DEVWISP_TERMINAL_SOCKET'].startswith('tcp://127.0.0.1:'))
                    self.assertEqual(env['DEVWISP_TERMINAL_TOKEN'], broker.mcp_environment()['DEVWISP_TERMINAL_TOKEN'])
                    self.assertEqual(broker._contexts[env['DEVWISP_TERMINAL_CONTEXT']], 'thread')
            finally:
                await broker.close()

    async def test_real_powershell_and_independent_mcp_process(self):
        import json
        from devwisp.terminal import TerminalSession
        with tempfile.TemporaryDirectory() as directory:
            broker = TerminalHandoffBroker(Path(directory) / 'terminal.sock')
            terminal = await TerminalSession.create(Path(directory), thread_id='integration')
            pump = None
            client = None
            try:
                await broker.start()
                broker.attach_terminal('integration', terminal)
                broker.bind_context('integration-context', 'integration')

                async def consume():
                    while True:
                        output = await terminal.read()
                        if not output:
                            return
                        if b'\x1b[6n' in output:
                            await terminal.write('\x1b[1;1R')

                pump = asyncio.create_task(consume())
                await asyncio.sleep(1)
                await broker.set_enabled('integration', True)
                env = {**os.environ, **broker.mcp_environment(), 'DEVWISP_TERMINAL_CONTEXT': 'integration-context',
                       'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
                client = await asyncio.create_subprocess_exec(
                    sys.executable, '-m', 'devwisp.terminal_handoff', env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                # Split marker: an echoed input line alone cannot satisfy this assertion.
                request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                    'name': 'send_command', 'arguments': {
                        'command': "Write-Output ('WINDOWS_' + 'HANDOFF_OK')", 'wait_ms': 1000}}}
                stdout, stderr = await asyncio.wait_for(client.communicate((json.dumps(request) + '\n').encode()), 15)
                self.assertEqual(client.returncode, 0, stderr.decode(errors='replace'))
                response = json.loads(stdout)
                self.assertFalse(response['result']['isError'], response)
                result = json.loads(response['result']['content'][0]['text'])
                self.assertIn('WINDOWS_HANDOFF_OK', result['screen'])
                self.assertFalse(terminal.persistent)
                self.assertTrue(terminal.handoff_available)
            finally:
                if client is not None and client.returncode is None:
                    client.kill()
                    await client.wait()
                broker.detach_terminal('integration', terminal)
                await terminal.close()
                if pump is not None:
                    pump.cancel()
                    await asyncio.gather(pump, return_exceptions=True)
                await broker.close()
