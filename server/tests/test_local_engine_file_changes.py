from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from devwisp.claude_code import ClaudeCodeServer
from test_deepseek_harness import _RealHarnessFixture


@unittest.skipUnless(os.name == 'posix', 'requires an executable fake Claude CLI')
class ClaudeFileChangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        cli = self.root / 'fake-claude'
        cli.write_text('''#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
record=json.loads(sys.stdin.readline())
command=json.loads(record['message']['content'][0]['text'])
print(json.dumps({'type':'system','subtype':'init'}),flush=True)
for path, text in command.get('writes',{}).items():
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text)
for path in command.get('deletes',[]): Path(path).unlink()
if command.get('wait'): time.sleep(60)
print(json.dumps({'type':'result','result':'Done','is_error':command.get('fail',False)}),flush=True)
sys.stdin.read()
''')
        cli.chmod(0o755)
        self.events = []
        async def emit(event): self.events.append(event)
        # Deliberately put the runtime DB inside the project to prove it is
        # excluded, rather than making every no-op look like a code edit.
        self.server = ClaudeCodeServer(str(cli), self.workspace, self.workspace / 'runtime.sqlite3', emit)
        self.server._native_home = self.workspace / 'sdk-state'
        self.addAsyncCleanup(self.server.close)

    async def turn(self, thread, command):
        turn_id = await self.server.start_turn(thread['id'], json.dumps(command))
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 5)
        history = await self.server.read_thread(thread['id'])
        return turn_id, history['turns'][-1]

    @staticmethod
    def changes(turn):
        return [i for i in turn['items'] if i['type'] == 'fileChange']

    async def test_command_edits_have_counts_diff_history_and_no_runtime_or_ignored_files(self):
        (self.workspace / 'edit.txt').write_text('kept\nold\n')
        (self.workspace / 'delete.txt').write_text('removed\n')
        thread = await self.server.create_thread()
        turn_id, turn = await self.turn(thread, {'writes': {
            'edit.txt': 'kept\nnew\nextra\n', 'add.md': 'first\nsecond\n',
            'node_modules/ignored.txt': 'dependency', '.git/ignored.txt': 'git state',
            'sdk-state/internal.txt': 'SDK state', str(self.root / 'outside.txt'): 'outside',
        }, 'deletes': ['delete.txt']})
        self.assertEqual(turn['status'], 'completed')
        items = self.changes(turn)
        self.assertEqual(len(items), 1)
        by_path = {change['path']: change for change in items[0]['changes']}
        self.assertEqual(set(by_path), {'edit.txt', 'add.md', 'delete.txt'})
        self.assertEqual((by_path['edit.txt']['additions'], by_path['edit.txt']['deletions']), (2, 1))
        self.assertEqual((by_path['add.md']['additions'], by_path['add.md']['deletions']), (2, 0))
        self.assertEqual((by_path['delete.txt']['additions'], by_path['delete.txt']['deletions']), (0, 1))
        self.assertIn('-old\n+new\n+extra', by_path['edit.txt']['diff'])
        event_index = next(i for i, e in enumerate(self.events)
                           if e.get('params', {}).get('item', {}).get('id') == items[0]['id'])
        completion = next(i for i, e in enumerate(self.events)
                          if e.get('method') == 'turn/completed' and e['params']['turn']['id'] == turn_id)
        self.assertLess(event_index, completion)
        self.assertEqual(self.events[event_index]['params']['item'], items[0])
        reopened = ClaudeCodeServer(self.server.command, self.workspace, self.server.database_path, self.server.emit)
        try:
            self.assertEqual(self.changes((await reopened.read_thread(thread['id']))['turns'][0]), items)
        finally:
            await reopened.close()

    async def test_noop_next_turn_and_separate_working_directory_do_not_reuse_changes(self):
        thread = await self.server.create_thread()
        first_id, first = await self.turn(thread, {'writes': {'one.txt': 'first\n'}})
        second_id, second = await self.turn(thread, {'writes': {'one.txt': 'second\n'}})
        self.assertNotEqual(first_id, second_id)
        self.assertIn('+first', self.changes(first)[0]['changes'][0]['diff'])
        self.assertIn('-first\n+second', self.changes(second)[0]['changes'][0]['diff'])
        _, no_change = await self.turn(thread, {})
        self.assertEqual(self.changes(no_change), [])
        other_workspace = self.root / 'other-project'
        other_workspace.mkdir()
        other = await self.server.create_thread(cwd=other_workspace)
        _, other_turn = await self.turn(other, {'writes': {'other.txt': 'independent\n'}})
        self.assertEqual([entry['path'] for entry in self.changes(other_turn)[0]['changes']], ['other.txt'])
        original = await self.server.read_thread(thread['id'])
        self.assertEqual(len(original['turns']), 3)
        self.assertEqual(self.changes(original['turns'][0]), self.changes(first))

    async def test_failed_and_interrupted_turns_keep_files_already_written(self):
        thread = await self.server.create_thread()
        _, failed = await self.turn(thread, {'writes': {'partial.txt': 'written before failure\n'}, 'fail': True})
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(self.changes(failed)[0]['changes'][0]['path'], 'partial.txt')
        turn_id = await self.server.start_turn(thread['id'], json.dumps({'writes': {'stopped.txt': 'written before stop\n'}, 'wait': True}))
        for _ in range(100):
            if (self.workspace / 'stopped.txt').exists(): break
            await asyncio.sleep(.02)
        self.assertTrue((self.workspace / 'stopped.txt').exists())
        self.assertTrue(await self.server.interrupt(thread['id'], turn_id))
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values()), return_exceptions=True), 5)
        turn = (await self.server.read_thread(thread['id']))['turns'][-1]
        self.assertEqual(turn['status'], 'stopped')
        self.assertEqual(self.changes(turn)[0]['changes'][0]['path'], 'stopped.txt')

    async def test_snapshot_failures_do_not_break_reply_or_completion(self):
        thread = await self.server.create_thread()
        for failures in ([OSError('baseline unavailable')], [None, OSError('final unavailable')]):
            from devwisp.codex_app_server import snapshot_workspace
            real = snapshot_workspace
            calls = iter(failures)
            def snapshot(*args, **kwargs):
                failure = next(calls, None)
                if failure: raise failure
                return real(*args, **kwargs)
            with patch('devwisp.claude_code.snapshot_workspace', side_effect=snapshot):
                _, turn = await self.turn(thread, {})
            self.assertEqual(turn['status'], 'completed')
            self.assertEqual(self.changes(turn), [])
        self.assertIsNone(self.server._row(thread['id'])['running_turn_id'])

    async def test_stop_during_final_snapshot_still_journals_changes_and_finishes(self):
        from devwisp.codex_app_server import snapshot_workspace
        thread = await self.server.create_thread()
        capture_started, release = threading.Event(), threading.Event()
        calls = 0
        def delayed_snapshot(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                capture_started.set()
                release.wait(3)
            return snapshot_workspace(*args, **kwargs)
        with patch('devwisp.claude_code.snapshot_workspace', side_effect=delayed_snapshot):
            try:
                turn_id = await self.server.start_turn(thread['id'], json.dumps({'writes': {'final.txt': 'saved\n'}}))
                for _ in range(100):
                    if capture_started.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(capture_started.is_set())
                for _ in range(2):
                    self.assertTrue(await self.server.interrupt(thread['id'], turn_id))
                    await asyncio.sleep(0)
            finally:
                release.set()
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values()), return_exceptions=True), 3)
        self.assertIsNone(self.server._row(thread['id'])['running_turn_id'])
        self.assertFalse(self.server._tasks)
        self.assertFalse(self.server._turn_inputs)
        turn = (await self.server.read_thread(thread['id']))['turns'][-1]
        self.assertEqual(self.changes(turn)[0]['changes'][0]['path'], 'final.txt')


