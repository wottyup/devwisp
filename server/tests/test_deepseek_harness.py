from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from devwisp.claude_code import ClaudeCodeError
from devwisp.deepseek_harness import DeepSeekHarnessServer, harness_failure_message, is_deepseek_thread_id, provider_base_url
from devwisp.permission_prompt import PermissionPromptBroker


class _HarnessFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.events = []
        async def emit(event):
            self.events.append(event)
        self.server = DeepSeekHarnessServer(self.root, self.root / 'threads.sqlite3', emit)
        self.server.home = self.root / 'home'
        self.addAsyncCleanup(self.server.close)


class DeepSeekHarnessTests(_HarnessFixture):
    async def test_question_answer_is_durable_before_ack_and_emitted_before_resume(self):
        thread = await self.server.create_thread()
        questions = [
            {'id': 'food', 'question': '今晚吃什么？', 'multiSelect': True,
             'options': [{'label': '火锅'}, {'label': '面条'}]},
            {'id': 'where', 'question': '在哪里吃？'},
        ]
        task = asyncio.create_task(self.server.questions.ask(thread['id'], 'turn-answer', questions, threading.Event()))
        try:
            await asyncio.sleep(0)
            request = self.server.questions.requests()[0]
            response = {'answers': [
                {'id': 'where', 'selected': [], 'custom': ' 家里 '},
                {'id': 'food', 'selected': ['火锅', '面条'], 'custom': '少辣'},
            ]}
            self.server.questions.respond(thread['id'], request['questionId'], response)
            history = await self.server.read_thread(thread['id'])
            item = history['turns'][0]['items'][0]
            self.assertEqual(item['id'], f"user-question-{request['questionId']}")
            self.assertEqual(item['type'], 'userMessage')
            self.assertEqual(item['content'][0]['text'], '1. 今晚吃什么？\n答：火锅、面条、少辣\n\n2. 在哪里吃？\n答：家里')
            with self.assertRaises(ClaudeCodeError):
                self.server.questions.respond(thread['id'], request['questionId'], response)
            await task
            answer_events = [event for event in self.events if event.get('params', {}).get('item', {}).get('id') == item['id']]
            self.assertEqual(len(answer_events), 1)
            self.assertLess(self.events.index(answer_events[0]), next(index for index, event in enumerate(self.events)
                if event.get('type') == 'deepseek/question/resolved'))
            reopened = DeepSeekHarnessServer(self.root, self.root / 'threads.sqlite3', AsyncMock())
            try:
                self.assertEqual((await reopened.read_thread(thread['id']))['turns'][0]['items'], [item])
            finally:
                await reopened.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_agent_validation_and_rejected_turn_do_not_lock_blank_chat(self):
        thread = await self.server.create_thread()
        for preset in ('unknown', 'legacy'):
            with self.assertRaises(ClaudeCodeError):
                await self.server.start_turn(thread['id'], 'Hello', deepseek_agent_preset=preset)
        with self.assertRaises(ClaudeCodeError):
            await self.server.start_turn(thread['id'], 'Hello', deepseek_agent_preset='minimal', deepseek_permission_mode='plan')
        with self.assertRaises(ClaudeCodeError):
            await self.server.start_turn(thread['id'], '', deepseek_agent_preset='ptc')
        self.assertFalse((await self.server.read_thread(thread['id']))['deepseekAgentPresetLocked'])
        self.assertFalse(self.server._tasks)

    async def test_old_database_migration_keeps_old_sdk_sessions_and_blank_chats(self):
        blank = await self.server.create_thread()
        old = await self.server.create_thread()
        with self.server._connection:
            self.server._connection.execute('UPDATE claude_threads SET started = 1 WHERE thread_id = ?', (old['id'],))
            self.server._connection.execute('ALTER TABLE claude_threads DROP COLUMN agent_preset')
        migrated = DeepSeekHarnessServer(self.root, self.root / 'threads.sqlite3', AsyncMock())
        try:
            history = await migrated.read_thread(old['id'], include_turns=False)
            self.assertEqual(history['deepseekAgentPreset'], 'legacy')
            self.assertTrue(history['deepseekAgentPresetLocked'])
            self.assertEqual(migrated._validate_agent_preset(old['id'], None), 'legacy')
            with self.assertRaisesRegex(ClaudeCodeError, '锁定'):
                migrated._validate_agent_preset(old['id'], 'standard')
            self.assertFalse((await migrated.read_thread(blank['id']))['deepseekAgentPresetLocked'])
        finally:
            await migrated.close()

    async def test_thread_identity_and_metadata_survive_reopen(self):
        thread = await self.server.create_thread()
        self.assertTrue(is_deepseek_thread_id(thread['id']))
        self.assertEqual(thread['engine'], 'deepseek-harness')
        await self.server.rename_thread(thread['id'], 'Harness task')
        self.assertEqual((await self.server.read_thread(thread['id']))['name'], 'Harness task')
        other = DeepSeekHarnessServer(self.root, self.root / 'threads.sqlite3', AsyncMock())
        try:
            self.assertEqual(other.list_threads()[0]['id'], thread['id'])
        finally:
            await other.close()

    async def test_settings_never_return_key_and_preserve_blank(self):
        value = self.server.save_settings('deepseek-v4-flash', 'https://api.deepseek.com', 'test-placeholder')
        self.assertTrue(value['hasApiKey'])
        self.assertNotIn('test-placeholder', json.dumps(value))
        self.assertEqual(self.server.settings_path.stat().st_mode & 0o777, 0o600)
        self.server.save_settings('custom-model', 'http://localhost:1234', None)
        self.assertEqual(self.server._settings()['apiKey'], 'test-placeholder')
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            self.assertFalse(self.server.save_settings('custom-model', 'http://localhost:1234', None, True)['hasApiKey'])
        with self.assertRaises(ClaudeCodeError):
            self.server.save_settings('model', 'https://secret@example.com', None)

    async def test_protocol_settings_and_endpoint_validation(self):
        self.assertEqual(self.server.status()['protocol'], 'anthropic')
        for protocol in ['responses', 'anthropic', 'chat']:
            saved = self.server.save_settings('gateway-model', 'https://gateway.example/api/v1/', 'test-placeholder', protocol=protocol)
            self.assertEqual(saved['protocol'], protocol)
            self.assertNotIn('test-placeholder', json.dumps(saved))
            self.assertEqual(self.server.save_settings('gateway-model', saved['baseUrl'], '   ')['protocol'], protocol)
            self.assertEqual(self.server._settings()['apiKey'], 'test-placeholder')
        for url in ['https://gateway.example/v1/messages', 'https://gateway.example/v1/responses',
                    'https://gateway.example/v1/chat/completions', 'https://gateway.example:bad',
                    'https://gateway.example?key=hidden', 'https://gateway.example/with space']:
            with self.assertRaises(ClaudeCodeError):
                self.server.save_settings('model', url, None, protocol='chat')
        with self.assertRaises(ClaudeCodeError):
            self.server.save_settings('model', 'https://gateway.example', None, protocol='unsupported')
        self.assertEqual(provider_base_url('https://gateway.example', 'chat'), 'https://gateway.example/v1')
        self.assertEqual(provider_base_url('https://gateway.example/api/v1', 'anthropic'), 'https://gateway.example/api')
        self.assertEqual(provider_base_url('https://gateway.example/api/v1', 'responses'), 'https://gateway.example/api/v1')

    async def test_profile_migration_and_save_apply_key_isolation(self):
        from devwisp.native_engine_config import NativeEngineConfigError
        legacy = {'model': 'legacy-model', 'baseUrl': 'https://legacy.example', 'apiKey': 'legacy-test-secret'}
        self.server.settings_path.write_text(json.dumps(legacy))
        config = self.server.profiles()
        self.assertEqual(config['activeProfileId'], 'local')
        self.assertTrue(config['profiles'][0]['hasKey'])
        self.assertEqual(json.loads(self.server.settings_path.read_text()), legacy)  # read-only import
        draft = {'name': 'Backup', 'model': 'backup-model', 'baseUrl': 'https://backup.example/v1',
                 'protocol': 'responses', 'apiKey': 'backup-test-secret', 'apply': False,
                 'expectedRevision': config['profilesRevision']}
        saved = self.server.save_profile(draft)
        self.assertEqual(self.server._settings(), legacy)
        self.assertNotIn('test-secret', json.dumps(saved))
        self.assertEqual(len(saved['config']['profiles']), 2)
        self.assertEqual(self.server.settings_path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(NativeEngineConfigError):
            self.server.save_profile(draft)  # stale editor
        applied = self.server.save_profile({**draft, 'profileId': saved['profileId'], 'apiKey': '',
            'expectedRevision': saved['config']['profilesRevision'], 'apply': True})
        self.assertEqual(self.server._settings()['apiKey'], 'backup-test-secret')
        with self.assertRaises(NativeEngineConfigError):
            self.server.delete_profile(saved['profileId'], applied['config']['profilesRevision'])
        # Editing even the active profile without applying leaves its live snapshot untouched.
        edited = self.server.save_profile({**draft, 'profileId': saved['profileId'], 'model': 'edited-model',
            'apiKey': '', 'expectedRevision': applied['config']['profilesRevision']})
        self.assertEqual(self.server._settings()['model'], 'backup-model')
        self.assertTrue(edited['config']['activeProfileModified'])
        before_test = self.server.settings_path.read_bytes()
        connection = self.server.connection_draft({**draft, 'profileId': 'local', 'apiKey': '',
            'expectedRevision': edited['config']['profilesRevision']})
        self.assertEqual(connection['apiKey'], 'legacy-test-secret')
        self.assertEqual(connection['model'], 'backup-model')
        self.assertEqual(self.server.settings_path.read_bytes(), before_test)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            with self.assertRaises(NativeEngineConfigError):
                self.server.connection_draft({**draft, 'apiKey': '', 'expectedRevision': edited['config']['profilesRevision']})
        result = self.server.delete_profile('local', edited['config']['profilesRevision'])
        self.assertEqual(len(result['profiles']), 1)
        reopened = DeepSeekHarnessServer(self.root, self.root / 'threads.sqlite3', AsyncMock())
        try:
            reopened.home = self.server.home
            self.assertEqual(reopened.profiles(), result)
            self.assertEqual(reopened._settings()['model'], 'backup-model')
        finally:
            await reopened.close()

    async def test_profile_clear_key_duplicates_and_validation_do_not_mutate(self):
        from devwisp.native_engine_config import NativeEngineConfigError
        self.server.save_settings('model', 'https://gateway.example', 'test-secret', protocol='chat')
        config = self.server.profiles()
        draft = {'name': '本机当前配置', 'model': 'model', 'baseUrl': 'https://gateway.example',
                 'protocol': 'chat', 'apiKey': '', 'expectedRevision': config['profilesRevision']}
        before = self.server.settings_path.read_bytes()
        for changes in [{}, {'name': 'Other', 'baseUrl': 'https://user:secret@gateway.example'},
                        {'name': 'Other', 'model': 'bad\nmodel'}, {'name': 'Other', 'apiKey': 'bad\nkey'}]:
            with self.assertRaises(NativeEngineConfigError):
                self.server.save_profile({**draft, **changes})
            self.assertEqual(self.server.settings_path.read_bytes(), before)
        saved = self.server.save_profile({**draft, 'profileId': 'local', 'clearKey': True})
        self.assertFalse(saved['config']['profiles'][0]['hasKey'])
        self.assertNotIn('apiKey', self.server._settings())

    async def test_approval_allow_reject_cross_thread_and_cancellation(self):
        broker = PermissionPromptBroker(self.root / 'unused.sock', self.server.emit)
        task = asyncio.create_task(broker.request('deepseek:one', tool_name='bash', reason='test', cwd=str(self.root)))
        await asyncio.sleep(0)
        request = broker.pending_requests()[0]
        with self.assertRaises(RuntimeError):
            await broker.respond(request['permissionId'], 'deepseek:other', True)
        await broker.respond(request['permissionId'], 'deepseek:one', True)
        self.assertTrue(await task)
        task = asyncio.create_task(broker.request('deepseek:one', tool_name='bash', reason='test', cwd=str(self.root)))
        await asyncio.sleep(0)
        request = broker.pending_requests()[0]
        await broker.respond(request['permissionId'], 'deepseek:one', False)
        self.assertFalse(await task)
        task = asyncio.create_task(broker.request('deepseek:one', tool_name='bash', reason='test', cwd=str(self.root)))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(broker.pending_requests(), [])
        await broker.close()

    async def test_startup_error_finishes_accepted_turn_without_diagnostics(self):
        thread = await self.server.create_thread()
        with patch('deepseek_harness.DeepSeekHarness', side_effect=RuntimeError('test-sensitive-diagnostic')):
            await self.server.start_turn(thread['id'], 'hello')
            await asyncio.gather(*tuple(self.server._tasks.values()))
        history = await self.server.read_thread(thread['id'])
        self.assertEqual(history['status']['type'], 'idle')
        self.assertEqual(history['turns'][-1]['status'], 'failed')
        self.assertNotIn('test-sensitive-diagnostic', json.dumps(self.events))
        self.assertFalse(list(self.root.glob('harness-stream-*.json')))

    def test_native_error_classification_never_returns_provider_payload(self):
        for status, code, detail, expected in (
            (400, 'INVALID_REQUEST', 'reasoning_effort is invalid', '思考等级'),
            (401, 'AUTHENTICATION_ERROR', 'bad key', '认证失败'),
            (403, 'PERMISSION_DENIED', 'no access', '无访问权限'),
            (404, None, 'unknown model', '未找到模型或接口'),
            (429, 'RATE_LIMITED', 'too many requests', '请求受限'),
            (503, None, 'unavailable', '暂时不可用'),
            (400, 'INVALID_REQUEST', 'invalid image', '拒绝了请求参数'),
            (None, None, 'unknown error', '执行失败'),
        ):
            with self.subTest(status=status):
                message = f'{status}: {detail}; authorization=Bearer private-fixture-key; https://private.example/?token=secret'
                classified = harness_failure_message({'error': {'message': message, 'code': code}})
                self.assertIn(expected, classified)
                self.assertNotIn('private', classified)
                self.assertNotIn('secret', classified)
        for malformed in (None, [], {}, {'error': 'private'}, {'error': {'message': []}}):
            self.assertIn('执行失败', harness_failure_message(malformed))


class HarnessLifecycleTests(_HarnessFixture):
    async def test_startup_failure_releases_waiting_input_and_terminal_context(self):
        from devwisp.terminal_handoff import TerminalHandoffBroker
        broker = TerminalHandoffBroker(self.root / 'terminal.sock')
        self.server.enable_terminal_handoff(broker)
        thread = await self.server.create_thread()
        with patch('deepseek_harness.DeepSeekHarness', side_effect=RuntimeError('private diagnostic')):
            await self.server.start_turn(thread['id'], 'Initial')
            await asyncio.wait_for(self.server.steer_turn(thread['id'], 'Follow after failure'), 3)
            await asyncio.gather(*tuple(self.server._tasks.values()))
        self.assertFalse(self.server._turn_inputs)
        self.assertFalse(self.server._live_inputs)
        self.assertFalse(broker._contexts)
        self.assertFalse(self.server._terminal_contexts)
        self.assertEqual((await self.server.read_thread(thread['id']))['status']['type'], 'idle')


class DeepSeekDiagnosticTests(_HarnessFixture):
    async def test_real_http_draft_diagnostics_all_protocols_and_safe_failures(self):
        from devwisp.native_engine_connection import check_connection
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.server.requests.append((self.path, body, dict(self.headers)))
                if body['model'] == 'reject':
                    self.send_response(401)
                    payload = {'error': 'secret-diagnostic-key'}
                else:
                    self.send_response(200)
                    payload = {'/v1/messages': {'type': 'message', 'content': [{'type': 'text', 'text': 'OK'}]},
                        '/v1/chat/completions': {'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]},
                        '/v1/responses': {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]}}.get(self.path, {})
                data = json.dumps(payload).encode()
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        http = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        http.requests = []
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            for protocol, suffix in [('responses', '/responses'), ('anthropic', '/messages'), ('chat', '/chat/completions')]:
                draft = {'name': 'Unsaved', 'model': 'test-model', 'baseUrl': f'http://127.0.0.1:{http.server_port}/v1',
                         'protocol': protocol, 'apiKey': 'secret-diagnostic-key',
                         'expectedRevision': self.server.profiles()['profilesRevision']}
                connection = self.server.connection_draft(draft)
                result = await check_connection(connection, asyncio.Semaphore(2))
                self.assertTrue(result['ok'], result)
                path, body, headers = http.requests[-1]
                self.assertEqual(path, '/v1' + suffix)
                self.assertEqual(body['model'], 'test-model')
                self.assertEqual(headers.get('X-Api-Key') if protocol == 'anthropic' else headers.get('Authorization'),
                    'secret-diagnostic-key' if protocol == 'anthropic' else 'Bearer secret-diagnostic-key')
                rejected = await check_connection({**connection, 'model': 'reject'}, asyncio.Semaphore(2))
                self.assertFalse(rejected['ok'])
                self.assertIn('401', rejected['results'][0]['message'])
                self.assertNotIn('secret-diagnostic-key', json.dumps(rejected))
                with patch('devwisp.native_engine_connection.build_opener') as opener:
                    opener.return_value.open.side_effect = TimeoutError()
                    timed_out = await check_connection(connection, asyncio.Semaphore(2))
                    self.assertFalse(timed_out['ok'])
                    self.assertIn('20', timed_out['results'][0]['message'])
            self.assertFalse(self.server.settings_path.exists())  # no save or activation
        finally:
            await asyncio.to_thread(http.shutdown)
            http.server_close()
            thread.join(2)


class _ModelEndpoint(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        data = json.dumps({'object': 'list', 'data': [{'id': 'deepseek-v4-flash', 'object': 'model', 'owned_by': 'deepseek'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        path = urlsplit(self.path).path
        self.server.paths.append(path)
        self.server.requests.append(body)
        tool_enabled = getattr(self.server, 'tool_enabled', False) and not (getattr(self.server, 'exclude_tool_prompt', None) and self.server.exclude_tool_prompt in json.dumps(body))
        if getattr(self.server, 'hold_first', None) and len(self.server.requests) == 1:
            self.server.hold_first.wait(10)
        self.server.request_headers.append(dict(self.headers))
        api_error = getattr(self.server, 'api_error', None)
        allowed_efforts = getattr(self.server, 'allowed_efforts', None)
        if allowed_efforts is not None and body.get('reasoning_effort') not in [None, *allowed_efforts]:
            api_error = (400, {'error': {'message': "reasoning_effort must be one of: 'instant', 'low', 'medium', 'high'"}})
        if api_error:
            self.send_response(api_error[0])
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(api_error[1]).encode())
            return
        expected = getattr(self.server, 'expected_path', None)
        if expected and path != expected:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if path.endswith('/messages'):
            chunks = [
                ('message_start', {'type': 'message_start', 'message': {'id': 'msg-test', 'type': 'message', 'role': 'assistant', 'content': [], 'model': body.get('model'), 'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Hello '}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Harness'}}),
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 2}}),
                ('message_stop', {'type': 'message_stop'}),
            ]
            has_tool_result = any(isinstance(block, dict) and block.get('type') == 'tool_result'
                for message in body.get('messages', [])
                for block in (message.get('content', []) if isinstance(message.get('content'), list) else []))
            calls = []
            if hasattr(self.server, 'tool_batches'):
                completed = {block.get('tool_use_id') for message in body.get('messages', [])
                    for block in (message.get('content', []) if isinstance(message.get('content'), list) else [])
                    if isinstance(block, dict) and block.get('type') == 'tool_result'}
                for batch_index, batch in enumerate(self.server.tool_batches):
                    calls = [(f'{self.server.tool_batch_tag}-{batch_index}-{index}', name, args)
                             for index, (name, args) in enumerate(batch)]
                    if not all(call_id in completed for call_id, _, _ in calls):
                        break
                    calls = []
            elif tool_enabled and not has_tool_result:
                calls = [('call-smoke', getattr(self.server, 'tool_name', 'bash'), getattr(self.server, 'tool_arguments', {
                    'description': 'Print a harmless local integration test marker',
                    'command': 'printf harness-tool-ok', 'sandbox_permissions': 'danger-full-access',
                    'justification': 'Test a harmless command through the browser approval adapter.'}))]
            if calls:
                chunks = [chunks[0]]
                for index, (call_id, name, arguments) in enumerate(calls):
                    chunks.extend([
                        ('content_block_start', {'type': 'content_block_start', 'index': index,
                            'content_block': {'type': 'tool_use', 'id': call_id, 'name': name, 'input': {}}}),
                        ('content_block_delta', {'type': 'content_block_delta', 'index': index,
                            'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(arguments)}}),
                        ('content_block_stop', {'type': 'content_block_stop', 'index': index})])
                chunks.extend([
                    ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 2}}),
                    ('message_stop', {'type': 'message_stop'})])
            for event, data in chunks:
                self.wfile.write((f'event: {event}\ndata: {json.dumps(data)}\n\n').encode())
                self.wfile.flush()
        elif path.endswith('/responses'):
            has_result = any(item.get('type') == 'function_call_output' for item in body.get('input', []) if isinstance(item, dict))
            tool = tool_enabled and not has_result
            arguments = json.dumps({'description': 'Print an integration test marker', 'command': 'printf harness-tool-ok',
                                    'sandbox_permissions': 'danger-full-access', 'justification': 'Test browser approval.'})
            item = ({'id': 'fc-test', 'type': 'function_call', 'call_id': 'call-smoke', 'name': 'bash', 'arguments': ''} if tool else
                    {'id': 'msg-test', 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []})
            response = {'id': 'resp-test', 'object': 'response', 'created_at': 1, 'model': body.get('model'), 'status': 'in_progress', 'output': []}
            chunks = [('response.created', {'response': response}),
                      ('response.output_item.added', {'output_index': 0, 'item': dict(item)})]
            if tool:
                chunks += [('response.function_call_arguments.delta', {'item_id': item['id'], 'output_index': 0, 'delta': arguments}),
                           ('response.function_call_arguments.done', {'item_id': item['id'], 'output_index': 0, 'arguments': arguments})]
                item['arguments'] = arguments
            else:
                part = {'type': 'output_text', 'text': '', 'annotations': []}
                chunks.append(('response.content_part.added', {'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'part': dict(part)}))
                for delta in ['Hello ', 'Harness']:
                    chunks.append(('response.output_text.delta', {'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'delta': delta}))
                part['text'] = 'Hello Harness'
                chunks += [('response.output_text.done', {'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'text': part['text']}),
                           ('response.content_part.done', {'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'part': part})]
                item.update(status='completed', content=[part])
            chunks.append(('response.output_item.done', {'output_index': 0, 'item': item}))
            chunks.append(('response.completed', {'response': {**response, 'status': 'completed', 'output': [item],
                'usage': {'input_tokens': 1, 'output_tokens': 2, 'total_tokens': 3, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}}))
            for seq, (event, payload) in enumerate(chunks):
                data = {'type': event, 'sequence_number': seq, **payload}
                self.wfile.write((f'event: {event}\ndata: {json.dumps(data)}\n\n').encode())
                self.wfile.flush()
        else:
            chat_messages = body.get('messages', [])
            if getattr(self.server, 'tools_each_turn', False):
                last_user = max((i for i, message in enumerate(chat_messages) if message.get('role') == 'user'), default=-1)
                chat_messages = chat_messages[last_user + 1:]
            has_result = any(message.get('role') == 'tool' for message in chat_messages)
            question_intros = getattr(self.server, 'question_intros', None)
            question_index = sum(message.get('role') == 'tool' for message in chat_messages)
            deltas = [({'role': 'assistant', 'content': 'Hello '}, None), ({'content': 'Harness'}, None), ({}, 'stop')]
            if tool_enabled and (question_index < len(question_intros) if question_intros is not None else not has_result):
                arguments = json.dumps({'description': 'Print an integration test marker', 'command': 'printf harness-tool-ok',
                                        'sandbox_permissions': 'danger-full-access', 'justification': 'Test browser approval.'})
                deltas = [({'role': 'assistant', 'tool_calls': [{'index': 0, 'id': f'call-question-{question_index}' if question_intros is not None else 'call-smoke', 'type': 'function',
                    'function': {'name': getattr(self.server, 'tool_name', 'bash'), 'arguments': json.dumps(getattr(self.server, 'tool_arguments', None)) if getattr(self.server, 'tool_arguments', None) else arguments}}]}, None), ({}, 'tool_calls')]
                if question_intros is not None:
                    deltas.insert(0, ({'role': 'assistant', 'content': question_intros[question_index]}, None))
            if getattr(self.server, 'reasoning_enabled', False):
                deltas.insert(0, ({'role': 'assistant', 'reasoning_content': 'Fixture reasoning.'}, None))
            for delta, reason in deltas:
                data = {'id': 'chat-test', 'created': 1, 'model': body.get('model'), 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
                self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
                self.wfile.flush()
            if getattr(self.server, 'usage_enabled', False):
                data = {'id': 'chat-test', 'created': 1, 'model': body.get('model'), 'object': 'chat.completion.chunk', 'choices': [],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'prompt_tokens_details': {'cached_tokens': 2}}}
                self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()


@unittest.skipUnless(importlib.util.find_spec('deepseek_harness'), 'official Harness SDK not installed')
class _RealHarnessFixture(_HarnessFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), _ModelEndpoint)
        self.http.paths = []
        self.http.requests = []
        self.http.request_headers = []
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.addCleanup(self.http_thread.join, 2)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.server.save_settings('deepseek-v4-flash', f'http://127.0.0.1:{self.http.server_port}', 'local-test-placeholder')


class RealHarnessTests(_RealHarnessFixture):
    async def test_native_agent_presets_change_tools_and_resume_the_same_composition(self):
        catalogs = {}
        for preset in ('standard', 'ptc', 'minimal', 'cordis'):
            with self.subTest(preset=preset):
                thread = await self.server.create_thread()
                self.assertFalse(thread['deepseekAgentPresetLocked'])
                offset = len(self.http.requests)
                await self.server.start_turn(thread['id'], 'Hello', deepseek_agent_preset=preset)
                await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
                history = await self.server.read_thread(thread['id'])
                self.assertEqual(history['turns'][-1]['status'], 'completed', history)
                self.assertEqual(history['deepseekAgentPreset'], preset)
                self.assertTrue(history['deepseekAgentPresetLocked'])
                self.assertEqual(len(self.http.requests), offset + 1)  # no hidden initialization prompts
                catalogs[preset] = {tool['name'] for tool in self.http.requests[-1].get('tools', [])}
                await self.server.start_turn(thread['id'], 'Resume')  # omitted selection uses journal
                await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
                history = await self.server.read_thread(thread['id'])
                self.assertEqual(history['turns'][-1]['status'], 'completed', history)
                self.assertEqual(catalogs[preset], {t['name'] for t in self.http.requests[-1].get('tools', [])})
                with self.assertRaisesRegex(ClaudeCodeError, '锁定'):
                    await self.server.start_turn(thread['id'], 'Change', deepseek_agent_preset='ptc' if preset == 'standard' else 'standard')
        self.assertIn('bash', catalogs['standard'])
        self.assertIn('ask_user_question', catalogs['standard'])
        self.assertNotEqual(catalogs['standard'], catalogs['ptc'])
        self.assertNotIn('ask_user_question', catalogs['minimal'])
        self.assertNotIn('write', catalogs['minimal'])
        self.assertLess(len(catalogs['minimal']), len(catalogs['standard']))
        self.assertNotEqual(catalogs['standard'], catalogs['cordis'])

    async def test_native_preset_specific_tools_run_and_old_sdk_history_still_resumes(self):
        self.http.tool_enabled = True
        for preset, tool_name, arguments, marker in (
            ('ptc', 'run_code', {'code': 'console.log("ptc-agent-ok")', 'description': 'Print fixture marker'}, 'ptc-agent-ok'),
            ('minimal', 'bash', {'command': 'printf minimal-agent-ok'}, 'minimal-agent-ok'),
            ('cordis', 'cordis_inspect_list', {}, 'Service'),
            ('legacy', 'bash', {'command': 'printf legacy-agent-ok', 'description': 'Print fixture marker'}, 'legacy-agent-ok'),
        ):
            with self.subTest(preset=preset):
                self.http.tool_name, self.http.tool_arguments = tool_name, arguments
                thread = await self.server.create_thread()
                if preset == 'legacy':
                    with self.server._connection:
                        self.server._connection.execute("UPDATE claude_threads SET agent_preset = 'legacy' WHERE thread_id = ?", (thread['id'],))
                await self.server.start_turn(thread['id'], 'Call the test tool', deepseek_agent_preset=preset,
                                             deepseek_permission_mode='danger-full-access')
                await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
                history = await self.server.read_thread(thread['id'])
                self.assertEqual(history['turns'][-1]['status'], 'completed', history)
                self.assertIn(marker, json.dumps(history), history)
                self.assertTrue(any(m['role'] == 'tool' for m in json.loads(self.server._row(thread['id'])['messages_json'])))
                if preset == 'legacy':
                    await self.server.start_turn(thread['id'], 'Resume old SDK chat')
                    await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
                    history = await self.server.read_thread(thread['id'])
                    self.assertEqual(history['turns'][-1]['status'], 'completed', history)
                    self.assertEqual(history['deepseekAgentPreset'], 'legacy')

    async def test_recorded_native_preset_mismatch_fails_without_replacing_history(self):
        thread = await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Keep original history', deepseek_agent_preset='standard')
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        calls = len(self.http.requests)
        # Simulate a damaged local journal: the native session remains authoritative.
        with self.server._connection:
            self.server._connection.execute("UPDATE claude_threads SET agent_preset = 'ptc' WHERE thread_id = ?", (thread['id'],))
        await self.server.start_turn(thread['id'], 'Must not run with the wrong preset')
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        history = await self.server.read_thread(thread['id'])
        self.assertEqual(history['turns'][-1]['status'], 'failed')
        self.assertEqual(len(self.http.requests), calls)
        with self.server._connection:
            self.server._connection.execute("UPDATE claude_threads SET agent_preset = 'standard' WHERE thread_id = ?", (thread['id'],))
        await self.server.start_turn(thread['id'], 'Resume the intact original session')
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        self.assertEqual((await self.server.read_thread(thread['id']))['turns'][-1]['status'], 'completed')
        self.assertIn('Keep original history', json.dumps(self.http.requests[-1]))

    async def test_native_questions_plan_and_cancel(self):
        self.http.tool_enabled = True
        self.http.tool_name = 'ask_user_question'
        self.http.tool_arguments = {'questions': [{'id': 'choice', 'question': 'Pick two', 'multiSelect': True,
            'options': [{'label': 'Original A'}, {'label': 'Original B'}]}]}
        responses = []
        async def emit(event):
            self.events.append(event)
            if event.get('type') == 'deepseek/question/request':
                request = event['params']; responses.append(request)
                q = request['questions'][0]
                self.server.questions.respond(request['threadId'], request['questionId'], {'answers': [
                    {'id': q['id'], 'selected': [q['intent']['approve']] if q.get('intent') else ['Original A', 'Original B']}]})
        self.server.questions.emit = emit
        for mode in ['workspace-write', 'plan']:
            if mode == 'plan':
                self.http.tool_name = 'exit_plan_mode'
                self.http.tool_arguments = {'plan': '# Test plan\nRead the fixture only.'}
            thread = await self.server.create_thread()
            await self.server.start_turn(thread['id'], 'Ask for user input', deepseek_permission_mode=mode)
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
            history = await self.server.read_thread(thread['id'])
            self.assertEqual(history['turns'][-1]['status'], 'completed', history)
        self.assertEqual(len(responses), 2, self.events)
        self.assertEqual(responses[-1]['questions'][0]['intent']['kind'], 'plan-review')
        self.assertEqual(self.server.questions.requests(), [])

    async def test_native_live_followups_same_turn_and_dedup(self):
        self.http.hold_first = threading.Event()
        self.addCleanup(self.http.hold_first.set)
        thread = await self.server.create_thread()
        turn = await self.server.start_turn(thread['id'], 'Initial', request_id='initial')
        for _ in range(200):
            if self.http.requests: break
            await asyncio.sleep(.05)
        self.assertTrue(self.http.requests)
        upload = self.root / 'follow-upload'; upload.mkdir()
        for key in ['follow-one', 'follow-two', 'follow-one']:
            self.assertEqual(await asyncio.wait_for(self.server.steer_turn(thread['id'], key, request_id=key, temporary_directory=upload), 5), turn)
        self.http.hold_first.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        history = await self.server.read_thread(thread['id'])
        self.assertEqual(history['turns'][-1]['status'], 'completed', history)
        self.assertEqual(len(history['turns']), 1)
        self.assertFalse(upload.exists())
        self.assertFalse(self.server._turn_inputs)
        self.assertGreaterEqual(len(self.http.requests), 2)
        last = json.dumps(self.http.requests[-1])
        self.assertIn('follow-one', last); self.assertIn('follow-two', last)
        users = [i for i in history['turns'][0]['items'] if i['type'] == 'userMessage']
        self.assertEqual(len(users), 3, history)

    async def test_disconnected_live_request_still_records_native_admission(self):
        self.http.hold_first = threading.Event()
        self.addCleanup(self.http.hold_first.set)
        thread = await self.server.create_thread()
        turn = await self.server.start_turn(thread['id'], 'Initial')
        for _ in range(200):
            if self.http.requests: break
            await asyncio.sleep(.05)
        self.assertTrue(self.http.requests)
        live = self.server._live_inputs[thread['id']]
        original = live.client.session_prompt
        accepted, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def admit_then_wait(*args):
            result = original(*args)
            accepted.set()
            release.wait(5)
            return result
        with patch.object(live.client, 'session_prompt', side_effect=admit_then_wait):
            request = asyncio.create_task(self.server.steer_turn(thread['id'], 'Accepted despite disconnect', request_id='disconnect-test'))
            try:
                for _ in range(100):
                    if accepted.is_set(): break
                    await asyncio.sleep(.02)
                self.assertTrue(accepted.is_set())
                request.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError): await request
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
        self.http.hold_first.set()
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        history = await self.server.read_thread(thread['id'])
        self.assertIn('Accepted despite disconnect', json.dumps(history))
        self.assertEqual(history['turns'][0]['id'], turn)
        self.assertEqual(history['turns'][0]['status'], 'completed')
        self.assertFalse(self.server._turn_inputs)

    async def test_saved_ssh_settings_load_native_tools_without_connecting(self):
        from devwisp.ssh_mcp_settings import SshMcpSettingsStore
        store = SshMcpSettingsStore(self.root / 'settings')
        store.save([{'name': 'isolated-viewer', 'host': '192.0.2.1', 'port': 22, 'user': 'fixture',
            'auth': 'agent', 'group': 'dev', 'role': 'viewer', 'readOnly': True, 'approvalPolicy': 'ask-all'}])
        self.server.enable_ssh_mcp(store)
        thread = await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Discover tools only')
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        self.assertEqual((await self.server.read_thread(thread['id']))['turns'][-1]['status'], 'completed')
        names = [tool['name'] for tool in self.http.requests[0]['tools']]
        self.assertTrue(any(name.startswith('mcp__devwisp_ssh_mcp__') for name in names), names)
        self.assertIn('role = "viewer"', store.config_path.read_text())
        self.assertIn('approvalPolicy = "ask-all"', store.config_path.read_text())

    async def test_native_ssh_mcp_browser_approval_allow_and_deny(self):
        from types import SimpleNamespace
        fixture = Path(__file__).parent / 'fixtures' / 'harness_mcp.py'
        self.server.enable_ssh_mcp(SimpleNamespace(codex_server_config=lambda: {
            'command': sys.executable, 'args': [str(fixture)], 'env': {'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'HARNESS_TEST_TRACE': str(self.root / 'mcp-trace')}}))
        self.http.tool_enabled = True
        self.http.tool_name = 'mcp__devwisp_ssh_mcp__confirm_test'
        self.http.tool_arguments = {}
        for allow in [True, False]:
            decisions = []
            async def emit(event):
                self.events.append(event)
                if event.get('type') == 'claude/permission/request':
                    request = event['params']; decisions.append(request)
                    await broker.respond(request['permissionId'], request['threadId'], allow)
            broker = PermissionPromptBroker(self.root / 'runtime' / 'permission.sock', emit)
            await broker.start()
            self.server.enable_permission_prompt(broker)
            try:
                thread = await self.server.create_thread()
                await self.server.start_turn(thread['id'], 'Test server confirmation', deepseek_permission_mode='danger-full-access')
                await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
                history = await self.server.read_thread(thread['id'])
                self.assertEqual(history['turns'][-1]['status'], 'completed', history)
                self.assertEqual(len(decisions), 1, {'history': history, 'trace': (self.root / 'mcp-trace').read_text() if (self.root / 'mcp-trace').exists() else 'not started'})
                self.assertEqual(decisions[0]['threadId'], thread['id'])
                self.assertIn('accept' if allow else 'decline', json.dumps(history))
                self.assertEqual(broker.pending_requests(), [])
            finally:
                await broker.close()

        received = asyncio.Event()
        async def pending_emit(event):
            self.events.append(event)
            if event.get('type') == 'claude/permission/request': received.set()
        broker = PermissionPromptBroker(self.root / 'runtime' / 'permission.sock', pending_emit)
        await broker.start()
        self.server.enable_permission_prompt(broker)
        try:
            thread = await self.server.create_thread()
            await self.server.start_turn(thread['id'], 'Stop while waiting for SSH approval', deepseek_permission_mode='danger-full-access')
            tasks = tuple(self.server._tasks.values())
            await asyncio.wait_for(received.wait(), 20)
            await self.server.interrupt(thread['id'])
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            for _ in range(40):
                if not broker.pending_requests(): break
                await asyncio.sleep(.05)
            self.assertEqual(broker.pending_requests(), [])
        finally:
            await broker.close()

    async def test_native_all_permission_presets_and_question_stop(self):
        for mode in ['read-only', 'workspace-write', 'full-access-ask', 'danger-full-access', 'plan']:
            thread = await self.server.create_thread()
            await self.server.start_turn(thread['id'], 'Check permission initialization', deepseek_permission_mode=mode)
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 30)
            self.assertEqual((await self.server.read_thread(thread['id']))['turns'][-1]['status'], 'completed', mode)
        self.http.tool_enabled = True
        self.http.tool_name = 'ask_user_question'
        self.http.tool_arguments = {'questions': [{'id': 'wait', 'question': 'Wait for user', 'options': [{'label': 'Yes'}]}]}
        received = asyncio.Event()
        async def emit(event):
            self.events.append(event)
            if event.get('type') == 'deepseek/question/request': received.set()
        self.server.questions.emit = emit
        thread = await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Wait')
        tasks = tuple(self.server._tasks.values())
        await asyncio.wait_for(received.wait(), 20)
        self.assertEqual(len(self.server.questions.requests(thread['id'])), 1)
        await self.server.interrupt(thread['id'])
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        self.assertEqual(self.server.questions.requests(), [])
        self.assertFalse(self.server._turn_inputs)
        self.assertFalse(self.server._live_inputs)

    async def test_native_terminal_mcp_loads_and_respects_consent(self):
        from devwisp.terminal_handoff import TerminalHandoffBroker
        broker = TerminalHandoffBroker(self.root / 'runtime' / 'terminal.sock')
        await broker.start()
        self.addAsyncCleanup(broker.close)
        self.server.enable_terminal_handoff(broker)
        self.http.tool_enabled = True
        self.http.tool_name = 'mcp__devwisp_terminal__read_terminal'
        self.http.tool_arguments = {}
        thread = await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Read terminal', deepseek_permission_mode='danger-full-access')
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        history = await self.server.read_thread(thread['id'])
        self.assertEqual(history['turns'][-1]['status'], 'completed', history)
        tools = self.http.requests[0]['tools']
        self.assertIn(self.http.tool_name, [t['name'] for t in tools])
        self.assertFalse(broker.is_enabled(thread['id']))
        self.assertIn(thread['id'], broker._contexts.values())
        await self.server.delete_thread(thread['id'])
        self.assertEqual(broker._contexts, {})
        self.assertTrue(any(i['type'] == 'dynamicToolCall' for i in history['turns'][0]['items']), history)

    async def test_real_runtime_stream_and_persistent_resume(self):
        thread = await self.server.create_thread()
        thread_id = thread['id']
        for prompt in ['Hello', 'Continue']:
            await self.server.start_turn(thread_id, prompt, model='deepseek-v4-flash')
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 30)
            history = await self.server.read_thread(thread_id)
            self.assertEqual(history['turns'][-1]['status'], 'completed', history)
        deltas = [event['params']['delta'] for event in self.events if event.get('method') == 'item/agentMessage/delta']
        self.assertEqual(deltas, ['Hello ', 'Harness', 'Hello ', 'Harness'])
        self.assertTrue(self.http.paths)
        self.assertEqual(len((await self.server.read_thread(thread_id))['turns']), 2)
        telemetry = next(item for item in history['turns'][-1]['items'] if item['type'] == 'harnessTelemetry')
        self.assertEqual(telemetry['context'], {'contextWindow': 1000000, 'pressureTokens': 1})
        self.assertEqual(telemetry['usage']['totalTokens'], 3)
        self.assertEqual(telemetry['continuity'], 'reused')
        self.assertFalse(list(self.root.glob('harness-stream-*.json')))

    async def test_third_party_stream_tools_approvals_and_resume_all_protocols(self):
        for protocol, suffix in [('responses', 'responses'), ('anthropic', 'messages'), ('chat', 'chat/completions')]:
            with self.subTest(protocol=protocol):
                self.http.expected_path = '/gateway/v1/' + suffix
                self.http.paths.clear(); self.http.requests.clear(); self.http.request_headers.clear()
                self.http.tool_enabled = True
                self.server.save_settings('gateway-deepseek', f'http://127.0.0.1:{self.http.server_port}/gateway/v1', 'third-party-placeholder', protocol=protocol)
                decisions = []
                async def emit(event):
                    self.events.append(event)
                    if event.get('type') == 'claude/permission/request':
                        request = event['params']; decisions.append(request)
                        await broker.respond(request['permissionId'], request['threadId'], True)
                self.server.emit = emit
                broker = PermissionPromptBroker(self.root / 'unused.sock', emit)
                self.server.enable_permission_prompt(broker)
                try:
                    thread = await self.server.create_thread()
                    for prompt in ['Test third-party tool', 'Continue persisted history']:
                        await self.server.start_turn(thread['id'], prompt)
                        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
                        history = await self.server.read_thread(thread['id'])
                        self.assertEqual(history['turns'][-1]['status'], 'completed', history)
                        self.assertIn('Hello Harness', json.dumps(history))
                    self.assertEqual(len(history['turns']), 2)
                    self.assertTrue(decisions)
                    self.assertEqual(set(self.http.paths), {self.http.expected_path})
                    self.assertTrue(all(body['model'] == 'gateway-deepseek' for body in self.http.requests))
                    first = {key.lower(): value for key, value in self.http.request_headers[0].items()}
                    header = 'x-api-key' if protocol == 'anthropic' else 'authorization'
                    self.assertIn('third-party-placeholder', first[header])
                    self.assertNotIn('third-party-placeholder', json.dumps(self.events))
                finally:
                    await broker.close()

    async def test_legacy_session_can_switch_protocol_and_model_without_losing_history(self):
        thread = await self.server.create_thread()
        for protocol in [None, 'chat', 'responses', 'anthropic']:
            self.server.save_settings('gateway-model' if protocol else 'deepseek-v4-flash',
                f'http://127.0.0.1:{self.http.server_port}' + ('/v1' if protocol else ''), None, protocol=protocol)
            self.http.expected_path = '/v1/' + {'chat': 'chat/completions', 'responses': 'responses'}.get(protocol, 'messages')
            self.http.paths.clear()
            await self.server.start_turn(thread['id'], 'Continue with the newly selected protocol')
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
            history = await self.server.read_thread(thread['id'])
            self.assertEqual(history['turns'][-1]['status'], 'completed', history)
            self.assertEqual(self.http.paths, [self.http.expected_path])
        self.assertEqual(len(history['turns']), 4)

    async def test_third_party_missing_key_does_not_use_unrelated_ambient_credentials(self):
        self.server.save_settings('gateway-model', f'http://127.0.0.1:{self.http.server_port}/v1',
                                  None, clear_key=True, protocol='chat')
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': '', 'OPENAI_API_KEY': 'unrelated-placeholder',
                                     'DEVWISP_HARNESS_API_KEY': 'unrelated-inherited-placeholder'}):
            thread = await self.server.create_thread()
            await self.server.start_turn(thread['id'], 'No configured key')
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 30)
        history = await self.server.read_thread(thread['id'])
        self.assertEqual(history['turns'][-1]['status'], 'failed')
        self.assertEqual(self.http.requests, [])

    async def test_third_party_high_reasoning_and_stop(self):
        for protocol in ['responses', 'anthropic', 'chat']:
            with self.subTest(protocol=protocol):
                self.server.save_settings('gateway-model', f'http://127.0.0.1:{self.http.server_port}/v1', None, protocol=protocol)
                thread = await self.server.create_thread()
                await self.server.start_turn(thread['id'], 'Use selected reasoning', effort='high')
                await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 30)
                history = await self.server.read_thread(thread['id'])
                self.assertEqual(history['turns'][-1]['status'], 'completed', history)
        await self.test_stop_cancels_pending_approval_and_closes_runtime()

    async def test_mercury_supported_reasoning_and_saved_max_resume(self):
        self.http.allowed_efforts = ('instant', 'low', 'medium', 'high')
        self.server.save_settings('mercury-3', f'http://127.0.0.1:{self.http.server_port}/v1',
                                  'local-test-placeholder', protocol='chat')
        supported = self.server.status()['reasoningEffortsByModel']
        self.assertEqual(supported['mercury-3'], ['default', 'instant', 'low', 'medium', 'high'])
        self.assertIn('max', supported['deepseek-v4-flash'])
        thread = await self.server.create_thread()
        # Harness chooses its native default from the configured model's levels.
        for effort, expected in [('max', 'high'), ('instant', 'instant'), ('default', 'high'),
                                 ('low', 'low'), ('medium', 'medium'), ('high', 'high')]:
            with self.subTest(effort=effort):
                offset = len(self.http.requests)
                await self.server.start_turn(thread['id'], 'Hello', effort=effort, deepseek_agent_preset='standard')
                await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 30)
                history = await self.server.read_thread(thread['id'])
                self.assertEqual(history['turns'][-1]['status'], 'completed', history)
                self.assertEqual(len(self.http.requests), offset + 1)
                self.assertEqual(self.http.requests[-1].get('reasoning_effort'), expected)
                self.assertEqual(history['deepseekAgentPreset'], 'standard')
        self.assertEqual(len(history['turns']), 6)
        self.assertNotIn('local-test-placeholder', json.dumps(self.events))

    async def test_native_api_failure_shows_safe_reasoning_error(self):
        self.server.save_settings('gateway-model', f'http://127.0.0.1:{self.http.server_port}/v1',
                                  'local-test-placeholder', protocol='chat')
        self.http.api_error = (400, {'error': {'message': 'Invalid reasoning_effort; private-fixture-key'}})
        thread = await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Hello', effort='max')
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 30)
        history = await self.server.read_thread(thread['id'])
        self.assertEqual(history['turns'][-1]['status'], 'failed')
        self.assertIn('不支持所选思考等级', json.dumps(history, ensure_ascii=False))
        self.assertNotIn('private-fixture-key', json.dumps(self.events))
        self.assertNotIn('local-test-placeholder', json.dumps(self.events))

    async def test_real_tool_and_approval_decisions(self):
        self.http.tool_enabled = True
        for allow in [True, False]:
            decisions = []
            async def emit(event):
                self.events.append(event)
                if event.get('type') == 'claude/permission/request':
                    request = event['params']
                    decisions.append(request)
                    await broker.respond(request['permissionId'], request['threadId'], allow)
            broker = PermissionPromptBroker(self.root / 'unused.sock', emit)
            self.server.enable_permission_prompt(broker)
            thread = await self.server.create_thread()
            await self.server.start_turn(thread['id'], 'Test the tool')
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 30)
            history = await self.server.read_thread(thread['id'])
            self.assertEqual(history['turns'][-1]['status'], 'completed', history)
            self.assertTrue(decisions, 'native tool request did not reach the broker')
            items = [item for item in history['turns'][-1]['items'] if item['type'] == 'dynamicToolCall']
            self.assertEqual(len(items), 1, history)
            self.assertEqual(items[0]['status'], 'completed' if allow else 'failed')
            self.assertEqual(broker.pending_requests(), [])
            await broker.close()

    async def test_stop_cancels_pending_approval_and_closes_runtime(self):
        self.http.tool_enabled = True
        received = asyncio.Event()
        async def emit(event):
            self.events.append(event)
            if event.get('type') == 'claude/permission/request':
                received.set()
        broker = PermissionPromptBroker(self.root / 'unused.sock', emit)
        self.server.enable_permission_prompt(broker)
        self.addAsyncCleanup(broker.close)
        thread = await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Wait for permission')
        tasks = tuple(self.server._tasks.values())
        await asyncio.wait_for(received.wait(), 20)
        self.assertTrue(await self.server.interrupt(thread['id']))
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        self.assertEqual(broker.pending_requests(), [])
        self.assertEqual((await self.server.read_thread(thread['id']))['turns'][-1]['status'], 'stopped')


class HarnessApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from devwisp.app import create_app
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        with patch.dict(os.environ, {'DEVWISP_AUTH_FILE': str(self.root / 'auth.json'),
                                     'DEVWISP_STATE_FILE': str(self.root / 'state.sqlite3')}):
            self.app = create_app(self.root)
        self.addCleanup(self.app.state.shared_state.close)
        self.addAsyncCleanup(self.app.state.claude_code.close)
        self.addAsyncCleanup(self.app.state.deepseek.close)
        self.app.state.codex._emit = AsyncMock()
        self.app.state.codex.notify_turn_delivery = AsyncMock()
        self.harness = self.app.state.deepseek
        self.harness.emit = AsyncMock()

    def endpoint(self, path, method=None):
        return next(route.endpoint for route in self.app.routes if getattr(route, 'path', None) == path and (method is None or method in route.methods))

    async def test_thread_routes_and_codex_failure_fallback(self):
        from devwisp.app import ThreadStartRequest, ThreadNameRequest, ThreadWorkspaceRequest, ResumeRequest
        from devwisp.codex_app_server import CodexRuntimeError
        thread = await self.endpoint('/api/v1/codex/threads', 'POST')(ThreadStartRequest(engine='deepseek-harness'))
        thread_id = thread['id']
        await self.endpoint('/api/v1/codex/threads/{thread_id}/name')(thread_id, ThreadNameRequest(name='Harness task'))
        await self.endpoint('/api/v1/codex/threads/{thread_id}/workspace')(thread_id, ThreadWorkspaceRequest(cwd=str(self.root)))
        resumed = await self.endpoint('/api/v1/codex/threads/{thread_id}/resume')(thread_id, ResumeRequest(include_history=False))
        self.assertEqual(resumed['engine'], 'deepseek-harness')
        self.assertNotIn('turns', resumed)
        self.app.state.codex.list_threads = AsyncMock(side_effect=CodexRuntimeError('offline'))
        threads = await self.endpoint('/api/v1/codex/threads')()
        self.assertIn(thread_id, [value['id'] for value in threads])
        await self.endpoint('/api/v1/codex/threads/{thread_id}')(thread_id, since=None, include_images=False, tail_turns=None)
        await self.endpoint('/api/v1/codex/threads/{thread_id}', 'DELETE')(thread_id)
        self.assertEqual(self.harness.list_threads(), [])

    async def test_direct_turn_preserves_model_effort_and_permission(self):
        from devwisp.app import TurnRequest
        thread = await self.harness.create_thread()
        self.harness.start_turn = AsyncMock(return_value='turn-test')
        await self.endpoint('/api/v1/codex/threads/{thread_id}/turns')(thread['id'], TurnRequest(
            prompt='Hello', model='custom-deepseek', effort='max', approval_policy='never', deepseek_agent_preset='ptc',
        ))
        args = self.harness.start_turn.call_args.kwargs
        self.assertEqual(args['model'], 'custom-deepseek')
        self.assertEqual(args['effort'], 'max')
        self.assertEqual(args['permission_mode'], 'bypassPermissions')
        self.assertEqual(args['deepseek_agent_preset'], 'ptc')

    async def test_question_routes_replay_thread_scope_and_resolution(self):
        from fastapi import HTTPException
        thread = await self.harness.create_thread()
        task = asyncio.create_task(self.harness.questions.ask(thread['id'], 'turn',
            [{'id': 'choice', 'question': 'Pick', 'options': [{'label': 'A'}]}], threading.Event()))
        try:
            await asyncio.sleep(0)
            pending = await self.endpoint('/api/v1/deepseek/questions')()
            request = pending['requests'][0]
            response = self.endpoint('/api/v1/deepseek/threads/{thread_id}/questions/{question_id}', 'POST')
            with self.assertRaises(HTTPException) as error:
                await response('deepseek:other', request['questionId'], {'cancelled': True})
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(await response(thread['id'], request['questionId'], {'answers': [{'id': 'choice', 'selected': ['A']}]}), {'answered': True})
            self.assertEqual((await task)['answers'][0]['selected'], ['A'])
            self.assertEqual((await self.harness.read_thread(thread['id']))['turns'][0]['items'][0]['type'], 'userMessage')
            self.assertEqual(await self.endpoint('/api/v1/deepseek/questions')(), {'requests': []})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_native_permission_forwarding_direct_steer_and_queue(self):
        from devwisp.app import TurnRequest, SteerRequest, QueueMessageIdRequest
        from test_shared_queue import message
        thread = await self.harness.create_thread()
        self.harness.start_turn = AsyncMock(return_value='turn-test')
        self.harness.steer_turn = AsyncMock(return_value='turn-test')
        for mode in ['read-only', 'workspace-write', 'full-access-ask', 'danger-full-access', 'plan']:
            await self.endpoint('/api/v1/codex/threads/{thread_id}/turns')(thread['id'], TurnRequest(prompt='Direct', deepseek_permission_mode=mode))
            self.assertEqual(self.harness.start_turn.call_args.kwargs['deepseek_permission_mode'], mode)
            await self.endpoint('/api/v1/codex/threads/{thread_id}/steer')(thread['id'], SteerRequest(prompt='Live', deepseek_permission_mode=mode, deepseek_agent_preset='ptc'))
            self.assertEqual(self.harness.steer_turn.call_args.kwargs['deepseek_permission_mode'], mode)
            self.assertEqual(self.harness.steer_turn.call_args.kwargs['deepseek_agent_preset'], 'ptc')
        payload = message('queued-native-mode', 'Plan queued'); payload['deepseekPermissionMode'] = 'plan'
        payload['deepseekAgentPreset'] = 'cordis'
        self.app.state.shared_state.enqueue(thread['id'], payload)
        await self.endpoint('/api/v1/codex/queue/{thread_id}/send-now')(thread['id'], QueueMessageIdRequest(message_id=payload['id']))
        self.assertEqual(self.harness.start_turn.call_args.kwargs['deepseek_permission_mode'], 'plan')
        self.assertEqual(self.harness.start_turn.call_args.kwargs['deepseek_agent_preset'], 'cordis')

    async def test_api_protocol_setting_persists_without_returning_key(self):
        from devwisp.app import DeepSeekSettingsRequest
        response = await self.endpoint('/api/v1/deepseek/settings')(DeepSeekSettingsRequest(
            protocol='responses', model='gateway-model', base_url='https://gateway.example/v1', api_key='test-private-key'))
        saved = json.loads(response.body)
        self.assertEqual(saved['protocol'], 'responses')
        self.assertTrue(saved['hasApiKey'])
        self.assertNotIn('test-private-key', response.body.decode())
        self.assertEqual(self.harness._settings()['protocol'], 'responses')

    async def test_profile_http_auth_cache_and_validation_redaction(self):
        async def request(path, method='GET', payload=None, authorized=True):
            content = json.dumps(payload).encode() if payload is not None else b''
            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                'http_version': '1.1', 'method': method, 'scheme': 'http', 'path': path,
                'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
                'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(content)).encode()),
                            (b'authorization', b'Bearer fixture' if authorized else b'')],
                'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 8000)}
            sent = False
            messages = []
            async def receive():
                nonlocal sent
                if sent:
                    await asyncio.Event().wait()
                sent = True
                return {'type': 'http.request', 'body': content, 'more_body': False}
            async def send(message):
                messages.append(message)
            with patch.object(self.app.state.auth, 'accepts', side_effect=lambda token: token == 'fixture'):
                await asyncio.wait_for(self.app(scope, receive, send), 3)
            start = next(m for m in messages if m['type'] == 'http.response.start')
            body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
            return start, body
        for path in ['/api/v1/deepseek/profiles', '/api/v1/deepseek/test']:
            start, _ = await request(path, authorized=False)
            self.assertEqual(start['status'], 401)
            start, body = await request(path, 'POST', {'apiKey': 'invalid-secret-key', 'protocol': 'bad'})
            self.assertEqual(start['status'], 422)
            self.assertNotIn(b'invalid-secret-key', body)
            self.assertIn(b'no-store', dict(start['headers'])[b'cache-control'])
        for path in ['/api/v1/deepseek/questions', '/api/v1/deepseek/threads/deepseek:test/questions/missing']:
            start, _ = await request(path, 'POST' if '/threads/' in path else 'GET', {'cancelled': True}, authorized=False)
            self.assertEqual(start['status'], 401)
        start, _ = await request('/api/v1/deepseek/profiles')
        self.assertEqual(start['status'], 200)
        self.assertIn(b'no-store', dict(start['headers'])[b'cache-control'])

    async def test_profile_api_crud_test_and_conflicts(self):
        from devwisp.app import DeepSeekProfileRequest, NativeEngineProfileDeleteRequest
        from fastapi import HTTPException
        config = await self.endpoint('/api/v1/deepseek/profiles', 'GET')()
        request = DeepSeekProfileRequest(name='API test', model='test-model', baseUrl='https://gateway.example',
            apiKey='api-test-secret', protocol='chat', apply=False, expectedRevision=config['profilesRevision'])
        saved = await self.endpoint('/api/v1/deepseek/profiles', 'POST')(request)
        self.assertFalse(saved['applied'])
        self.assertNotIn('api-test-secret', json.dumps(saved))
        self.app.state.codex._emit.assert_not_awaited()
        with self.assertRaises(HTTPException) as conflict:
            await self.endpoint('/api/v1/deepseek/profiles', 'POST')(request)
        self.assertEqual(conflict.exception.status_code, 409)
        test_request = request.model_copy(update={'profileId': saved['profileId'], 'apiKey': '',
            'expectedRevision': saved['config']['profilesRevision']})
        before = self.harness.settings_path.read_bytes()
        with patch('devwisp.app.check_native_connection', new_callable=AsyncMock, return_value={'ok': True, 'results': []}) as check:
            result = await self.endpoint('/api/v1/deepseek/test')(test_request)
            self.assertTrue(result['ok'])
            self.assertEqual(check.call_args.args[0]['apiKey'], 'api-test-secret')
        self.assertEqual(self.harness.settings_path.read_bytes(), before)
        with patch('devwisp.app.check_native_connection', new_callable=AsyncMock, side_effect=TimeoutError):
            with self.assertRaises(HTTPException) as timeout:
                await self.endpoint('/api/v1/deepseek/test')(test_request)
            self.assertEqual(timeout.exception.status_code, 504)
        applied = await self.endpoint('/api/v1/deepseek/profiles', 'POST')(test_request.model_copy(update={'apply': True}))
        self.app.state.codex._emit.assert_awaited_once_with({'type': 'deepseek.config.changed'})
        deleted = await self.endpoint('/api/v1/deepseek/profiles/{profile_id}', 'DELETE')('local',
            NativeEngineProfileDeleteRequest(expectedRevision=applied['config']['profilesRevision']))
        self.assertEqual(len(deleted['profiles']), 1)

    async def test_busy_harness_explicit_send_steers(self):
        from devwisp.app import QueueMessageIdRequest
        from test_shared_queue import message
        thread = await self.harness.create_thread()
        thread_id = thread['id']
        self.harness.active_turn_id = AsyncMock(return_value='running-turn')
        self.harness.read_thread = AsyncMock(return_value={'status': {'type': 'active'}})
        self.harness.steer_turn = AsyncMock(return_value='running-turn')
        self.app.state.shared_state.enqueue(thread_id, message('queued-harness', 'next'))
        response = await self.endpoint('/api/v1/codex/queue/{thread_id}/send-now')(
            thread_id, QueueMessageIdRequest(message_id='queued-harness'))
        self.assertTrue(response['sent'])
        self.harness.steer_turn.assert_awaited_once()
