from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from devwisp.local_usage import ClaudeUsage, LocalUsageStore, normalize_usage, track
from devwisp.codex_app_server import CodexAppServer
from devwisp.harness_observability import HarnessObservability


def usage(i=10, o=5, **other):
    return normalize_usage({'inputTokens': i, 'outputTokens': o, **other}, fresh_input=False)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'usage.sqlite3'
        self.store = LocalUsageStore(self.path)
        self.addCleanup(lambda: self.store.close())

    def begin(self, turn='turn', engine='codex', model='model'):
        self.store.begin(engine, 'thread', turn, model)

    def test_cache_reasoning_and_invalid_values(self):
        actual = normalize_usage({'input_tokens': 10, 'output_tokens': 7, 'cache_read_input_tokens': 20,
                                  'cache_creation_input_tokens': 3, 'reasoning_output_tokens': 4}, fresh_input=True)
        self.assertEqual(actual, dict(inputTokens=33, outputTokens=7, cacheReadTokens=20,
                                      cacheWriteTokens=3, reasoningTokens=4, totalTokens=40))
        actual = normalize_usage({'prompt_tokens': 30, 'completion_tokens': 7,
                                 'prompt_tokens_details': {'cached_tokens': 20},
                                 'completion_tokens_details': {'reasoning_tokens': 4}}, fresh_input=False)
        self.assertEqual(actual['totalTokens'], 37)
        self.assertEqual(actual['cacheReadTokens'], 20)
        for raw in (None, {}, {'inputTokens': True, 'outputTokens': -2}, {'inputTokens': 0, 'outputTokens': 0}, {'inputTokens': 1.5}, {'inputTokens': 2**60}):
            self.assertTrue(all(v is None for v in normalize_usage(raw, fresh_input=False).values()))

    def test_ownership_upserts_partial_and_stopped_consumption(self):
        self.store.record('codex', 'outside', 'external', 'sample', None, usage(999, 999))
        self.begin()
        self.store.record('codex', 'thread', 'turn', 'request', None, usage(10, None))
        self.assertEqual(self.store.query()['totals']['partialTurns'], 1)
        self.store.record('codex', 'thread', 'turn', 'request', None, usage(None, 5))
        self.store.record('codex', 'thread', 'turn', 'request', None, usage(10, 5))
        self.store.finish('codex', 'thread', 'turn', 'stopped')
        result = self.store.query()
        self.assertEqual(result['totals']['turns'], 1)
        self.assertEqual(result['totals']['totalTokens'], 15)
        self.assertEqual(result['turns'][0]['status'], 'stopped')
        self.assertEqual(result['turns'][0]['samples'], 1)
        self.assertEqual(result['totals']['partialTurns'], 0)

    def test_restart_unknown_and_interrupted(self):
        self.begin()
        self.store.record('codex', 'thread', 'turn', 'sample', None, usage())
        self.begin('missing')
        self.begin('partial')
        self.store.record('codex', 'thread', 'partial', 'sample', None, usage(4, None))
        self.store.close()
        self.store = LocalUsageStore(self.path)
        result = self.store.query()
        self.assertEqual(result['totals']['totalTokens'], 15)
        self.assertEqual(result['totals']['missingTurns'], 1)
        self.assertEqual(result['totals']['partialTurns'], 1)
        self.assertTrue(all(row['status'] == 'interrupted' for row in result['turns']))
        self.assertIsNone(next(row for row in result['turns'] if row['turnId'] == 'missing')['totalTokens'])

    def test_scope_selection_model_filter_and_pagination(self):
        for n in range(35):
            self.begin(str(n), model='requested')
            self.store.record('codex', 'thread', str(n), 'request', 'actual', usage())
            self.store.record('codex', 'thread', str(n), 'result', 'actual', usage(20, 5), scope='summary')
            self.store.finish('codex', 'thread', str(n), 'completed')
        result = self.store.query(model='actual', offset=30, limit=30)
        self.assertEqual(result['totals']['totalTokens'], 35 * 25)
        self.assertEqual(result['models'][0]['turns'], 35)
        self.assertEqual(len(result['turns']), 5)
        self.assertEqual(self.store.query(model='requested')['totals']['turns'], 0)
        self.assertEqual(self.store.query(engine='claude-code')['totals']['turns'], 0)
        self.assertEqual(self.store.query(since=result['turns'][0]['startedAt'] + 10000)['totals']['turns'], 0)
        self.assertEqual(sum(row['totalTokens'] for row in result['daily']), 875)

    def test_smaller_summary_does_not_drop_child_requests(self):
        self.begin()
        for sample in ('root', 'child'):
            self.store.record('codex', 'thread', 'turn', sample, 'actual', usage())
        self.store.record('codex', 'thread', 'turn', 'summary', 'actual', usage(), scope='summary')
        self.assertEqual(self.store.query()['totals']['totalTokens'], 30)

    def test_conversation_filter_isolates_threads_and_engines(self):
        for engine, thread, tokens in [('codex', 'a', 10), ('codex', 'b', 100), ('claude-code', 'a', 1000), ('deepseek-harness', 'c', 10000)]:
            self.store.begin(engine, thread, 'turn', 'actual')
            self.store.record(engine, thread, 'turn', 'sample', None, usage(tokens, 1))
        result = self.store.query(thread_id='a', engine='codex')
        self.assertEqual(result['threadId'], 'a')
        self.assertEqual(result['totals']['totalTokens'], 11)
        self.assertEqual(result['totals']['turns'], 1)
        self.assertEqual({row['threadId'] for row in result['turns']}, {'a'})
        self.assertEqual(self.store.query(thread_id='unknown')['totals']['turns'], 0)
        project = self.store.query()
        self.assertIsNone(project['threadId'])
        self.assertEqual(project['totals']['totalTokens'], 11114)
        self.assertEqual(project['totals']['turns'], 4)

    def test_codex_replay_identical_calls_and_cross_turn_baseline(self):
        self.begin()
        first = {'total': usage(10, 5), 'last': usage(10, 5)}
        self.store.codex('thread', 'turn', first)
        self.store.codex('thread', 'turn', first)
        second = {'total': usage(20, 10), 'last': usage(10, 5)}
        self.store.codex('thread', 'turn', second)
        self.assertEqual(self.store.query()['totals']['totalTokens'], 30)
        self.begin('next')
        self.store.codex('thread', 'next', second)  # replay on reconnection
        self.store.codex('thread', 'next', {'total': usage(30, 15), 'last': usage(10, 5)})
        self.assertEqual(self.store.query()['totals']['totalTokens'], 45)

    def test_codex_cumulative_baseline_zero_reset_and_external_history(self):
        self.store.codex('thread', None, {'total': {'inputTokens': 0, 'outputTokens': 0}})
        self.begin()
        self.store.codex('thread', 'turn', {'total': usage(10, 5)})
        self.assertEqual(self.store.query()['totals']['totalTokens'], 15)
        self.store.codex('thread', 'turn', {'total': usage(2, 1)})  # reset: no negative consumption
        self.store.codex('thread', 'turn', {'total': usage(4, 2)})
        self.assertEqual(self.store.query()['totals']['totalTokens'], 18)
        self.store.codex('other', 'external', {'total': usage(1000, 500), 'last': usage()})
        self.assertEqual(self.store.query()['totals']['turns'], 1)
        self.store.begin('codex', 'resumed', 'owned')
        self.store.codex('resumed', 'owned', {'total': usage(1000, 500)})
        self.assertIsNone(next(r for r in self.store.query()['turns'] if r['threadId'] == 'resumed')['totalTokens'])

    def test_codex_partial_usage_survives_runtime_disconnect(self):
        self.begin()
        self.store.codex('thread', 'turn', {'last': {'inputTokens': 7}})
        self.store.interrupt('codex')
        result = self.store.query()
        self.assertEqual(result['totals']['inputTokens'], 7)
        self.assertEqual(result['totals']['partialTurns'], 1)
        self.assertEqual(result['totals']['activeTurns'], 0)
        self.assertEqual(result['turns'][0]['status'], 'interrupted')


class CollectorsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = LocalUsageStore(self.root / 'usage.sqlite3')
        self.addCleanup(self.store.close)

    async def test_committed_changes_publish_once_and_revision_survives_restart(self):
        events = []
        async def changed(engine, thread_id):
            # The callback must run after commit, outside the writer thread.
            with sqlite3.connect(self.store.path) as reader:
                revision = reader.execute("SELECT value FROM usage_meta WHERE key='revision'").fetchone()[0]
            snapshot = self.store.query(engine=engine, thread_id=thread_id)
            self.assertEqual(snapshot['revision'], revision)
            events.append((engine, thread_id, revision, snapshot['totals']['totalTokens']))
        self.store.on_change = changed
        await track(self.store, 'begin', 'codex', 'thread', 'turn')
        await track(self.store, 'begin', 'codex', 'thread', 'turn')
        await track(self.store, 'record', 'codex', 'thread', 'turn', 'sample', None, usage())
        await track(self.store, 'record', 'codex', 'thread', 'turn', 'sample', None, usage())
        await track(self.store, 'record', 'codex', 'foreign', 'turn', 'sample', None, usage(999, 1))
        await track(self.store, 'finish', 'codex', 'thread', 'turn', 'completed')
        await track(self.store, 'finish', 'codex', 'thread', 'turn', 'completed')
        self.assertEqual(len(events), 3)
        self.assertEqual([e[3] for e in events], [None, 15, 15])
        self.assertEqual([e[2] for e in events], sorted(set(e[2] for e in events)))
        # Reopen only after all turns have finished, preserving the revision.
        reopened = LocalUsageStore(self.store.path)
        try:
            self.assertEqual(reopened.query()['revision'], events[-1][2])
        finally:
            reopened.close()

    async def test_codex_child_and_disconnect_publish_owner_and_affected_threads(self):
        self.store.on_change = AsyncMock()
        await track(self.store, 'begin', 'codex', 'parent', 'turn')
        await track(self.store, 'begin', 'codex', 'other', 'turn')
        self.store.on_change.reset_mock()
        packet = {'last': usage(), 'total': usage()}
        await track(self.store, 'codex', 'child', 'native-turn', packet, owner=('parent', 'turn'))
        self.store.on_change.assert_awaited_once_with('codex', 'parent')
        await track(self.store, 'codex', 'child', 'native-turn', packet, owner=('parent', 'turn'))
        self.store.on_change.assert_awaited_once()
        self.store.on_change.reset_mock()
        await track(self.store, 'interrupt', 'codex')
        self.assertEqual({call.args for call in self.store.on_change.await_args_list}, {('codex', 'parent'), ('codex', 'other')})

    async def test_publication_failure_keeps_committed_usage_and_chat_alive(self):
        self.store.begin('codex', 'thread', 'turn')
        self.store.on_change = AsyncMock(side_effect=RuntimeError('fixture publication failure'))
        with self.assertLogs('devwisp.local_usage', level='ERROR'):
            await track(self.store, 'record', 'codex', 'thread', 'turn', 'sample', None, usage())
        self.assertEqual(self.store.query()['totals']['totalTokens'], 15)

    async def test_claude_stream_final_replay_and_cumulative_results(self):
        self.store.begin('claude-code', 'thread', 'turn')
        c = ClaudeUsage(self.store, 'thread', 'turn', 'requested')
        await c.observe({'type': 'stream_event', 'event': {'type': 'message_start', 'message': {
            'id': 'm1', 'model': 'actual', 'usage': {'input_tokens': 10, 'cache_read_input_tokens': 20, 'output_tokens': 0}}}})
        await c.observe({'type': 'stream_event', 'event': {'type': 'message_delta', 'usage': {'output_tokens': 5}}})
        final = {'type': 'assistant', 'message': {'id': 'm1', 'model': 'actual', 'usage': {
            'input_tokens': 10, 'cache_read_input_tokens': 20, 'output_tokens': 5}}}
        await c.observe(final)
        await c.observe(final)
        self.assertEqual(self.store.query()['totals']['totalTokens'], 35)
        for n in (1, 2, 2):
            await c.observe({'type': 'result', 'uuid': f'r{n}', 'modelUsage': {'actual': {
                'inputTokens': 10*n, 'cacheReadInputTokens': 20*n, 'outputTokens': 5*n}}})
        result = self.store.query()
        self.assertEqual(result['totals']['totalTokens'], 70)
        self.assertEqual(result['totals']['cacheReadTokens'], 40)
        self.assertEqual(result['models'][0]['model'], 'actual')

    async def test_claude_result_fallback_and_children(self):
        self.store.begin('claude-code', 'thread', 'turn')
        c = ClaudeUsage(self.store, 'thread', 'turn', 'actual')
        for mid, parent in [('root', None), ('child', 'tool-1')]:
            await c.observe({'type': 'assistant', 'parent_tool_use_id': parent, 'message': {
                'id': mid, 'usage': {'input_tokens': 10, 'output_tokens': 5}}})
        await c.observe({'type': 'result', 'parent_tool_use_id': 'tool-1', 'usage': {'input_tokens': 999, 'output_tokens': 1}})
        await c.observe({'type': 'result', 'uuid': 'r', 'usage': {'input_tokens': 10, 'output_tokens': 5}})
        self.assertEqual(self.store.query()['totals']['totalTokens'], 30)
        self.store.begin('claude-code', 'thread', 'fallback')
        c = ClaudeUsage(self.store, 'thread', 'fallback', 'actual')
        await c.observe({'type': 'result', 'uuid': 'fallback', 'usage': {'input_tokens': 8, 'output_tokens': 2}})
        self.assertEqual(self.store.query()['totals']['totalTokens'], 40)

    async def test_codex_native_ingress_owns_only_requested_turns_and_descendants(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}):
            server = CodexAppServer('codex', self.root)
        server._usage_store = self.store
        server._usage_models['thread'] = 'actual'
        server._schedule_runtime_thread_sync = Mock()
        async def emit(method, **params):
            await server._handle_message({'method': method, 'params': params})
        server._pending_turn_starts['thread'] = asyncio.get_running_loop().create_future()
        await emit('turn/started', threadId='thread', turn={'id': 'owned'})
        await emit('thread/tokenUsage/updated', threadId='thread', turnId='owned', tokenUsage={'last': usage(), 'total': usage()})
        await emit('thread/started', thread={'id': 'child', 'model': 'child-model', 'source': {'subAgent': {'thread_spawn': {'parent_thread_id': 'thread', 'depth': 1}}}})
        await emit('turn/started', threadId='child', turn={'id': 'child-turn'})
        await emit('thread/tokenUsage/updated', threadId='child', turnId='child-turn', tokenUsage={'last': usage(2, 1), 'total': usage(2, 1)})
        await emit('turn/started', threadId='external', turn={'id': 'foreign'})
        await emit('thread/tokenUsage/updated', threadId='external', turnId='foreign', tokenUsage={'last': usage(999, 1), 'total': usage(999, 1)})
        await emit('turn/completed', threadId='thread', turn={'id': 'owned', 'status': 'interrupted'})
        result = self.store.query()
        self.assertEqual(result['totals']['turns'], 1)
        self.assertEqual(result['totals']['totalTokens'], 18)
        self.assertEqual(result['turns'][0]['status'], 'interrupted')
        self.assertEqual({r['model'] for r in result['models']}, {'actual', 'child-model'})
        await server.close()

    async def test_harness_owned_descendants_attempts_and_direct_message_model(self):
        for turn in ('one', 'two'):
            self.store.begin('deepseek-harness', 'thread', turn, 'requested')
            p = HarnessObservability('root')
            p.observe('subagent.started', {'childSessionId': 'foreign', 'parentSessionId': 'unrelated'})
            p.observe('subagent.started', {'childSessionId': 'child', 'parentSessionId': 'root'})
            for sid in ('root', 'child', 'foreign'):
                p.observe('session.stream', {'sessionId': sid, 'frame': {'type': 'start'}})
                p.observe('session.event', {'sessionId': sid, 'event': {'type': 'assistant/message', 'data': {
                    'message': {'source': {'model': sid+'-model'}}, 'usage': {'inputTokens': 8, 'cacheReadTokens': 2, 'outputTokens': 5}}}})
            for key, model, raw in p.usage_records():
                self.store.record('deepseek-harness', 'thread', turn, key, model, normalize_usage(raw, fresh_input=True))
        result = self.store.query()
        self.assertEqual(result['totals']['totalTokens'], 60)
        self.assertEqual({r['model'] for r in result['models']}, {'root-model', 'child-model'})

    async def test_storage_failure_does_not_fail_chat(self):
        with patch.object(self.store, 'record', side_effect=OSError('fixture')), self.assertLogs('devwisp.local_usage', level='ERROR'):
            self.assertIsNone(await track(self.store, 'record', 'codex', 'thread', 'turn', 'sample', None, usage()))


