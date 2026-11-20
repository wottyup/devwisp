from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from devwisp.harness_observability import HarnessObservability
from devwisp.local_usage import LocalUsageStore
from devwisp.deepseek_harness import harness_failure_message
from test_deepseek_harness import _HarnessFixture, _ModelEndpoint


class ProjectionTests(unittest.TestCase):
    def test_reasoning_usage_attempts_children_and_context_are_separate(self):
        p = HarnessObservability('root')
        def frame(sid, chunk=None, kind='chunk'):
            p.observe('session.stream', {'sessionId': sid, 'frame': {'type': kind, 'chunk': chunk or {}}})
        frame('root', kind='start')
        frame('root', {'type': 'reasoning-delta', 'text': 'Think once.'})
        usage = {'inputTokens': 8, 'outputTokens': 5, 'cacheReadTokens': 2, 'totalTokens': 15, 'reasoningTokens': 3}
        frame('root', {'type': 'usage', 'usage': usage})
        frame('root', {'type': 'usage', 'usage': usage})  # cumulative update, not a new call
        p.observe('session.event', {'sessionId': 'root', 'event': {'type': 'assistant/message', 'data': {
            'message': {'content': [{'type': 'reasoning', 'text': 'Think once.'}]},
            'stream': [{'type': 'chunk', 'chunk': {'type': 'usage', 'usage': usage}}]}}})
        p.observe('subagent.started', {'parentSessionId': 'unrelated', 'childSessionId': 'foreign'})
        p.observe('subagent.started', {'parentSessionId': 'root', 'childSessionId': 'child'})
        frame('child', kind='start')
        frame('child', {'type': 'text-delta', 'text': 'Child answer'})
        frame('child', {'type': 'usage', 'usage': usage})
        frame('root', kind='start')
        frame('root', {'type': 'usage', 'usage': {'inputTokens': 1, 'outputTokens': 2, 'totalTokens': 3}})
        p.observe('session.context', {'sessionId': 'root', 'pressureTokens': 10, 'contextWindow': 1000})
        p.observe('subagent.finished', {'parentSessionId': 'root', 'childSessionId': 'child', 'status': 'completed'})
        out = p.snapshot()
        self.assertEqual(out['reasoning'], 'Think once.')
        self.assertEqual(out['usage'], {'inputTokens': 9, 'outputTokens': 7, 'totalTokens': 18})
        self.assertEqual(out['children'][0]['text'], 'Child answer')
        self.assertEqual(out['children'][0]['status'], 'completed')
        self.assertEqual(out['children'][0]['usage']['totalTokens'], 15)
        self.assertEqual(len(out['children']), 1)
        self.assertEqual(out['context'], {'pressureTokens': 10, 'contextWindow': 1000})

    def test_missing_or_invalid_counters_are_not_estimated(self):
        p = HarnessObservability('root')
        for value in (None, {'inputTokens': True, 'outputTokens': 1}, {'inputTokens': -1, 'outputTokens': 2}, {'inputTokens': 1.5, 'outputTokens': 2}, {'inputTokens': 0, 'outputTokens': 0, 'totalTokens': 0}, {'inputTokens': 10**500, 'outputTokens': 2}):
            p.observe('session.stream', {'sessionId': 'root', 'frame': {'type': 'chunk', 'chunk': {'type': 'usage', 'usage': value}}})
        self.assertEqual(p.snapshot()['usage'], {})
        self.assertEqual(p.snapshot()['reasoning'], '')

    def test_safe_error_diagnostics(self):
        for code, expected in [('CONTEXT_LENGTH_EXCEEDED', '上下文'), ('ETIMEDOUT', '超时'), ('NETWORK_ERROR', '连接失败')]:
            result = harness_failure_message({'error': {'code': code, 'message': 'private-fixture-secret'}})
            self.assertIn(expected, result)
            self.assertNotIn('private-fixture-secret', result)