class DeepSeekFileChangeTests(_RealHarnessFixture):
    async def test_native_command_and_edit_tool_restore_each_turn_card_with_reused_runtime(self):
        self.server.save_settings('fixture-model', f'http://127.0.0.1:{self.http.server_port}', 'fixture-placeholder', protocol='chat')
        self.http.tool_enabled = self.http.tools_each_turn = True
        self.http.tool_name = 'bash'
        thread = await self.server.create_thread()
        lease = None
        saved = []
        for value in ('first', 'second'):
            self.http.tool_name = 'bash' if value == 'first' else 'edit'
            self.http.tool_arguments = ({'command': "printf 'first\\n' > edited.txt", 'description': 'Write the fixture'} if value == 'first' else
                                        {'file_path': str(self.root / 'edited.txt'), 'old_string': 'first', 'new_string': 'second',
                                         'description': 'Edit the fixture'})
            turn_id = await self.server.start_turn(thread['id'], 'Edit the fixture file',
                                                   deepseek_agent_preset='standard', deepseek_permission_mode='danger-full-access')
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
            history = await self.server.read_thread(thread['id'])
            turn = history['turns'][-1]
            self.assertEqual(turn['status'], 'completed', history)
            changes = [i for i in turn['items'] if i['type'] == 'fileChange']
            self.assertEqual(len(changes), 1, history)
            self.assertEqual(changes[0]['id'], f'workspace-change-{turn_id}')
            self.assertEqual([c['path'] for c in changes[0]['changes']], ['edited.txt'])
            self.assertIn(f'+{value}', changes[0]['changes'][0]['diff'])
            if lease: self.assertIs(self.server._runtimes[thread['id']], lease)
            lease = self.server._runtimes[thread['id']]
            saved.append(changes[0])
        self.assertIn('-first\n+second', saved[1]['changes'][0]['diff'])
        self.assertEqual([[i for i in t['items'] if i['type'] == 'fileChange'][0] for t in history['turns']], saved)
        self.assertNotIn('fixture-placeholder', json.dumps(saved))