async def asgi_get(app, path, token=''):
    """Exercise authentication/middleware/validation without a network server."""
    url, _, query = path.partition('?')
    messages = []
    sent = False
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
    await app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1',
               'method': 'GET', 'scheme': 'http', 'path': url, 'raw_path': url.encode(), 'query_string': query.encode(),
               'root_path': '', 'headers': [(b'authorization', f'Bearer {token}'.encode())],
               'client': ('127.0.0.1', 1), 'server': ('test', 80)}, receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return start['status'], dict(start['headers']), json.loads(body)


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_validation_filters_pagination_and_private_cache(self):
        from devwisp.app import create_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'
            runtime.mkdir()
            with patch.dict(os.environ, {'DEVWISP_AUTH_FILE': str(root / 'auth.json'), 'DEVWISP_STATE_FILE': str(root / 'state.sqlite3'), 'CODEX_HOME': str(root / 'codex')}), patch('devwisp.app.tempfile.mkdtemp', return_value=str(runtime)):
                app = create_app(root)
            try:
                path = '/api/v1/codex/usage/local'
                self.assertEqual((await asgi_get(app, path))[0], 401)
                token = app.state.auth.pair(app.state.auth.code)
                store = app.state.local_usage
                for n in range(3):
                    store.begin('codex', 'thread', str(n), 'model')
                    store.record('codex', 'thread', str(n), 'sample', None, usage())
                status, headers, body = await asgi_get(app, path+'?engine=codex&model=model&offset=2&limit=1', token)
                self.assertEqual(status, 200)
                self.assertEqual(headers[b'cache-control'], b'private, no-store')
                self.assertEqual(body['totals']['totalTokens'], 45)
                queue = await app.state.codex.subscribe()
                await track(store, 'record', 'codex', 'thread', '0', 'sample', None, usage(20, 5))
                event = await asyncio.wait_for(queue.get(), 1)
                self.assertEqual(event['method'], 'usage/local/updated')
                self.assertEqual(event['params']['threadId'], 'thread')
                self.assertEqual(event['params']['totals']['totalTokens'], 55)
                self.assertEqual(event['params']['revision'], store.query()['revision'])
                app.state.codex.unsubscribe(queue)
                self.assertEqual(len(body['turns']), 1)
                self.assertEqual((await asgi_get(app, path+'?engine=claude-code', token))[2]['totals']['turns'], 0)
                self.assertEqual((await asgi_get(app, path+'?thread_id=thread', token))[2]['totals']['totalTokens'], 55)
                self.assertEqual((await asgi_get(app, path+'?thread_id=other', token))[2]['totals']['turns'], 0)
                for query in ('limit=101', 'offset=-1', 'since=20&until=10', 'engine=other', 'offset_minutes=9999'):
                    self.assertEqual((await asgi_get(app, path+'?'+query, token))[0], 422)
            finally:
                await app.state.deepseek.close()
                await app.state.claude_code.close()
                await app.state.codex.close()
                app.state.local_usage.close()
                app.state.shared_state.close()