@unittest.skipUnless(importlib.util.find_spec('deepseek_harness'), 'Harness not installed')
class NativeContinuityTests(_HarnessFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), _ModelEndpoint)
        self.http.paths, self.http.requests, self.http.request_headers = [], [], []
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.addCleanup(self.http_thread.join, 2)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.server.save_settings('fixture-model', f'http://127.0.0.1:{self.http.server_port}', 'fixture-placeholder', protocol='chat')

    async def turn(self, thread_id, prompt, **kwargs):
        await self.server.start_turn(thread_id, prompt, **kwargs)
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        history = await self.server.read_thread(thread_id)
        self.assertEqual(history['turns'][-1]['status'], 'completed', history)
        return history

    async def test_native_shell_state_is_retained_and_threads_are_isolated(self):
        self.http.tool_enabled = self.http.tools_each_turn = True
        self.http.tool_arguments = {'command': 'export DEVWISP_FIXTURE_STATE=retained-shell-marker'}
        first = await self.server.create_thread()
        await self.turn(first['id'], 'Set shell state', deepseek_agent_preset='minimal', deepseek_permission_mode='danger-full-access')
        lease = self.server._runtimes[first['id']]
        process = lease.runtime.client._proc
        self.http.tool_arguments = {'command': 'printf "%s" "$DEVWISP_FIXTURE_STATE"'}
        history = await self.turn(first['id'], 'Read shell state', deepseek_permission_mode='danger-full-access')
        self.assertIs(self.server._runtimes[first['id']], lease)
        self.assertIs(lease.runtime.client._proc, process)
        self.assertIn('retained-shell-marker', json.dumps(history['turns'][-1]))
        other = await self.server.create_thread()
        other_history = await self.turn(other['id'], 'Independent shell', deepseek_agent_preset='minimal', deepseek_permission_mode='danger-full-access')
        self.assertNotIn('retained-shell-marker', json.dumps(other_history))
        await self.server.delete_thread(first['id'])
        self.assertTrue(lease.closed.is_set())
        self.assertIsNotNone(process.poll())

    async def test_native_reasoning_usage_reconnect_history_and_config_reset(self):
        ledger = LocalUsageStore(self.server.database_path.parent / 'usage.sqlite3')
        self.addCleanup(ledger.close)
        self.server._usage_store = ledger
        self.http.reasoning_enabled = self.http.usage_enabled = True
        thread = await self.server.create_thread()
        history = await self.turn(thread['id'], 'Reason')
        telemetry = next(item for item in history['turns'][-1]['items'] if item['type'] == 'harnessTelemetry')
        self.assertEqual(telemetry['reasoning'], 'Fixture reasoning.')
        self.assertEqual(telemetry['usage']['totalTokens'], 15)
        self.assertEqual(telemetry['usage']['outputTokens'], 5)
        old = self.server._runtimes[thread['id']]
        await self.turn(thread['id'], 'Another turn')
        self.assertIs(self.server._runtimes[thread['id']], old)
        self.server.save_settings('new-fixture-model', f'http://127.0.0.1:{self.http.server_port}', None, protocol='chat')
        history = await self.turn(thread['id'], 'Changed configuration')
        self.assertTrue(old.closed.is_set())
        self.assertIsNot(self.server._runtimes[thread['id']], old)
        self.assertEqual(len(history['turns']), 3)
        self.assertEqual(next(item for item in history['turns'][-1]['items'] if item['type'] == 'harnessTelemetry')['continuity'], 'restored')
        self.assertEqual(self.http.requests[-1]['model'], 'new-fixture-model')
        self.assertNotIn('fixture-placeholder', json.dumps(self.events))
        recorded = ledger.query()
        self.assertEqual(recorded['totals']['turns'], 3)
        self.assertEqual(recorded['totals']['totalTokens'], 45)
        self.assertEqual(recorded['totals']['cacheReadTokens'], 6)
        self.assertTrue(all(row['status'] == 'completed' for row in recorded['turns']))

    async def test_failure_closes_cached_runtime_and_history_still_resumes(self):
        thread = await self.server.create_thread()
        await self.turn(thread['id'], 'First')
        lease = self.server._runtimes[thread['id']]
        self.http.api_error = (401, {'error': {'message': 'private-fixture-secret'}})
        await self.server.start_turn(thread['id'], 'Failure')
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        self.assertTrue(lease.closed.is_set())
        self.assertNotIn(thread['id'], self.server._runtimes)
        history = await self.server.read_thread(thread['id'])
        self.assertEqual(history['turns'][-1]['status'], 'failed')
        self.assertNotIn('private-fixture-secret', json.dumps(history))
        self.http.api_error = None
        history = await self.turn(thread['id'], 'Recover existing history')
        self.assertEqual(len(history['turns']), 3)

    async def test_native_subagent_activity_is_separate_and_completes(self):
        # Local model fixture, no real external inference or delegated work.
        self.http.tool_enabled = True
        self.http.tool_name = 'subagent'
        self.http.exclude_tool_prompt = 'CHILD_FIXTURE_ONLY'
        self.http.tool_arguments = {'name': 'fixture', 'description': 'Local child fixture', 'prompt': 'CHILD_FIXTURE_ONLY return the fixture response'}
        thread = await self.server.create_thread()
        history = await self.turn(thread['id'], 'Run the local child fixture')
        telemetry = next(item for item in history['turns'][-1]['items'] if item['type'] == 'harnessTelemetry')
        self.assertEqual(len(telemetry['children']), 1, telemetry)
        child = telemetry['children'][0]
        self.assertEqual(child['status'], 'completed', telemetry)
        self.assertIn('Hello Harness', child['text'])
        self.assertTrue(any(event.get('method') == 'deepseek/turn/telemetry' and event['params']['telemetry']['children'] for event in self.events))
        self.assertNotIn(child['id'], next(item['text'] for item in history['turns'][-1]['items'] if item['type'] == 'agentMessage'))

    async def test_reused_runtime_questions_bind_to_the_current_turn(self):
        self.http.tool_enabled = self.http.tools_each_turn = True
        self.http.tool_name = 'ask_user_question'
        self.http.tool_arguments = {'questions': [{'id': 'choice', 'question': 'Choose', 'options': [{'label': 'Yes'}]}]}
        received = []
        async def emit(event):
            self.events.append(event)
            if event.get('type') == 'deepseek/question/request':
                request = event['params']
                received.append(request)
                self.server.questions.respond(request['threadId'], request['questionId'], {'answers': [
                    {'id': request['questions'][0]['id'], 'selected': ['Yes']}]})
        self.server.questions.emit = emit
        thread = await self.server.create_thread()
        await self.turn(thread['id'], 'First question')
        lease = self.server._runtimes[thread['id']]
        await self.turn(thread['id'], 'Second question')
        self.assertIs(self.server._runtimes[thread['id']], lease)
        self.assertEqual(len(received), 2)
        self.assertNotEqual(received[0]['turnId'], received[1]['turnId'])
        self.assertEqual(self.server.questions.requests(), [])

    async def test_stop_of_second_turn_closes_process_and_releases_callbacks(self):
        thread = await self.server.create_thread()
        await self.turn(thread['id'], 'First')
        lease = self.server._runtimes[thread['id']]
        process = lease.runtime.client._proc
        self.http.hold_first = threading.Event()
        self.http.requests.clear()
        self.addCleanup(self.http.hold_first.set)
        await self.server.start_turn(thread['id'], 'Second')
        tasks = tuple(self.server._tasks.values())
        for _ in range(100):
            if self.http.requests:
                break
            await asyncio.sleep(.05)
        self.assertTrue(self.http.requests)
        await self.server.interrupt(thread['id'])
        await asyncio.wait_for(asyncio.gather(*tasks), 15)
        self.http.hold_first.set()
        self.assertTrue(lease.closed.is_set())
        self.assertIsNotNone(process.poll())
        self.assertIsNone(lease.callbacks)
        self.assertFalse(self.server._runtimes)
