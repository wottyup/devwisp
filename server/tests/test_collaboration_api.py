from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from devwisp.app import TurnRequest, create_app
from devwisp.codex_app_server import CodexAppServer, CodexRuntimeError


async def request(app, method, path, body=None, authorized=True):
    messages = []
    delivered = False
    payload = json.dumps(body).encode() if body is not None else b''

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    headers = [(b'content-type', b'application/json')]
    if authorized:
        headers.append((b'authorization', b'Bearer fixture-token'))
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1',
             'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
             'root_path': '', 'headers': headers, 'client': ('127.0.0.1', 1234), 'server': ('test', 80)}
    await app(scope, receive, send)
    start = next(message for message in messages if message['type'] == 'http.response.start')
    raw = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    return start['status'], json.loads(raw) if raw else None


class CollaborationApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        with patch.dict(os.environ, {'DEVWISP_AUTH_FILE': str(self.root / 'auth.json'), 'DEVWISP_STATE_FILE': str(self.root / 'shared.sqlite3')}):
            self.app = create_app(self.root)
        self.app.state.auth.accepts = lambda token: token == 'fixture-token'
        self.addCleanup(self.app.state.shared_state.close)
        self.addAsyncCleanup(self.app.state.claude_code.close)
        self.addAsyncCleanup(self.app.state.deepseek.close)
        self.addAsyncCleanup(self.app.state.collaboration.close)
        self.claude, self.codex = self.app.state.claude_code, self.app.state.codex
        self.claude.create_thread = AsyncMock(return_value={'id': 'claude:fixture'})
        self.claude.start_turn = AsyncMock(return_value='coder-turn')
        self.claude.read_thread = AsyncMock(return_value={'turns': [{'id': 'coder-turn', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'text': 'Implemented and tested\nDEVWISP_HANDOFF'}]}]})
        self.codex.resume_thread = AsyncMock(return_value={})
        self.codex.start_thread = AsyncMock(return_value={'id': 'codex-fixture'})
        self.codex.start_turn = AsyncMock(return_value={'turn': {'id': 'review-turn'}})
        self.codex.read_thread = AsyncMock(return_value={'turns': [{'id': 'review-turn', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'text': 'Clean review\nDEVWISP_REVIEW: APPROVED'}]}]})
        self.body = {'requestId': str(uuid.uuid4()), 'title': 'Fix parser', 'goal': 'Fix and validate', 'cwd': str(self.root),
                     'coder': {'engine': 'claude-code', 'model': ''}, 'reviewer': {'engine': 'codex', 'model': ''}, 'maxRounds': 2}

    async def create(self):
        code, task = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual(code, 201, task)
        await self.app.state.collaboration._workers[task['id']]
        return task['id']

    async def test_authenticated_creation_runs_real_adapter_and_preserves_review_sandbox(self):
        task_id = await self.create()
        code, task = await request(self.app, 'GET', f'/api/v1/collaboration/tasks/{task_id}')
        self.assertEqual(code, 200)
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(self.claude.start_turn.call_args.kwargs['permission_mode'], 'default')
        self.assertEqual(self.codex.start_turn.call_args.kwargs['sandbox_policy'], {'type': 'readOnly'})
        self.assertIn('Implemented and tested', self.codex.start_turn.call_args.args[1])
        code, data = await request(self.app, 'GET', '/api/v1/collaboration/tasks')
        self.assertEqual(len(data['tasks']), 1)
        self.assertNotIn('spec', data['tasks'][0])
        self.assertNotIn('lastResult', data['tasks'][0])

    def existing(self, role):
        member = self.body[role]
        prefix = {'codex': '', 'claude-code': 'claude:', 'deepseek-harness': 'deepseek:'}[member['engine']]
        member['threadId'] = prefix + str(uuid.uuid4())
        native = {'codex': self.codex, 'claude-code': self.claude, 'deepseek-harness': self.app.state.deepseek}[member['engine']]
        native.read_thread.return_value.update(id=member['threadId'], cwd=str(self.root), status={'type': 'idle'})
        if member['engine'] == 'codex':
            native._active_turn_id = AsyncMock(return_value=None)
        else:
            native.active_turn_id = AsyncMock(return_value=None)
        return member['threadId']

    async def test_existing_chats_keep_native_ids_and_resume_context_without_creating_members(self):
        coder_id = self.existing('coder')
        reviewer_id = self.existing('reviewer')
        task_id = await self.create()
        task = self.app.state.collaboration.store.get(task_id)
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['members']['coder']['threadId'], coder_id)
        self.assertEqual(task['members']['reviewer']['threadId'], reviewer_id)
        self.claude.create_thread.assert_not_awaited()
        self.codex.start_thread.assert_not_awaited()
        self.assertEqual(self.claude.start_turn.call_args.args[0], coder_id)
        self.codex.resume_thread.assert_awaited_once_with(reviewer_id, cwd=self.root)
        self.assertEqual(self.codex.start_turn.call_args.kwargs['cwd'], self.root)
        self.assertEqual(self.codex.start_turn.call_args.kwargs['sandbox_policy'], {'type': 'readOnly'})
        # A lost HTTP response can be retried after members became owned/busy.
        self.claude.active_turn_id.return_value = 'another-turn'
        code, retry = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual((code, retry['id']), (201, task_id))
        self.assertEqual(self.claude.start_turn.await_count, 1)

    async def test_existing_implementer_can_work_with_a_new_reviewer(self):
        coder_id = self.existing('coder')
        task_id = await self.create()
        self.claude.create_thread.assert_not_awaited()
        self.codex.start_thread.assert_awaited_once()
        self.assertEqual(self.app.state.collaboration.store.owner(coder_id), task_id)

    async def test_busy_existing_chat_is_rejected_without_starting_a_task(self):
        self.existing('coder')
        for active, live_status, tail_status in [(None, 'active', 'completed'), ('live-turn', 'idle', 'completed'),
                                                  (None, 'idle', 'inProgress'), (None, 'waiting', 'completed')]:
            self.claude.active_turn_id.return_value = active
            self.claude.read_thread.return_value['status'] = {'type': live_status}
            self.claude.read_thread.return_value['turns'][0]['status'] = tail_status
            code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
            self.assertEqual(code, 409)
        self.assertEqual(self.app.state.collaboration.store.list(), [])
        self.claude.start_turn.assert_not_awaited()
        self.codex.start_thread.assert_not_awaited()

    async def test_queued_chat_is_rejected_and_pending_message_is_untouched(self):
        thread_id = self.existing('coder')
        payload = {'id': str(uuid.uuid4()), 'text': 'Keep this scheduled message', 'scheduledAt': '2099-01-01T00:00:00Z'}
        self.app.state.shared_state.enqueue(thread_id, payload)
        before = self.app.state.shared_state.queue(thread_id)
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual(code, 409)
        self.assertEqual(self.app.state.shared_state.queue(thread_id), before)
        self.claude.start_turn.assert_not_awaited()

    async def test_unconfirmed_barrier_is_rejected_but_completed_barrier_can_join(self):
        thread_id = self.existing('coder')
        self.app.state.shared_state.set_barrier(thread_id, 'missing-turn')
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual(code, 409)
        self.app.state.shared_state.set_barrier(thread_id, 'coder-turn')
        await self.create()

    async def test_wrong_workspace_or_engine_and_missing_chat_leave_no_ownership(self):
        thread_id = self.existing('coder')
        self.claude.read_thread.return_value['cwd'] = str(self.root / 'other')
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual(code, 409)
        self.claude.read_thread.return_value['cwd'] = str(self.root)
        self.body['coder']['engine'] = 'codex'
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual(code, 409)
        self.body['coder']['engine'] = 'claude-code'
        self.claude.read_thread.side_effect = CodexRuntimeError('missing chat')
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual(code, 409)
        self.assertIsNone(self.app.state.collaboration.store.owner(thread_id))
        self.assertEqual(self.app.state.collaboration.store.list(), [])

    async def test_same_thread_for_both_roles_and_already_owned_member_are_rejected(self):
        thread_id = self.existing('coder')
        self.body['reviewer'] = dict(self.body['coder'])
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual(code, 409)
        self.body['reviewer'] = {'engine': 'codex', 'model': ''}
        task_id = await self.create()
        self.body['requestId'] = str(uuid.uuid4())
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual(code, 409)
        self.assertEqual(self.app.state.collaboration.store.owner(thread_id), task_id)
        self.assertEqual(len(self.app.state.collaboration.store.list()), 1)

    async def test_manual_request_waiting_during_adoption_cannot_send_after_ownership_changes(self):
        thread_id = self.existing('coder')
        validating, finish_validation = asyncio.Event(), asyncio.Event()
        original_read = self.claude.read_thread

        async def delayed_read(*args, **kwargs):
            validating.set()
            await finish_validation.wait()
            return await original_read(*args, **kwargs)

        self.claude.read_thread = delayed_read
        adoption = asyncio.create_task(request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body))
        await validating.wait()
        manual = asyncio.create_task(request(self.app, 'POST', f'/api/v1/codex/threads/{thread_id}/turns', {'prompt': 'Do not send'}))
        await asyncio.sleep(0)
        self.assertFalse(manual.done())
        finish_validation.set()
        code, task = await adoption
        self.assertEqual(code, 201)
        code, _ = await manual
        self.assertEqual(code, 409)
        await self.app.state.collaboration._workers[task['id']]
        self.assertEqual(self.claude.start_turn.await_count, 1)
        self.assertNotEqual(self.claude.start_turn.call_args.args[1], 'Do not send')

    async def test_delivery_in_progress_finishes_before_adoption_can_check_busy_state(self):
        thread_id = self.existing('coder')
        lock = self.app.state.shared_queue.delivery_lock(thread_id)
        await lock.acquire()
        try:
            adoption = asyncio.create_task(request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body))
            await asyncio.sleep(0)
            self.claude.active_turn_id.return_value = 'just-dispatched'
        finally:
            lock.release()
        code, _ = await adoption
        self.assertEqual(code, 409)
        self.assertIsNone(self.app.state.collaboration.store.owner(thread_id))

    async def test_two_tasks_cannot_concurrently_adopt_the_same_chat(self):
        thread_id = self.existing('coder')
        responses = await asyncio.gather(
            request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body),
            request(self.app, 'POST', '/api/v1/collaboration/tasks', {**self.body, 'requestId': str(uuid.uuid4())}),
        )
        self.assertEqual(sorted(code for code, _ in responses), [201, 409])
        task = next(task for code, task in responses if code == 201)
        await self.app.state.collaboration._workers[task['id']]
        self.assertEqual(self.app.state.collaboration.store.owner(thread_id), task['id'])
        self.assertEqual(self.claude.start_turn.await_count, 1)

    async def test_codex_bound_workspace_is_used_to_resume_existing_context(self):
        thread_id = self.existing('reviewer')
        self.codex.read_thread.return_value['cwd'] = str(self.root / 'old-location')
        self.app.state.shared_state.set_thread_workspace(thread_id, str(self.root))
        await self.create()
        self.codex.resume_thread.assert_awaited_once_with(thread_id, cwd=self.root)

    async def test_terminal_control_cannot_be_enabled_on_an_adopted_member(self):
        thread_id = self.existing('coder')
        await self.create()
        code, _ = await request(self.app, 'POST', f'/api/v1/codex/terminal/handoff/{thread_id}', {'enabled': True})
        self.assertEqual(code, 409)

    async def test_orphaned_legacy_send_callback_rechecks_ownership_inside_delivery_lock(self):
        thread_id = self.existing('coder')
        await self.create()
        endpoint = next(route.endpoint for route in self.app.routes if getattr(route, 'path', '') == '/api/v1/codex/threads/{thread_id}/turns')
        # Simulate a shielded mutation whose HTTP admission check happened
        # before adoption, but whose callback runs after the requester left.
        with self.assertRaises(HTTPException) as error:
            await endpoint(thread_id, TurnRequest(prompt='Late legacy send', request_id='late-send', direct=False))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.claude.start_turn.await_count, 1)

    async def test_ordinary_codex_chat_can_submit_after_collaboration_is_installed(self):
        self.codex._active_turn_id = AsyncMock(return_value=None)
        self.codex.read_thread.return_value.update(id='ordinary-chat', cwd=str(self.root), status={'type': 'idle'})
        payload = {'id': str(uuid.uuid4()), 'text': 'Hello', 'requestText': 'Hello',
                   'createdAt': '2026-11-20T00:00:00Z', 'images': [], 'documents': []}

        code, result = await asyncio.wait_for(
            request(self.app, 'POST', '/api/v1/codex/queue/ordinary-chat/submit', payload), timeout=2,
        )
        retry_code, retry = await asyncio.wait_for(
            request(self.app, 'POST', '/api/v1/codex/queue/ordinary-chat/submit', payload), timeout=2,
        )

        self.assertEqual((code, result), (200, {'sent': True, 'queue': []}))
        self.assertEqual((retry_code, retry), (code, result))
        self.codex.start_turn.assert_awaited_once()
        self.assertTrue(self.app.state.shared_state.was_delivered('ordinary-chat', payload['id']))

    async def test_existing_deepseek_chat_continues_in_workspace_write_mode(self):
        deepseek = self.app.state.deepseek
        deepseek.read_thread = AsyncMock(return_value={'turns': [{'id': 'harness-turn', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'text': 'Implemented with prior context\nDEVWISP_HANDOFF'}]}]})
        deepseek.start_turn = AsyncMock(return_value='harness-turn')
        deepseek.create_thread = AsyncMock()
        self.body['coder'] = {'engine': 'deepseek-harness', 'model': 'existing-override'}
        thread_id = self.existing('coder')
        await self.create()
        deepseek.create_thread.assert_not_awaited()
        self.assertEqual(deepseek.start_turn.call_args.args[0], thread_id)
        self.assertEqual(deepseek.start_turn.call_args.kwargs['deepseek_permission_mode'], 'workspace-write')
        self.assertEqual(deepseek.start_turn.call_args.kwargs['model'], 'existing-override')

    async def make_group(self, **fields):
        body = {'requestId': str(uuid.uuid4()), **fields}
        code, group = await request(self.app, 'POST', '/api/v1/collaboration/groups', body)
        self.assertEqual(code, 201, group)
        return group, body

    async def group_with_codex(self):
        group, _ = await self.make_group()
        code, group = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members',
                                    {'requestId': str(uuid.uuid4()), 'engine': 'codex'})
        self.assertEqual(code, 201)
        return group

    def group_payload(self, text='Group request', **fields):
        return {'id': str(uuid.uuid4()), 'text': text, 'requestText': text,
                'images': [], 'documents': [], 'createdAt': '2026-11-20T00:00:00Z', **fields}

    async def test_group_shared_composer_queue_delivers_mixed_engines_attachments_and_settings_once(self):
        group = await self.group_with_codex()
        for engine in ('claude-code', 'deepseek-harness'):
            code, _ = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members',
                                    {'requestId': str(uuid.uuid4()), 'engine': engine})
            self.assertEqual(code, 201)
        deepseek = self.app.state.deepseek
        deepseek.create_thread = AsyncMock(return_value={'id': 'deepseek:' + str(uuid.uuid4())})
        deepseek.start_turn = AsyncMock(return_value='harness-turn')
        deepseek.read_thread = AsyncMock(return_value={'turns': [{'id': 'harness-turn', 'status': 'completed',
            'items': [{'type': 'agentMessage', 'text': 'Harness reply'}]}]})
        image = {'name': 'tiny.png', 'mediaType': 'image/png',
                 'data': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP0YAAAAASUVORK5CYII='}
        original = self.app.state.pdf_attachments.save('original.pdf', b'%PDF-1.7\nFixture original document')
        payload = self.group_payload(images=[image], documents=[{'name': 'notes.txt', 'content': 'Shared document'},
            {'name': 'original.pdf', 'content': '', 'sourceFileId': original.attachment_id}],
            requestText='Group request\nShared document', collaborationSettings={
                'codex': {'model': 'gpt-fixture', 'effort': 'high', 'approvalPolicy': 'on-request'},
                'claude-code': {'model': 'claude-fixture', 'effort': 'medium', 'claudePermissionMode': 'acceptEdits'},
                'deepseek-harness': {'model': 'deepseek-fixture', 'effort': 'high', 'deepseekPermissionMode': 'read-only'},
            })
        path = f'/api/v1/codex/queue/collaboration:{group["id"]}/submit'
        for _ in range(2):
            code, result = await asyncio.wait_for(request(self.app, 'POST', path, payload), timeout=3)
            self.assertEqual((code, result), (200, {'sent': True, 'queue': []}))
        try:
            await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], timeout=3)
        except TimeoutError:
            self.fail(f"Group did not finish: {self.app.state.collaboration.store.detail(group['id'])}")
        self.codex.start_turn.assert_awaited_once()
        self.assertEqual(self.codex.start_turn.call_args.kwargs['model'], 'gpt-fixture')
        self.assertEqual(self.codex.start_turn.call_args.kwargs['effort'], 'high')
        self.assertEqual(len(self.codex.start_turn.call_args.args[2]), 1)
        self.assertEqual(self.claude.start_turn.call_args.kwargs['model'], 'claude-fixture')
        self.assertEqual(self.claude.start_turn.call_args.kwargs['permission_mode'], 'plan')
        self.assertEqual(self.codex.start_turn.call_args.kwargs['sandbox_policy'], {'type': 'readOnly'})
        self.assertEqual(self.codex.start_turn.call_args.kwargs['approval_policy'], 'untrusted')
        self.assertEqual(deepseek.start_turn.call_args.kwargs['deepseek_permission_mode'], 'read-only')
        self.assertTrue(deepseek.start_turn.call_args.kwargs['image_paths'])
        self.assertIn(str(original.path), self.codex.start_turn.call_args.args[1])
        self.assertEqual(self.claude.start_turn.call_args.kwargs['attachment_paths'][0].read_bytes(), original.path.read_bytes())
        self.assertEqual(deepseek.start_turn.call_args.kwargs['attachment_paths'][0].read_bytes(), original.path.read_bytes())
        self.assertIn('Shared document', self.claude.start_turn.call_args.args[1])
        event = self.app.state.collaboration.store.events(group['id'])[0]
        self.assertEqual(event['images'], [image])
        self.assertEqual(event['documents'][0]['name'], 'notes.txt')

    async def test_group_busy_queue_advances_after_round_and_preserves_edit_lock(self):
        group = await self.group_with_codex()
        service = self.app.state.collaboration
        started, release = asyncio.Event(), asyncio.Event()
        original_read = self.codex.read_thread
        async def blocked(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_read(*args, **kwargs)
        self.codex.read_thread = AsyncMock(side_effect=blocked)
        queue_id = f'collaboration:{group["id"]}'
        path = f'/api/v1/codex/queue/{queue_id}'
        first, second = self.group_payload('First'), self.group_payload('Second')
        code, result = await asyncio.wait_for(request(self.app, 'POST', path + '/submit', first), timeout=2)
        self.assertEqual((code, result['sent']), (200, True))
        await asyncio.wait_for(started.wait(), timeout=2)
        code, result = await asyncio.wait_for(request(self.app, 'POST', path + '/submit', second), timeout=2)
        self.assertEqual((code, result['sent']), (200, False))
        code, _ = await request(self.app, 'POST', path + '/edit/begin', {'message_id': second['id'], 'owner_id': 'fixture-owner'})
        self.assertEqual(code, 200)
        release.set()
        await service._workers[group['id']]
        await self.app.state.shared_queue.advance(queue_id)
        self.assertEqual(self.codex.start_turn.await_count, 1)
        await request(self.app, 'POST', path + '/edit/end', {'message_id': second['id'], 'owner_id': 'fixture-owner'})
        await self.app.state.shared_queue.advance(queue_id)
        await service._workers[group['id']]
        self.assertEqual(self.codex.start_turn.await_count, 2)
        self.assertEqual(self.app.state.shared_state.queue(queue_id), [])

    async def test_group_schedule_and_dissolve_remove_only_group_queue(self):
        from datetime import datetime, timedelta, timezone
        group = await self.group_with_codex()
        queue_id = f'collaboration:{group["id"]}'
        scheduled = self.group_payload(scheduledAt=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        code, _ = await request(self.app, 'POST', f'/api/v1/codex/queue/{queue_id}/schedule', scheduled)
        self.assertEqual(code, 200)
        await self.app.state.shared_queue.advance(queue_id)
        self.codex.start_turn.assert_not_awaited()
        self.app.state.shared_state.enqueue('ordinary', self.group_payload())
        code, _ = await request(self.app, 'DELETE', f'/api/v1/collaboration/tasks/{group["id"]}')
        self.assertEqual(code, 200)
        self.assertEqual(self.app.state.shared_state.queue(queue_id), [])
        self.assertEqual(len(self.app.state.shared_state.queue('ordinary')), 1)

    async def test_running_group_does_not_block_unrelated_single_chat(self):
        group = await self.group_with_codex()
        started, release = asyncio.Event(), asyncio.Event()
        original = self.codex.read_thread.return_value
        async def read(thread_id, **kwargs):
            if thread_id == 'ordinary-chat':
                return {'id': thread_id, 'status': {'type': 'idle'}, 'turns': []}
            started.set()
            await release.wait()
            return original
        self.codex.read_thread = AsyncMock(side_effect=read)
        self.codex._active_turn_id = AsyncMock(return_value=None)
        await request(self.app, 'POST', f'/api/v1/codex/queue/collaboration:{group["id"]}/submit', self.group_payload())
        try:
            await asyncio.wait_for(started.wait(), 2)
            code, result = await asyncio.wait_for(request(self.app, 'POST', '/api/v1/codex/queue/ordinary-chat/submit', self.group_payload('Independent')), 2)
            self.assertEqual((code, result['sent']), (200, True))
            self.assertEqual(self.codex.start_turn.call_args.args[:2], ('ordinary-chat', 'Independent'))
        finally:
            release.set()
            await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)

    async def test_group_new_input_waits_for_complete_parallel_round_even_with_send_now(self):
        group = await self.group_with_codex()
        await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members',
                      {'requestId': str(uuid.uuid4()), 'engine': 'claude-code'})
        started, release = asyncio.Event(), asyncio.Event()
        original = self.codex.read_thread.return_value
        async def read(*args, **kwargs):
            started.set()
            await release.wait()
            return original
        self.codex.read_thread = AsyncMock(side_effect=read)
        self.codex.steer_turn = AsyncMock(return_value={'turnId': 'review-turn'})
        path = f'/api/v1/codex/queue/collaboration:{group["id"]}'
        await request(self.app, 'POST', path + '/submit', self.group_payload('First'))
        await asyncio.wait_for(started.wait(), 2)
        try:
            self.codex.resume_thread.reset_mock()
            payload = self.group_payload('Additional request', documents=[{'name': 'extra.md', 'content': 'Extra context'}],
                                         requestText='Additional request\nExtra context')
            code, queued = await request(self.app, 'POST', path + '/submit', payload)
            self.assertEqual((code, queued['sent']), (200, False))
            for _ in range(2):
                code, sent = await asyncio.wait_for(request(self.app, 'POST', path + '/send-now', {'message_id': payload['id']}), 2)
                self.assertEqual((code, sent['sent']), (200, False))
                self.assertIn('本轮讨论与开发结束', sent['queue'][0]['blockedReason'])
            self.codex.steer_turn.assert_not_awaited()
            self.codex.resume_thread.assert_not_awaited()
        finally:
            release.set()
            await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        self.assertNotIn('Additional request', self.claude.start_turn.call_args.args[1])
        await self.app.state.shared_queue.advance('collaboration:' + group['id'])
        await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        self.assertIn('Additional request\nExtra context', self.claude.start_turn.call_args.args[1])
        events = self.app.state.collaboration.store.events(group['id'])
        self.assertEqual(len([event for event in events if event['role'] == 'human']), 2)
        self.assertEqual(next(event for event in events if event['role'] == 'human' and event['text'] == 'Additional request')['documents'][0]['name'], 'extra.md')

    async def test_group_pause_retains_queue_and_resume_advances_after_current_round(self):
        group = await self.group_with_codex()
        path = f'/api/v1/codex/queue/collaboration:{group["id"]}'
        service, queue = self.app.state.collaboration, self.app.state.shared_queue
        started, release = asyncio.Event(), asyncio.Event()
        original = self.codex.read_thread.return_value
        async def read(*args, **kwargs):
            started.set()
            await release.wait()
            return original
        self.codex.read_thread = AsyncMock(side_effect=read)
        self.codex.interrupt = AsyncMock()
        await request(self.app, 'POST', path + '/submit', self.group_payload('First'))
        await asyncio.wait_for(started.wait(), 2)
        await request(self.app, 'POST', path + '/submit', self.group_payload('Second'))
        pause = asyncio.create_task(request(self.app, 'POST', f'/api/v1/collaboration/tasks/{group["id"]}/control/pause'))
        for _ in range(100):
            if service.group(group['id'])['status'] == 'paused':
                break
            await asyncio.sleep(.01)
        release.set()
        code, paused = await asyncio.wait_for(pause, 2)
        self.assertEqual((code, paused['status']), (200, 'paused'))
        await queue.advance('collaboration:' + group['id'])
        self.assertEqual(self.codex.start_turn.await_count, 1)
        code, _ = await request(self.app, 'POST', f'/api/v1/collaboration/tasks/{group["id"]}/control/resume')
        self.assertEqual(code, 200)
        await asyncio.wait_for(service._workers[group['id']], 2)
        await queue.advance('collaboration:' + group['id'])
        await asyncio.wait_for(service._workers[group['id']], 2)
        self.assertEqual(self.codex.start_turn.await_count, 2)

    async def test_group_background_translation_preserves_attachment_and_engine_settings(self):
        group = await self.group_with_codex()
        queue_id = 'collaboration:' + group['id']
        queue = self.app.state.shared_queue
        queue.translate = AsyncMock(return_value='Translated request')
        payload = self.group_payload('中文要求', translationStatus='pending', translation={'provider': 'microsoft', 'style': 'natural'},
            documents=[{'name': 'extra.md', 'content': 'Document text'}], collaborationSettings={'codex': {'model': 'snapshot-model'}})
        code, _ = await request(self.app, 'POST', f'/api/v1/codex/queue/{queue_id}/enqueue-translating', payload)
        self.assertEqual(code, 200)
        await queue.advance(queue_id)
        self.codex.start_turn.assert_not_awaited()
        queue._schedule_pending_translations(queue_id)
        await asyncio.gather(*queue._translation_tasks.values())
        await queue.advance(queue_id)
        await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        self.assertEqual(self.codex.start_turn.call_args.kwargs['model'], 'snapshot-model')
        self.assertIn('Translated request', self.codex.start_turn.call_args.args[1])
        self.assertIn('Document text', self.codex.start_turn.call_args.args[1])
        self.assertEqual(self.app.state.collaboration.store.events(group['id'])[0]['text'], '中文要求')

    async def mentioned_group(self):
        group = await self.group_with_codex()
        code, group = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members',
                                    {'requestId': str(uuid.uuid4()), 'engine': 'claude-code'})
        self.assertEqual(code, 201)
        claude_id = next(key for key, member in group['members'].items() if member['engine'] == 'claude-code')
        return group, claude_id, f'/api/v1/codex/queue/collaboration:{group["id"]}'

    async def test_translated_mention_routes_only_named_member_and_retry_is_idempotent(self):
        group, claude_id, path = await self.mentioned_group()
        payload = self.group_payload('Translated request', originalText='@Claude 1 只让你回答',
            requestText='Translated request\nDocument says @Codex 1', documents=[{'name': 'doc.txt', 'content': '@Codex 1'}])
        for _ in range(2):
            code, sent = await request(self.app, 'POST', path + '/submit', payload)
            self.assertEqual((code, sent['sent']), (200, True))
        await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        self.codex.start_turn.assert_not_awaited()
        self.claude.start_turn.assert_awaited_once()
        self.assertEqual(self.app.state.collaboration.store.events(group['id'])[0]['text'], payload['originalText'])

    async def test_unknown_mention_fails_before_enqueue_and_client_cannot_override_targets(self):
        group, claude_id, path = await self.mentioned_group()
        code, error = await request(self.app, 'POST', path + '/submit', self.group_payload('@Codex 99 hello'))
        self.assertEqual(code, 422)
        self.assertEqual(self.app.state.shared_state.queue('collaboration:' + group['id']), [])
        self.codex.start_turn.assert_not_awaited()
        code, sent = await request(self.app, 'POST', path + '/submit', self.group_payload('@Claude hello', collaborationRecipientIds=list(group['members'])))
        self.assertEqual((code, sent['sent']), (200, True))
        await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        self.codex.start_turn.assert_not_awaited()
        self.claude.start_turn.assert_awaited_once()

    async def test_busy_direct_mention_never_steers_wrong_member_and_runs_separately(self):
        group, claude_id, path = await self.mentioned_group()
        started, release = asyncio.Event(), asyncio.Event()
        original = self.codex.read_thread.return_value
        async def read(*args, **kwargs):
            started.set()
            await release.wait()
            return original
        self.codex.read_thread = AsyncMock(side_effect=read)
        self.codex.steer_turn = AsyncMock()
        await request(self.app, 'POST', path + '/submit', self.group_payload('Everyone first'))
        await asyncio.wait_for(started.wait(), 2)
        try:
            payload = self.group_payload('@Claude 1 Only you next')
            code, result = await request(self.app, 'POST', path + '/submit', payload)
            self.assertEqual((code, result['sent']), (200, False))
            self.assertEqual(result['queue'][0]['collaborationRecipientIds'], [claude_id])
            code, result = await request(self.app, 'POST', path + '/send-now', {'message_id': payload['id']})
            self.assertEqual((code, result['sent']), (200, False))
            self.assertNotIn('sendAttempt', result['queue'][0])
            self.assertIn('点名消息', result['queue'][0]['blockedReason'])
            self.codex.steer_turn.assert_not_awaited()
        finally:
            release.set()
            await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        await self.app.state.shared_queue.advance('collaboration:' + group['id'])
        await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        self.assertEqual(self.codex.start_turn.await_count, 1)
        self.assertEqual(self.claude.start_turn.await_count, 2)

    async def test_scheduled_mention_retains_recipient_id_and_removed_member_blocks_without_claim(self):
        from datetime import datetime, timedelta, timezone
        group, claude_id, path = await self.mentioned_group()
        payload = self.group_payload('@Claude hello', scheduledAt=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        code, result = await request(self.app, 'POST', path + '/schedule', payload)
        self.assertEqual(code, 200)
        self.assertEqual(result['queue'][0]['collaborationRecipientIds'], [claude_id])
        await request(self.app, 'DELETE', f'/api/v1/collaboration/groups/{group["id"]}/members/{claude_id}')
        with patch('devwisp.shared_state.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=2)
            await self.app.state.shared_queue.advance('collaboration:' + group['id'])
        pending = self.app.state.shared_state.queue('collaboration:' + group['id'])[0]
        self.assertEqual(pending['deliveryState'], 'pending')
        self.assertIn('已不在群里', pending['blockedReason'])
        code, result = await request(self.app, 'POST', path + '/send-now', {'message_id': payload['id']})
        self.assertEqual((code, result['sent']), (200, False))
        self.assertEqual(result['queue'][0]['deliveryState'], 'pending')
        self.assertIn('已不在群里', result['queue'][0]['blockedReason'])
        self.codex.start_turn.assert_not_awaited()
        self.claude.start_turn.assert_not_awaited()

    async def test_scheduled_alias_keeps_original_member_after_same_engine_member_joins(self):
        from datetime import datetime, timedelta, timezone
        group, claude_id, path = await self.mentioned_group()
        payload = self.group_payload('@Claude hello', scheduledAt=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        code, _ = await request(self.app, 'POST', path + '/schedule', payload)
        self.assertEqual(code, 200)
        code, updated = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members',
                                      {'requestId': str(uuid.uuid4()), 'engine': 'claude-code'})
        self.assertEqual((code, len(updated['members'])), (201, 3))
        with patch('devwisp.shared_state.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=2)
            await self.app.state.shared_queue.advance('collaboration:' + group['id'])
        await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        self.claude.start_turn.assert_awaited_once()
        self.assertEqual(self.app.state.collaboration.store.events(group['id'])[-1]['role'], claude_id)
        self.codex.start_turn.assert_not_awaited()

    async def test_edit_and_background_translation_preserve_correct_mention_target(self):
        group, claude_id, path = await self.mentioned_group()
        queue_id = 'collaboration:' + group['id']
        payload = self.group_payload('@Codex 1 Original', translationStatus='pending', translation={'provider': 'microsoft', 'style': 'natural'})
        await request(self.app, 'POST', path + '/enqueue-translating', payload)
        await request(self.app, 'POST', path + '/edit/begin', {'message_id': payload['id'], 'owner_id': 'fixture-owner'})
        updated = {**payload, 'text': '@Claude 1 修改后', 'requestText': '@Claude 1 修改后'}
        code, result = await request(self.app, 'POST', path + '/edit/update', updated)
        self.assertEqual(code, 200)
        self.assertEqual(result['queue'][0]['collaborationRecipientIds'], [claude_id])
        await request(self.app, 'POST', path + '/edit/end', {'message_id': payload['id'], 'owner_id': 'fixture-owner'})
        queue = self.app.state.shared_queue
        queue.translate = AsyncMock(return_value='Translated without any mention')
        queue._schedule_pending_translations(queue_id)
        await asyncio.gather(*queue._translation_tasks.values())
        await queue.advance(queue_id)
        await asyncio.wait_for(self.app.state.collaboration._workers[group['id']], 2)
        self.codex.start_turn.assert_not_awaited()
        self.claude.start_turn.assert_awaited_once()

    async def test_dissolve_requires_auth_and_releases_native_chat_without_deleting_history(self):
        thread_id = self.existing('coder')
        self.claude.delete_thread = AsyncMock()
        self.codex.delete_thread = AsyncMock()
        self.codex.notify_threads_changed = AsyncMock()
        group, _ = await self.make_group(member=self.body['coder'])
        service = self.app.state.collaboration
        path = f'/api/v1/collaboration/tasks/{group["id"]}'
        code, _ = await request(self.app, 'DELETE', path, authorized=False)
        self.assertEqual(code, 401)
        self.assertEqual(service.store.owner(thread_id), group['id'])
        self.codex.notify_threads_changed.reset_mock()
        code, data = await request(self.app, 'DELETE', path)
        self.assertEqual((code, data), (200, {'ok': True}))
        self.codex.notify_threads_changed.assert_awaited_once()
        self.assertIsNone(service.store.owner(thread_id))
        self.assertEqual((await request(self.app, 'GET', path))[0], 404)
        self.assertEqual((await request(self.app, 'GET', '/api/v1/collaboration/tasks'))[1]['tasks'], [])
        self.assertEqual((await request(self.app, 'DELETE', path))[0], 200)
        self.claude.delete_thread.assert_not_awaited()
        self.codex.delete_thread.assert_not_awaited()
        code, native = await request(self.app, 'GET', f'/api/v1/codex/threads/{thread_id}')
        self.assertEqual(code, 200, native)
        code, result = await asyncio.wait_for(request(self.app, 'POST', f'/api/v1/codex/threads/{thread_id}/turns',
                                                     {'prompt': 'Continue as a single chat', 'request_id': 'after-dissolve', 'direct': True}), 2)
        self.assertEqual(code, 200, result)
        self.claude.start_turn.assert_awaited_once()
        # Adoption works immediately after dissolution, using the same history.
        new_group, _ = await self.make_group(member=self.body['coder'])
        self.assertEqual(service.store.owner(thread_id), new_group['id'])

    async def test_dissolve_legacy_task_and_invalid_id(self):
        task_id = await self.create()
        ids = [member['threadId'] for member in self.app.state.collaboration.store.get(task_id)['members'].values()]
        code, data = await request(self.app, 'DELETE', f'/api/v1/collaboration/tasks/{task_id}')
        self.assertEqual((code, data), (200, {'ok': True}))
        self.assertTrue(all(self.app.state.collaboration.store.owner(thread_id) is None for thread_id in ids))
        self.assertEqual((await request(self.app, 'DELETE', '/api/v1/collaboration/tasks/invalid'))[0], 422)

    async def test_dissolve_timeout_is_retryable_and_keeps_members_owned(self):
        from devwisp.collaboration import CollaborationError

        thread_id = self.existing('coder')
        group, _ = await self.make_group(member=self.body['coder'])
        service = self.app.state.collaboration
        path = f'/api/v1/collaboration/tasks/{group["id"]}'
        with patch.object(service, 'dissolve', AsyncMock(side_effect=CollaborationError('请稍后重试解散。'))):
            code, data = await request(self.app, 'DELETE', path)
        self.assertEqual(code, 409, data)
        self.assertEqual(service.store.owner(thread_id), group['id'])
        self.assertEqual((await request(self.app, 'DELETE', path))[0], 200)

    async def test_rename_same_named_groups_independently_and_persist_in_reads(self):
        first, _ = await self.make_group()
        second, _ = await self.make_group()
        self.assertEqual(first['title'], second['title'])
        path = f'/api/v1/collaboration/tasks/{first["id"]}/name'
        for _ in range(2):
            code, renamed = await request(self.app, 'PUT', path, {'name': '  登录修复   讨论组  '})
            self.assertEqual((code, renamed['title']), (200, '登录修复 讨论组'))
        code, detail = await request(self.app, 'GET', f'/api/v1/collaboration/tasks/{first["id"]}')
        self.assertEqual((code, detail['title'], detail['status']), (200, '登录修复 讨论组', 'idle'))
        _, result = await request(self.app, 'GET', '/api/v1/collaboration/tasks')
        names = {task['id']: task['title'] for task in result['tasks']}
        self.assertEqual(names, {first['id']: '登录修复 讨论组', second['id']: second['title']})
        self.codex.start_turn.assert_not_awaited()
        self.claude.start_turn.assert_not_awaited()

    async def test_group_rename_validates_names_authentication_and_missing_id(self):
        group, _ = await self.make_group()
        path = f'/api/v1/collaboration/tasks/{group["id"]}/name'
        for invalid in ('', '  ', '\x00', '\x7f', 'a' * 73):
            with self.subTest(invalid=repr(invalid)):
                code, _ = await request(self.app, 'PUT', path, {'name': invalid})
                self.assertEqual(code, 422)
        code, _ = await request(self.app, 'PUT', path, {'name': 'Unauthorized'}, authorized=False)
        self.assertEqual(code, 401)
        code, _ = await request(self.app, 'PUT', f'/api/v1/collaboration/tasks/{uuid.uuid4()}/name', {'name': 'Missing'})
        self.assertEqual(code, 404)
        self.assertEqual(self.app.state.collaboration.store.get(group['id'])['title'], group['title'])

    async def test_legacy_collaboration_rename_retains_completed_state_and_members(self):
        task_id = await self.create()
        before = self.app.state.collaboration.store.get(task_id)
        code, renamed = await request(self.app, 'PUT', f'/api/v1/collaboration/tasks/{task_id}/name', {'name': '已完成的登录审查'})
        self.assertEqual((code, renamed['title']), (200, '已完成的登录审查'))
        self.assertEqual((renamed['status'], renamed['members'], renamed['goal']),
                         (before['status'], before['members'], before['goal']))

    async def test_group_empty_creation_is_idempotent_and_does_not_call_engines(self):
        group, body = await self.make_group()
        self.assertEqual(group['members'], {})
        self.assertEqual(group['status'], 'idle')
        code, repeated = await request(self.app, 'POST', '/api/v1/collaboration/groups', body)
        self.assertEqual((code, repeated['id']), (201, group['id']))
        self.claude.create_thread.assert_not_awaited()
        self.codex.start_thread.assert_not_awaited()
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/groups', body, authorized=False)
        self.assertEqual(code, 401)

    async def test_group_can_adopt_chat_directly_on_creation_and_preserve_context(self):
        thread_id = self.existing('coder')
        group, body = await self.make_group(member=self.body['coder'])
        self.assertEqual(next(iter(group['members'].values()))['threadId'], thread_id)
        self.assertEqual(self.app.state.collaboration.store.owner(thread_id), group['id'])
        code, repeated = await request(self.app, 'POST', '/api/v1/collaboration/groups', body)
        self.assertEqual((code, repeated['id']), (201, group['id']))
        code, _ = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/messages', {'requestId': str(uuid.uuid4()), 'text': 'Check with prior context'})
        self.assertEqual(code, 200)
        await self.app.state.collaboration._workers[group['id']]
        self.assertEqual(self.claude.start_turn.call_args.args[0], thread_id)
        self.claude.create_thread.assert_not_awaited()
        self.assertEqual(self.app.state.collaboration.store.get(group['id'])['status'], 'idle')

    async def test_group_add_many_same_engine_without_creating_sessions_and_remove(self):
        group, _ = await self.make_group()
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for member_id in ids:
            body = {'requestId': member_id, 'engine': 'codex'}
            code, current = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members', body)
            self.assertEqual(code, 201, current)
        self.assertEqual([member['name'] for member in current['members'].values()], ['Codex 1', 'Codex 2', 'Codex 3'])
        self.codex.start_thread.assert_not_awaited()
        code, current = await request(self.app, 'DELETE', f'/api/v1/collaboration/groups/{group["id"]}/members/{ids[1]}')
        self.assertEqual(code, 200)
        self.assertEqual(len(current['members']), 2)

    async def test_group_admission_rejects_busy_queued_owned_and_wrong_project_chats(self):
        thread_id = self.existing('coder')
        group, _ = await self.make_group()
        body = {'requestId': str(uuid.uuid4()), **self.body['coder']}
        path = f'/api/v1/collaboration/groups/{group["id"]}/members'
        self.claude.active_turn_id.return_value = 'busy'
        code, _ = await request(self.app, 'POST', path, body)
        self.assertEqual(code, 409)
        self.claude.active_turn_id.return_value = None
        self.app.state.shared_state.enqueue(thread_id, {'id': 'pending', 'text': 'Do not lose'})
        code, _ = await request(self.app, 'POST', path, body)
        self.assertEqual(code, 409)
        self.app.state.shared_state.remove(thread_id, 'pending')
        code, _ = await request(self.app, 'POST', path, body)
        self.assertEqual(code, 201)
        other, _ = await self.make_group()
        code, _ = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{other["id"]}/members', {**body, 'requestId': str(uuid.uuid4())})
        self.assertEqual(code, 409)
        code, _ = await request(self.app, 'POST', f'/api/v1/codex/threads/{thread_id}/turns', {'prompt': 'Manual send'})
        self.assertEqual(code, 409)

    async def test_group_first_existing_chat_defines_project_and_later_mismatch_is_rejected(self):
        thread_id = self.existing('coder')
        project = self.root / 'project'
        project.mkdir()
        self.claude.read_thread.return_value['cwd'] = str(project)
        group, _ = await self.make_group()
        body = {'requestId': str(uuid.uuid4()), **self.body['coder']}
        code, group = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members', body)
        self.assertEqual(code, 201)
        self.assertEqual(group['cwd'], str(project))
        self.existing('reviewer')
        code, _ = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members', {'requestId': str(uuid.uuid4()), **self.body['reviewer']})
        self.assertEqual(code, 409)
        member_id = next(iter(group['members']))
        code, _ = await request(self.app, 'DELETE', f'/api/v1/collaboration/groups/{group["id"]}/members/{member_id}')
        self.assertEqual(code, 200)
        self.assertIsNone(self.app.state.collaboration.store.owner(thread_id))

    async def test_group_broadcast_runs_three_native_engines_readonly_with_same_snapshot(self):
        deepseek = self.app.state.deepseek
        deepseek.create_thread = AsyncMock(return_value={'id': 'deepseek:fixture'})
        deepseek.start_turn = AsyncMock(return_value='deepseek-turn')
        deepseek.read_thread = AsyncMock(return_value={'turns': [{'id': 'deepseek-turn', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'text': 'Harness checked both earlier replies'}]}]})
        group, _ = await self.make_group()
        for engine in ('codex', 'claude-code', 'deepseek-harness'):
            code, _ = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members', {'requestId': str(uuid.uuid4()), 'engine': engine})
            self.assertEqual(code, 201)
        body = {'requestId': str(uuid.uuid4()), 'text': 'Work as a group'}
        path = f'/api/v1/collaboration/groups/{group["id"]}/messages'
        code, _ = await request(self.app, 'POST', path, body)
        self.assertEqual(code, 200)
        await self.app.state.collaboration._workers[group['id']]
        finished = self.app.state.collaboration.store.detail(group['id'])
        self.assertEqual(finished['status'], 'idle')
        self.assertEqual(len(finished['events']), 4)
        self.assertNotIn('Clean review', self.claude.start_turn.call_args.args[1])
        self.assertNotIn('Implemented and tested', deepseek.start_turn.call_args.args[1])
        self.assertEqual(self.codex.start_turn.call_args.kwargs['sandbox_policy'], {'type': 'readOnly'})
        self.assertEqual(self.claude.start_turn.call_args.kwargs['permission_mode'], 'plan')
        self.assertEqual(deepseek.start_turn.call_args.kwargs['deepseek_permission_mode'], 'read-only')
        code, repeated = await request(self.app, 'POST', path, body)
        self.assertEqual(code, 200)
        self.assertEqual(len(repeated['events']), 4)
        deepseek.start_turn.assert_awaited_once()

    async def test_group_workflow_uses_isolated_native_sessions_and_forces_stage_permissions(self):
        project = self.root / 'workflow-project'
        project.mkdir()
        engines = {'codex': self.codex, 'claude-code': self.claude, 'deepseek-harness': self.app.state.deepseek}
        directories, turns, calls = {}, {}, []
        group, _ = await self.make_group(cwd=str(project))
        for engine in engines:
            (project / (engine + '.txt')).write_text('base')
            code, group = await request(self.app, 'POST', f'/api/v1/collaboration/groups/{group["id"]}/members',
                                        {'requestId': str(uuid.uuid4()), 'engine': engine})
            self.assertEqual(code, 201)
        service = self.app.state.collaboration

        def configure(engine, native):
            async def create(cwd=None, **options):
                prefix = {'codex': '', 'claude-code': 'claude:', 'deepseek-harness': 'deepseek:'}[engine]
                thread_id = prefix + str(uuid.uuid4())
                directories[thread_id] = Path(cwd)
                return {'id': thread_id}
            async def emit(thread_id, prompt, *args, **options):
                state = service.group(group['id'])
                phase = state['run']['stage']
                calls.append((engine, phase, thread_id, options))
                if phase == 'discussion':
                    text = 'Proposal\n<devwisp:develop/>'
                elif phase == 'planning':
                    text = json.dumps({'action': 'develop', 'summary': 'Implement independent files', 'tasks': [
                        {'id': str(index), 'member': key, 'goal': 'Implement and verify', 'files': [member['engine'] + '.txt'], 'dependsOn': []}
                        for index, (key, member) in enumerate(state['members'].items())]})
                elif phase == 'developing':
                    self.assertNotEqual(directories[thread_id], project)
                    (directories[thread_id] / (engine + '.txt')).write_text(engine + ' changed')
                    text = 'Implemented and verified\n<devwisp:task-complete/>'
                else:
                    self.assertEqual(phase, 'summarizing')
                    text = 'Integrated changes verified'
                turn_id = str(uuid.uuid4())
                turns[thread_id] = {'turns': [{'id': turn_id, 'status': 'completed', 'items': [{'type': 'agentMessage', 'text': text}]}]}
                return {'turn': {'id': turn_id}} if engine == 'codex' else turn_id
            async def read(thread_id, **options):
                return turns.get(thread_id, {'turns': []})
            if engine == 'codex':
                native.start_thread = AsyncMock(side_effect=create)
            else:
                native.create_thread = AsyncMock(side_effect=create)
            native.start_turn = AsyncMock(side_effect=emit)
            native.read_thread = AsyncMock(side_effect=read)

        for engine, native in engines.items():
            configure(engine, native)
        payload = self.group_payload('Implement all three files', collaborationSettings={
            'codex': {'approvalPolicy': 'never'}, 'claude-code': {'claudePermissionMode': 'bypassPermissions'},
            'deepseek-harness': {'deepseekPermissionMode': 'danger-full-access'}})
        code, result = await request(self.app, 'POST', f'/api/v1/codex/queue/collaboration:{group["id"]}/submit', payload)
        self.assertEqual((code, result['sent']), (200, True))
        await asyncio.wait_for(service._workers[group['id']], 5)
        finished = service.group(group['id'])
        self.assertEqual(finished['status'], 'idle', finished['reason'])
        for engine in engines:
            self.assertEqual((project / (engine + '.txt')).read_text(), engine + ' changed')
        self.assertEqual(len(finished['workerMembers']), 3)
        for engine, phase, thread_id, options in calls:
            readonly = phase != 'developing'
            if engine == 'codex':
                self.assertEqual(options['sandbox_policy'], {'type': 'readOnly' if readonly else 'workspaceWrite'})
                self.assertEqual(options['approval_policy'], 'untrusted')
            elif engine == 'claude-code':
                self.assertEqual(options['permission_mode'], 'plan' if readonly else 'default')
            else:
                self.assertEqual(options['deepseek_permission_mode'], 'read-only' if readonly else 'workspace-write')
            self.assertEqual(service.store.owner(thread_id), group['id'])

    async def test_group_direct_adoption_race_never_binds_a_chat_twice(self):
        thread_id = self.existing('coder')
        responses = await asyncio.gather(*[
            request(self.app, 'POST', '/api/v1/collaboration/groups', {'requestId': str(uuid.uuid4()), 'member': self.body['coder']})
            for _ in range(2)
        ])
        self.assertEqual(sorted(code for code, _ in responses), [201, 409])
        accepted = next(group for code, group in responses if code == 201)
        self.assertEqual(self.app.state.collaboration.store.owner(thread_id), accepted['id'])
        self.assertEqual(len(self.app.state.collaboration.store.list()), 1)

    async def test_pairing_required_before_creating_or_reading_tasks(self):
        for method, path, body in [('POST', '/api/v1/collaboration/tasks', self.body), ('GET', '/api/v1/collaboration/tasks', None)]:
            code, _ = await request(self.app, method, path, body, authorized=False)
            self.assertEqual(code, 401)
        self.claude.create_thread.assert_not_awaited()

    async def test_invalid_inputs_never_create_members(self):
        for changes in ({'goal': ' '}, {'cwd': '\x00'}, {'maxRounds': 0}, {'maxRounds': 6},
                        {'maxRounds': True}, {'coder': {'engine': 'codex', 'threadId': 'bad/id'}},
                        {'coder': {'engine': 'codex', 'threadId': ''}}, {'reviewer': {'engine': 'unknown'}}, {'requestId': 'bad-id'}):
            code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', {**self.body, **changes})
            self.assertEqual(code, 422, changes)
        self.claude.create_thread.assert_not_awaited()

    async def test_duplicate_creation_returns_original_task_and_conflicts_on_changed_goal(self):
        task_id = await self.create()
        code, task = await request(self.app, 'POST', '/api/v1/collaboration/tasks', self.body)
        self.assertEqual((code, task['id']), (201, task_id))
        code, _ = await request(self.app, 'POST', '/api/v1/collaboration/tasks', {**self.body, 'goal': 'different'})
        self.assertEqual(code, 409)
        self.claude.create_thread.assert_awaited_once()

    async def test_notes_route_is_not_shadowed_by_controls(self):
        self.claude.read_thread.return_value['turns'][0]['items'][0]['text'] = 'Need human decision'
        task_id = await self.create()
        code, task = await request(self.app, 'POST', f'/api/v1/collaboration/tasks/{task_id}/notes', {'text': 'Preserve API'})
        self.assertEqual(code, 200)
        self.assertEqual(task['events'][-1]['text'], 'Preserve API')
        code, _ = await request(self.app, 'POST', f'/api/v1/collaboration/tasks/{task_id}/control/pause')
        self.assertEqual(code, 200)
        code, _ = await request(self.app, 'POST', f'/api/v1/collaboration/tasks/{task_id}/control/nonsense')
        self.assertEqual(code, 422)

    async def test_owned_members_cannot_be_sent_deleted_or_queued_manually(self):
        await self.create()
        for method, suffix, body in [('POST', 'threads/claude:fixture/turns', {'prompt': 'edit'}),
                                      ('DELETE', 'threads/codex-fixture', None),
                                      ('POST', 'queue/codex-fixture/submit', {})]:
            code, _ = await request(self.app, method, '/api/v1/codex/' + suffix, body)
            self.assertEqual(code, 409)
        self.assertEqual(self.claude.start_turn.await_count, 1)

    async def test_members_are_excluded_from_regular_chat_list(self):
        await self.create()
        self.claude.schedule_native_refresh = lambda: None
        self.claude.list_threads = lambda: [{'id': 'claude:fixture'}]
        self.app.state.deepseek.list_threads = lambda: []
        self.codex.list_threads = AsyncMock(return_value=[{'id': 'codex-fixture'}, {'id': 'normal-chat'}])
        code, data = await request(self.app, 'GET', '/api/v1/codex/threads')
        self.assertEqual(code, 200)
        self.assertEqual([thread['id'] for thread in data], ['normal-chat'])

    async def test_claude_and_harness_reviewers_use_native_read_only_modes(self):
        await self.app.state.collaboration.send({'engine': 'claude-code', 'model': '', 'threadId': 'claude:fixture'}, 'Review', True)
        self.assertEqual(self.claude.start_turn.call_args.kwargs['permission_mode'], 'plan')
        deepseek = self.app.state.deepseek
        deepseek.start_turn = AsyncMock(return_value='harness-turn')
        member = {'engine': 'deepseek-harness', 'model': 'custom-model', 'threadId': 'deepseek:fixture'}
        await self.app.state.collaboration.send(member, 'Review', True)
        self.assertEqual(deepseek.start_turn.call_args.kwargs['deepseek_permission_mode'], 'read-only')
        self.assertEqual(deepseek.start_turn.call_args.kwargs['model'], 'custom-model')
        await self.app.state.collaboration.send(member, 'Implement', False)
        self.assertEqual(deepseek.start_turn.call_args.kwargs['deepseek_permission_mode'], 'workspace-write')

    async def test_codex_read_only_policy_is_serialized_without_affecting_default_turns(self):
        codex = CodexAppServer('unused', self.root)
        codex.request = AsyncMock(return_value={'turn': {'id': 'readonly-turn', 'status': 'completed'}})
        codex._start_workspace_change_watch = AsyncMock()
        await codex.start_turn('thread', 'Review', approval_policy='untrusted', sandbox_policy={'type': 'readOnly'})
        self.assertEqual(codex.request.call_args.args[1]['sandboxPolicy'], {'type': 'readOnly'})
        await codex.start_turn('thread', 'Ordinary chat')
        self.assertNotIn('sandboxPolicy', codex.request.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
