from __future__ import annotations

import asyncio
import sqlite3
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

from devwisp.collaboration import CollaborationError, CollaborationService, CollaborationStore
from devwisp.collaboration_groups import mention_recipients


def spec(**changes):
    return {'requestId': str(uuid.uuid4()), 'title': 'Fix parser', 'goal': 'Fix and validate parsing', 'cwd': '/work',
            'coder': {'engine': 'claude-code', 'model': ''}, 'reviewer': {'engine': 'codex', 'model': 'review-model'},
            'maxRounds': 3, **changes}


class CollaborationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.store = CollaborationStore(self.db)
        self.turns = {}
        self.outputs = []
        self.sent = []
        self.create_member = AsyncMock(side_effect=lambda member, cwd, title: f'{member["engine"]}-{title}')
        self.interrupt = AsyncMock()

        async def send(member, prompt, readonly):
            turn_id = f'turn-{len(self.sent)}'
            self.sent.append((member.copy(), prompt, readonly))
            status, text = self.outputs.pop(0)
            self.turns[member['threadId']] = {'turns': [{'id': turn_id, 'status': status, 'items': [{'type': 'agentMessage', 'text': text}]}]}
            return turn_id

        async def read(member):
            return self.turns.get(member['threadId'], {'turns': []})

        self.service = CollaborationService(self.store, self.create_member, send, read, self.interrupt)
        self.addAsyncCleanup(self.service.close)

    async def run_task(self, **changes):
        result = self.service.create(spec(**changes))
        await self.service._workers[result['id']]
        return self.store.detail(result['id'])

    def test_attachment_migration_preserves_old_group_events(self):
        with sqlite3.connect(':memory:', isolation_level=None) as database:
            database.execute('CREATE TABLE collaboration_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL)')
            database.execute("INSERT INTO collaboration_events(task_id, role, text, created_at) VALUES ('old-group', 'human', 'Old message', '2026-11-20')")
            migrated = CollaborationStore(database)
            self.assertEqual(migrated.events('old-group')[0]['text'], 'Old message')
            migrated.event('old-group', 'human', 'New message', {'documents': [{'name': 'notes.txt', 'content': 'Document'}]})
            self.assertEqual(migrated.events('old-group')[1]['documents'][0]['name'], 'notes.txt')

    async def test_implements_reviews_fixes_and_finishes_with_independent_threads(self):
        self.outputs = [('completed', 'Implementation tested\nDEVWISP_HANDOFF'),
                        ('completed', 'Missing empty case\nDEVWISP_REVIEW: CHANGES_REQUESTED'),
                        ('completed', 'Empty case fixed and checked\nDEVWISP_HANDOFF'),
                        ('completed', 'No remaining issues\nDEVWISP_REVIEW: APPROVED')]
        task = await self.run_task()
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['round'], 2)
        self.assertEqual([entry[2] for entry in self.sent], [False, True, False, True])
        self.assertIn('Missing empty case', self.sent[2][1])
        self.assertNotEqual(task['members']['coder']['threadId'], task['members']['reviewer']['threadId'])
        self.assertEqual(self.create_member.await_count, 2)
        self.assertEqual(self.store.owner(task['members']['coder']['threadId']), task['id'])

    async def test_existing_members_append_new_turns_without_provisioning_or_replaying_history(self):
        self.turns['existing-coder'] = {'turns': [{'id': 'old', 'status': 'completed', 'items': [{'type': 'agentMessage', 'text': 'Previous private context'}]}]}
        self.outputs = [('completed', 'New work\nDEVWISP_HANDOFF'), ('completed', 'Reviewed\nDEVWISP_REVIEW: APPROVED')]
        task = await self.run_task(coder={'engine': 'claude-code', 'model': '', 'threadId': 'existing-coder'},
                                   reviewer={'engine': 'codex', 'model': '', 'threadId': 'existing-reviewer'})
        self.assertEqual(task['status'], 'completed')
        self.create_member.assert_not_awaited()
        self.assertEqual([entry[0]['threadId'] for entry in self.sent], ['existing-coder', 'existing-reviewer'])
        self.assertNotIn('Previous private context', self.sent[0][1])

    async def test_ownership_conflict_rolls_back_task_and_other_member_reservation(self):
        self.outputs = [('completed', 'Need user input')]
        original = await self.run_task(coder={'engine': 'codex', 'model': '', 'threadId': 'owned'})
        rejected = spec(coder={'engine': 'codex', 'model': '', 'threadId': 'unowned'},
                        reviewer={'engine': 'codex', 'model': '', 'threadId': 'owned'})
        with self.assertRaises(CollaborationError):
            self.service.create(rejected)
        self.assertIsNone(self.store.owner('unowned'))
        self.assertEqual(self.store.owner('owned'), original['id'])
        with self.assertRaises(KeyError):
            self.store.get(rejected['requestId'])

    def empty_group(self):
        return self.service.create_group({'requestId': str(uuid.uuid4()), 'cwd': '/work', 'member': None})

    async def test_dissolve_removes_group_events_and_ownership_but_preserves_native_history(self):
        group = self.empty_group()
        other = self.empty_group()
        self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex', 'threadId': 'existing'})
        self.store.event(group['id'], 'human', 'Private group message')
        self.store.event(other['id'], 'human', 'Keep this group')
        self.turns['existing'] = {'turns': [{'id': 'old', 'status': 'completed'}]}
        self.service._pending_inputs.update(question=group['id'], other=other['id'])
        await self.service.dissolve(group['id'])
        await self.service.dissolve(group['id'])
        with self.assertRaises(KeyError):
            self.store.get(group['id'])
        self.assertEqual(self.store.events(group['id']), [])
        self.assertIsNone(self.store.owner('existing'))
        self.assertEqual(self.turns['existing']['turns'][0]['id'], 'old')
        self.assertEqual(self.store.events(other['id'])[0]['text'], 'Keep this group')
        self.assertEqual(self.service._pending_inputs, {'other': other['id']})
        restarted = CollaborationStore(self.db)
        self.assertIsNone(restarted.owner('existing'))
        self.assertEqual([task['id'] for task in restarted.list()], [other['id']])

    async def test_failed_unknown_dispatch_and_empty_groups_can_be_dissolved(self):
        group = self.empty_group()
        member_id = str(uuid.uuid4())
        self.service.add_group_member(group['id'], member_id, {'engine': 'codex', 'threadId': 'unknown-dispatch'})
        task = self.store.get(group['id'])
        task.update(status='failed', activeTurn={'role': member_id, 'id': None}, run={'index': 0})
        self.store.save(task)
        await self.service.dissolve(group['id'])
        self.assertIsNone(self.store.owner('unknown-dispatch'))
        await self.service.dissolve(self.empty_group()['id'])
        self.assertEqual(self.store.list(), [])
        self.interrupt.assert_not_awaited()
        self.assertEqual(self.sent, [])

    async def test_dissolve_during_creation_drains_result_and_never_sends(self):
        group = self.empty_group()
        self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex'})
        entered, release = asyncio.Event(), asyncio.Event()

        async def create(member, cwd, title):
            entered.set()
            await release.wait()
            return 'new-native-chat'

        self.service.create_member = create
        await self.service.group_message(group['id'], str(uuid.uuid4()), 'Request')
        await asyncio.wait_for(entered.wait(), 1)
        deleting = asyncio.create_task(self.service.dissolve(group['id']))
        await asyncio.sleep(0)
        self.assertFalse(deleting.done())
        release.set()
        await asyncio.wait_for(deleting, 1)
        self.assertEqual(self.store.list(), [])
        self.assertIsNone(self.store.owner('new-native-chat'))
        self.assertEqual(self.sent, [])

    async def test_dispatch_timeout_keeps_ownership_then_retry_releases_without_resending(self):
        group = self.empty_group()
        self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex', 'threadId': 'owned'})
        self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'claude-code', 'threadId': 'next'})
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(member, prompt, readonly):
            entered.set()
            await release.wait()
            self.sent.append(member['threadId'])
            return 'admitted-turn'

        self.service.send = send
        await self.service.group_message(group['id'], str(uuid.uuid4()), 'Request')
        await asyncio.wait_for(entered.wait(), 1)
        with patch('devwisp.collaboration.DISSOLVE_TIMEOUT', 0.01):
            with self.assertRaisesRegex(CollaborationError, '稍后重试'):
                await self.service.dissolve(group['id'])
        self.assertEqual(self.store.owner('owned'), group['id'])
        self.assertEqual(self.store.get(group['id'])['status'], 'paused')
        self.assertFalse(self.service._workers[group['id']].done())
        release.set()
        await self.service.dissolve(group['id'])
        self.assertEqual(self.sent, ['owned', 'next'])
        self.assertIsNone(self.store.owner('owned'))
        self.assertIsNone(self.store.owner('next'))
        self.interrupt.assert_not_awaited()

    async def test_dissolve_while_reading_parallel_turns_never_starts_another_stage(self):
        group = self.empty_group()
        for engine in ('codex', 'claude-code'):
            self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': engine, 'threadId': engine})
        entered, release = asyncio.Event(), asyncio.Event()
        read = self.service.read

        async def slow_read(member):
            entered.set()
            await release.wait()
            return await read(member)

        self.service.read = slow_read
        self.outputs = [('completed', 'First reply')]
        await self.service.group_message(group['id'], str(uuid.uuid4()), 'Request')
        await asyncio.wait_for(entered.wait(), 1)
        deleting = asyncio.create_task(self.service.dissolve(group['id']))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(deleting, 1)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.store.list(), [])

    async def test_running_legacy_task_dissolves_without_waiting_for_native_reply(self):
        self.outputs = [('inProgress', '')]
        task = self.service.create(spec())
        worker = self.service._workers[task['id']]
        # Let the worker reach its turn wait, without completing the native turn.
        for _ in range(10):
            if self.sent:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(self.sent), 1)
        native_ids = [member['threadId'] for member in self.store.get(task['id'])['members'].values()]
        await asyncio.wait_for(self.service.dissolve(task['id']), 1)
        self.assertTrue(worker.done())
        self.assertEqual(self.store.list(), [])
        self.assertTrue(all(self.store.owner(thread_id) is None for thread_id in native_ids))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(next(iter(self.turns.values()))['turns'][0]['status'], 'inProgress')
        self.interrupt.assert_not_awaited()

    async def test_atomic_delete_rolls_back_partial_cleanup(self):
        group = self.empty_group()
        self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex', 'threadId': 'owned'})
        self.store.event(group['id'], 'human', 'Keep on failure')
        self.db.execute("CREATE TRIGGER reject_group_delete BEFORE DELETE ON collaboration_tasks BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.service.dissolve(group['id'])
        self.assertEqual(self.store.owner('owned'), group['id'])
        self.assertEqual(self.store.events(group['id'])[0]['text'], 'Keep on failure')

    async def test_cancelled_delete_request_does_not_cancel_dispatch_and_concurrent_retries_are_safe(self):
        group = self.empty_group()
        self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex', 'threadId': 'owned'})
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(member, prompt, readonly):
            entered.set()
            await release.wait()
            self.sent.append(member['threadId'])
            return 'native-turn'

        self.service.send = send
        await self.service.group_message(group['id'], str(uuid.uuid4()), 'Request')
        await asyncio.wait_for(entered.wait(), 1)
        deleting = asyncio.create_task(self.service.dissolve(group['id']))
        await asyncio.sleep(0)
        deleting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await deleting
        self.assertFalse(self.service._workers[group['id']].done())
        self.assertEqual(self.store.owner('owned'), group['id'])
        release.set()
        await asyncio.wait_for(asyncio.gather(self.service.dissolve(group['id']), self.service.dissolve(group['id'])), 1)
        self.assertIsNone(self.store.owner('owned'))
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.sent, ['owned'])

    async def test_custom_empty_group_name_survives_first_and_later_messages(self):
        group = self.empty_group()
        self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex', 'model': ''})
        await self.service.rename(group['id'], '登录修复群')
        self.outputs = [('completed', 'First reply'), ('completed', 'Second reply')]
        for text in ('First request', 'Follow-up request'):
            await self.service.group_message(group['id'], str(uuid.uuid4()), text)
            await self.service._workers[group['id']]
            self.assertEqual(self.store.get(group['id'])['title'], '登录修复群')
        restarted = CollaborationStore(self.db)
        self.assertEqual(restarted.get(group['id'])['title'], '登录修复群')

    async def test_group_rename_during_reply_keeps_running_turn_and_final_name(self):
        group = self.empty_group()
        self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex', 'model': ''})
        started, release = asyncio.Event(), asyncio.Event()
        original_read = self.service.read

        async def blocked_read(member):
            started.set()
            await release.wait()
            return await original_read(member)

        self.service.read = blocked_read
        self.outputs = [('completed', 'Completed reply')]
        await self.service.group_message(group['id'], str(uuid.uuid4()), 'First request')
        await asyncio.wait_for(started.wait(), timeout=2)
        before = self.store.get(group['id'])
        renamed = await self.service.rename(group['id'], '运行中改好的名字')
        self.assertEqual((renamed['run'], renamed['activeTurn'], renamed['members'], renamed['status']),
                         (before['run'], before['activeTurn'], before['members'], before['status']))
        release.set()
        await self.service._workers[group['id']]
        self.assertEqual((self.store.get(group['id'])['title'], self.store.get(group['id'])['status']),
                         ('运行中改好的名字', 'idle'))
        self.assertEqual(len(self.sent), 1)

    async def test_empty_group_starts_without_members_or_model_calls(self):
        group = self.empty_group()
        self.assertEqual(group['status'], 'idle')
        self.assertEqual(group['members'], {})
        self.assertEqual(group['events'], [])
        self.create_member.assert_not_awaited()
        self.assertEqual(self.sent, [])
        with self.assertRaises(CollaborationError):
            await self.service.group_message(group['id'], str(uuid.uuid4()), 'Hello')

    async def test_mentions_select_specific_repeated_engine_and_multiple_members_in_joining_order(self):
        group = self.empty_group()
        keys = [str(uuid.uuid4()) for _ in range(3)]
        for key, engine in zip(keys, ['codex', 'claude-code', 'codex']):
            self.service.add_group_member(group['id'], key, {'engine': engine, 'model': ''})
        self.outputs = [('completed', 'Only Codex 2'), ('completed', 'Claude answer'), ('completed', 'Codex 2 follows')]
        await self.service.group_message(group['id'], str(uuid.uuid4()), '@Codex 2 请单独回答')
        await self.service._workers[group['id']]
        self.assertEqual([call[0]['name'] for call in self.sent], ['Codex 2'])
        self.assertIn('本轮只由被点名的成员 Codex 2', self.sent[0][1])
        await self.service.group_message(group['id'], str(uuid.uuid4()), '@Codex 2，@Claude 1 核对上条回答')
        await self.service._workers[group['id']]
        self.assertEqual([call[0]['name'] for call in self.sent], ['Codex 2', 'Claude 1', 'Codex 2'])
        self.assertIn('Only Codex 2', self.sent[1][1])
        self.assertIsNone(self.store.get(group['id'])['members'][keys[0]]['threadId'])

    def test_mentions_reject_unknown_and_ambiguous_names_without_parsing_ordinary_text(self):
        group = {'members': {'a': {'name': 'Codex 1', 'engine': 'codex'}, 'b': {'name': 'Codex 2', 'engine': 'codex'},
                             'c': {'name': 'Claude 1', 'engine': 'claude-code'}}}
        for text in ['@Codex 请回复', '@Codex 99 请回复', '@Unknown hello', '@DeepSeek hello', '@Codex 1 @Unknown hello']:
            with self.subTest(text=text), self.assertRaises(CollaborationError):
                mention_recipients(group, text)
        for text in ['没有点名', '邮件 user@host.test', '讨论 @Codex 1 的回答', '```\n@Codex 1\n```', '@all 请回复', '@所有人 请回复']:
            self.assertIsNone(mention_recipients(group, text), text)
        self.assertEqual(mention_recipients(group, '  @cLaUdE 看一下'), ['c'])
        self.assertEqual(mention_recipients(group, '@Codex 2 @Codex 2 看一下'), ['b'])

    async def test_group_supports_single_member_and_subsequent_messages_without_verdict_markers(self):
        group = self.empty_group()
        member = {'engine': 'codex', 'model': ''}
        key = str(uuid.uuid4())
        self.service.add_group_member(group['id'], key, member)
        self.outputs = [('completed', 'First ordinary reply'), ('completed', 'Follow-up answer')]
        for message in ('Implement this', 'Check it again'):
            await self.service.group_message(group['id'], str(uuid.uuid4()), message)
            await self.service._workers[group['id']]
            self.assertEqual(self.store.get(group['id'])['status'], 'idle')
        self.create_member.assert_awaited_once()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('First ordinary reply', self.sent[1][1])
        self.assertEqual([event['role'] for event in self.store.events(group['id'])], ['human', key, 'human', key])

    async def test_saved_legacy_group_round_resumes_in_original_sequential_order(self):
        group = self.empty_group()
        keys = [str(uuid.uuid4()), str(uuid.uuid4())]
        for key in keys:
            self.service.add_group_member(group['id'], key, {'engine': 'codex', 'model': ''})
        group = self.store.get(group['id'])
        group.update(status='running', phase=keys[0], goal='Legacy request',
                     run={'requestId': 'legacy', 'text': 'Legacy request', 'memberIds': keys, 'index': 0})
        self.store.save(group)
        self.outputs = [('completed', 'Legacy first result'), ('completed', 'Legacy second result')]
        self.service._launch(group['id'])
        await self.service._workers[group['id']]
        self.assertEqual(self.store.get(group['id'])['status'], 'idle')
        self.assertEqual(len(self.sent), 2)
        self.assertIn('Legacy first result', self.sent[1][1])
        self.assertFalse(self.sent[0][2])

    async def test_group_same_engine_multiple_members_share_results_and_stop_after_one_pass(self):
        group = self.empty_group()
        for _ in range(3):
            self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex', 'model': ''})
        self.outputs = [('completed', 'A implemented'), ('completed', 'B checked A'), ('completed', 'C found no regressions')]
        message_id = str(uuid.uuid4())
        await self.service.group_message(group['id'], message_id, 'Work together')
        await self.service._workers[group['id']]
        result = self.store.detail(group['id'])
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len({entry[0]['threadId'] for entry in self.sent}), 3)
        self.assertNotIn('A implemented', self.sent[1][1])
        self.assertNotIn('B checked A', self.sent[2][1])
        self.assertTrue(all(entry[2] for entry in self.sent))
        await self.service.group_message(group['id'], message_id, 'Work together')
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len(result['events']), 4)
        with self.assertRaises(CollaborationError):
            await self.service.group_message(group['id'], message_id, 'Other text')

    async def test_group_member_idempotency_and_remove_release_ownership_keep_history(self):
        group = self.empty_group()
        member_id = str(uuid.uuid4())
        member = {'engine': 'claude-code', 'model': '', 'threadId': 'existing-group-member'}
        self.service.add_group_member(group['id'], member_id, member)
        self.service.add_group_member(group['id'], member_id, member)
        self.assertEqual(len(self.store.get(group['id'])['members']), 1)
        self.outputs = [('completed', 'Original context retained')]
        await self.service.group_message(group['id'], str(uuid.uuid4()), 'Please check')
        await self.service._workers[group['id']]
        self.create_member.assert_not_awaited()
        result = await self.service.remove_group_member(group['id'], member_id)
        self.assertIsNone(self.store.owner(member['threadId']))
        self.assertEqual(result['members'], {})
        self.assertEqual(result['departedMembers'][member_id]['name'], 'Claude 1')
        self.assertIn('Original context retained', [event['text'] for event in result['events']])
        with self.assertRaises(CollaborationError):
            self.service.add_group_member(group['id'], member_id, {**member, 'model': 'different'})

    async def test_group_unknown_dispatch_is_not_replayed_and_active_roster_cannot_change(self):
        group = self.empty_group()
        member_id = str(uuid.uuid4())
        member = {'engine': 'codex', 'model': '', 'threadId': 'existing-group-member'}
        self.service.add_group_member(group['id'], member_id, member)
        self.service.send = AsyncMock(side_effect=RuntimeError('Disconnected after dispatch'))
        await self.service.group_message(group['id'], str(uuid.uuid4()), 'Work')
        with self.assertLogs('devwisp.collaboration', level='ERROR'):
            await self.service._workers[group['id']]
        with self.assertRaises(CollaborationError):
            await self.service.resume(group['id'])
        with self.assertRaises(CollaborationError):
            self.service.add_group_member(group['id'], str(uuid.uuid4()), {'engine': 'codex', 'model': ''})
        with self.assertRaises(CollaborationError):
            await self.service.remove_group_member(group['id'], member_id)
        self.service.send.assert_awaited_once()

    async def test_group_pause_resume_consumes_completed_member_without_duplicate_dispatch(self):
        group = self.empty_group()
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        self.service.add_group_member(group['id'], first, {'engine': 'codex', 'model': ''})
        self.service.add_group_member(group['id'], second, {'engine': 'claude-code', 'model': ''})
        entered, release = asyncio.Event(), asyncio.Event()
        original_read = self.service.read
        async def slow_read(member):
            entered.set()
            await release.wait()
            return await original_read(member)
        self.service.read = slow_read
        self.outputs = [('completed', 'First result'), ('completed', 'Second result')]
        await self.service.group_message(group['id'], str(uuid.uuid4()), 'Work')
        await entered.wait()
        pausing = asyncio.create_task(self.service.pause(group['id']))
        await asyncio.sleep(0)
        release.set()
        await pausing
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.interrupt.await_args_list), 2)
        # Restart persists roster and the accepted turn awaiting consumption.
        recovered = CollaborationStore(self.db).get(group['id'])
        self.assertEqual(recovered['status'], 'paused')
        self.assertEqual(len(recovered['members']), 2)
        await self.service.resume(group['id'])
        await self.service._workers[group['id']]
        result = self.store.detail(group['id'])
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(len(self.sent), 2)
        self.assertEqual([event['role'] for event in result['events']], ['human', 'system', 'system', first, second])

    async def test_completed_turn_without_handoff_does_not_trigger_reviewer(self):
        self.outputs = [('completed', 'Need a directory decision before editing')]
        task = await self.run_task()
        self.assertEqual(task['status'], 'paused')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(task['phase'], 'coder')

    async def test_quoted_or_nonfinal_approval_is_not_a_verdict(self):
        self.outputs = [('completed', 'Ready\nDEVWISP_HANDOFF'),
                        ('completed', 'DEVWISP_REVIEW: APPROVED\nBut I could not inspect the diff.')]
        task = await self.run_task()
        self.assertEqual(task['status'], 'paused')
        self.assertEqual(task['phase'], 'reviewer')

    async def test_limit_stops_then_explicit_resume_allows_one_more_round(self):
        self.outputs = [('completed', 'Ready\nDEVWISP_HANDOFF'),
                        ('completed', 'Fix this\nDEVWISP_REVIEW: CHANGES_REQUESTED')]
        task = await self.run_task(maxRounds=1)
        self.assertTrue(task['limitReached'])
        self.assertEqual(task['status'], 'paused')
        self.service.note(task['id'], 'Keep the public API unchanged')
        self.outputs = [('completed', 'Fixed\nDEVWISP_HANDOFF'), ('completed', 'Clean\nDEVWISP_REVIEW: APPROVED')]
        await self.service.resume(task['id'])
        await self.service._workers[task['id']]
        result = self.store.get(task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['maxRounds'], 2)
        self.assertIn('Keep the public API unchanged', self.sent[2][1])

    async def test_failed_turn_does_not_advance_even_with_approval_text(self):
        self.outputs = [('failed', 'DEVWISP_HANDOFF')]
        task = await self.run_task()
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(len(self.sent), 1)

    async def test_unknown_dispatch_cannot_be_replayed_on_resume(self):
        self.service.send = AsyncMock(side_effect=RuntimeError('connection lost'))
        with self.assertLogs('devwisp.collaboration', level='ERROR'):
            task = await self.run_task()
        self.assertEqual(task['status'], 'failed')
        self.assertIsNone(task['activeTurn']['id'])
        with self.assertRaises(CollaborationError):
            await self.service.resume(task['id'])
        self.service.send.assert_awaited_once()

    async def test_human_request_and_resolution_update_waiting_state(self):
        queue = asyncio.Queue()
        hub = AsyncMock()
        hub.subscribe.return_value = queue
        hub.unsubscribe = Mock()
        self.service.hub = hub
        self.outputs = [('completed', 'Need input')]
        task = await self.run_task()
        task['status'] = 'running'
        self.store.save(task)
        saved = asyncio.Event()
        original_save = self.store.save

        def observe_save(value):
            original_save(value)
            saved.set()

        self.store.save = observe_save
        self.service.start()
        await queue.put({'type': 'server_request', 'request_id': 'question-1', 'params': {'threadId': task['members']['coder']['threadId']}})
        await asyncio.wait_for(saved.wait(), 1)
        self.assertEqual(self.store.get(task['id'])['status'], 'waiting')
        saved.clear()
        await queue.put({'type': 'server_request.resolved', 'request_id': 'question-1', 'threadId': task['members']['coder']['threadId']})
        await asyncio.wait_for(saved.wait(), 1)
        self.assertEqual(self.store.get(task['id'])['status'], 'running')

    async def test_retrying_create_does_not_spawn_duplicate_members(self):
        self.outputs = [('completed', 'Need user decision')]
        data = spec()
        first = self.service.create(data)
        second = self.service.create(data)
        self.assertEqual(first['id'], second['id'])
        await self.service._workers[first['id']]
        self.assertEqual(self.create_member.await_count, 2)
        with self.assertRaises(CollaborationError):
            self.service.create({**data, 'goal': 'different'})

    async def test_pause_during_dispatch_waits_for_admission_and_interrupts_only_owned_turn(self):
        admitted, release = asyncio.Event(), asyncio.Event()

        async def slow_send(member, prompt, readonly):
            admitted.set()
            await release.wait()
            return 'owned-turn'

        self.service.send = slow_send
        task = self.service.create(spec())
        await asyncio.wait_for(admitted.wait(), 1)
        pausing = asyncio.create_task(self.service.pause(task['id']))
        await asyncio.sleep(0)
        self.assertEqual(self.store.get(task['id'])['status'], 'paused')
        release.set()
        result = await asyncio.wait_for(pausing, 1)
        self.interrupt.assert_awaited_once_with(result['members']['coder'], 'owned-turn')
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['phase'], 'coder')

    async def test_pause_racing_with_completed_history_never_dispatches_review(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.outputs = [('completed', 'Ready\nDEVWISP_HANDOFF')]
        original_read = self.service.read

        async def slow_read(member):
            entered.set()
            await release.wait()
            return await original_read(member)

        self.service.read = slow_read
        task = self.service.create(spec())
        await asyncio.wait_for(entered.wait(), 1)
        pausing = asyncio.create_task(self.service.pause(task['id']))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(pausing, 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.get(task['id'])['status'], 'paused')

    async def test_restart_pauses_and_preserves_members_and_messages(self):
        self.outputs = [('completed', 'Need input')]
        task = await self.run_task()
        task['status'] = 'running'; self.store.save(task)
        recovered = CollaborationStore(self.db).detail(task['id'])
        self.assertEqual(recovered['status'], 'paused')
        self.assertEqual(recovered['members'], task['members'])
        self.assertIn('Need input', [entry['text'] for entry in recovered['events']])

    async def test_resume_interrupted_turn_restarts_current_stage_once(self):
        self.outputs = [('completed', 'Need input')]
        task = await self.run_task()
        task['activeTurn'] = {'id': 'old-turn', 'role': 'coder'}
        self.store.save(task)
        self.turns[task['members']['coder']['threadId']] = {'turns': [{'id': 'old-turn', 'status': 'interrupted'}]}
        self.outputs = [('completed', 'Done\nDEVWISP_HANDOFF'), ('completed', 'Clean\nDEVWISP_REVIEW: APPROVED')]
        await self.service.resume(task['id'])
        await self.service.resume(task['id'])
        await self.service._workers[task['id']]
        self.assertEqual(self.store.get(task['id'])['status'], 'completed')
        self.assertEqual(len(self.sent), 3)


if __name__ == '__main__':
    unittest.main()
