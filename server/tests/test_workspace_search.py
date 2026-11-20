from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from devwisp import workspace_search as search
from devwisp.app import create_app, WorkspaceSearchRequest, WorkspaceReplaceRequest, workspace_file_preview
from devwisp.workspace_search_stream import workspace_operation_stream


class WorkspaceSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()

    def assert_no_staging_files(self):
        self.assertFalse(list(self.base.rglob('.devwisp-replace-*')))

    def test_streamed_unicode_matches_and_replacements_equal_literal_reference(self):
        randomizer = random.Random(37)
        source = '\ufeff' + ''.join(randomizer.choice(['你好', 'ab', 'aa', '[x]', 'İ', 'i', '🙂', '\r\n', '\r', '\n', ' ']) for _ in range(150))
        target = self.base / 'unicode.txt'
        for chunk_size in (1, 2, 7, 32, 65536):
            for query in ('你好', 'aa', '[x]', 'i', '🙂'):
                with self.subTest(chunk_size=chunk_size, query=query), patch.object(search, 'TEXT_CHUNK_SIZE', chunk_size):
                    target.write_bytes(source.encode())
                    result = search.search_workspace(self.base, query, False)
                    normalized = source.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
                    pattern = re.compile(re.escape(query), re.IGNORECASE)
                    expected = []
                    for number, line in enumerate(normalized.split('\n'), 1):
                        expected.extend((number, match.start()+1, match.group()) for match in pattern.finditer(line))
                    self.assertEqual(result['matchCount'], len(expected))
                    actual = [(m['line'], m['column'], m['text']) for m in result['files'][0]['matches']]
                    self.assertEqual(actual, expected)
                    replacement = '改$1\\'
                    outcome = search.replace_workspace_all(self.base, query, replacement, False)
                    self.assertEqual(outcome['matchCount'], len(expected))
                    self.assertEqual(target.read_bytes(), ('\ufeff'+pattern.sub(lambda _: replacement, source[1:])).encode())
                    self.assert_no_staging_files()

    def test_complete_totals_and_replacement_beyond_both_display_caps(self):
        for i in range(115):
            (self.base / f'{i}.txt').write_text('needle ' * 12)
        result = search.search_workspace(self.base, 'needle', True)
        self.assertEqual(result['fileCount'], 115)
        self.assertEqual(result['matchCount'], 1380)
        self.assertTrue(result['displayTruncated'])
        self.assertFalse(result['truncated'])
        self.assertLessEqual(sum(len(f['matches']) for f in result['files']), 1000)
        self.assertLessEqual(len(result['files']), 100)
        outcome = search.replace_workspace_all(self.base, 'needle', 'done', True)
        self.assertEqual(len(outcome['paths']), 115)
        self.assertEqual(outcome['matchCount'], 1380)
        self.assertTrue(all(p.read_text() == 'done ' * 12 for p in self.base.glob('*.txt')))
        self.assert_no_staging_files()

    def test_large_text_exceeds_editor_and_old_aggregate_limits(self):
        target = self.base / 'large.txt'
        block = b'x' * (1024*1024) + b'\n'
        with target.open('wb') as file:
            for _ in range(65):
                file.write(block)
            file.write(b'needle\r\n')
        with self.assertRaises(HTTPException):
            workspace_file_preview(self.base, target.name)
        result = search.search_workspace(self.base, 'needle', True)
        self.assertEqual(result['matchCount'], 1)
        self.assertEqual(result['skippedFiles'], 0)
        self.assertFalse(result['truncated'])
        outcome = search.replace_workspace_all(self.base, 'needle', 'replacement', True)
        self.assertEqual(outcome['matchCount'], 1)
        with target.open('rb') as file:
            file.seek(-13, 2)
            self.assertEqual(file.read(), b'replacement\r\n')
        self.assert_no_staging_files()

    def test_more_than_twenty_thousand_entries_and_elapsed_budget_complete(self):
        for i in range(20005):
            (self.base / f'{i:05}.txt').touch()
        (self.base / 'final.txt').write_text('needle')
        clock = iter(range(0, 10000000, 10))
        with patch.object(search.time, 'monotonic', side_effect=lambda: next(clock)):
            result = search.search_workspace(self.base, 'needle', True)
        self.assertEqual(result['scannedFiles'], 20006)
        self.assertEqual(result['matchCount'], 1)
        self.assertFalse(result['truncated'])

    def test_skips_dependencies_binary_non_utf8_and_symlinks(self):
        for directory in ('node_modules', '.git', '.kotlin', 'build', '.pnpm'):
            folder = self.base / directory
            folder.mkdir()
            (folder / 'ignored.txt').write_text('needle')
        (self.base / 'binary').write_bytes(b'needle\x00')
        (self.base / 'encoded').write_bytes(b'needle\xff')
        (self.base / 'text').write_text('needle')
        (self.base / 'link').symlink_to(self.base / 'text')
        result = search.search_workspace(self.base, 'needle', True)
        self.assertEqual(result['skippedFiles'], 2)
        self.assertEqual(result['matchCount'], 1)
        self.assertEqual([f['path'] for f in result['files']], ['text'])

    def test_single_file_preflight_conflict_leaves_all_originals(self):
        for name in ('a.txt', 'b.txt'):
            (self.base / name).write_bytes(b'\xef\xbb\xbfneedle\r\nneedle\r\n')
        files = search.search_workspace(self.base, 'needle', True)['files']
        files[-1]['matchCount'] += 1
        with self.assertRaises(HTTPException) as error:
            search.replace_workspace(self.base, 'needle', '', True, files)
        self.assertEqual(error.exception.status_code, 409)
        self.assertTrue(all(p.read_bytes() == b'\xef\xbb\xbfneedle\r\nneedle\r\n' for p in self.base.glob('*.txt')))
        self.assert_no_staging_files()

    def test_stale_revision_and_unsafe_paths_are_rejected(self):
        target = self.base / 'text'
        target.write_text('needle')
        entry = search.search_workspace(self.base, 'needle', True)['files'][0]
        target.write_text('new needle')
        with self.assertRaises(HTTPException):
            search.replace_workspace(self.base, 'needle', 'done', True, [entry])
        self.assertEqual(target.read_text(), 'new needle')
        for path in ('../text', '/etc/passwd', 'a/../text'):
            with self.subTest(path=path), self.assertRaises(HTTPException):
                search.replace_workspace(self.base, 'needle', 'done', True, [{**entry, 'path': path}])
        self.assert_no_staging_files()

    def test_invalid_replacement_cannot_turn_text_into_binary(self):
        target = self.base / 'text'
        target.write_text('needle')
        for replacement in ('\x00', '\ud800'):
            with self.subTest(replacement=repr(replacement)), self.assertRaises(HTTPException):
                search.replace_workspace_all(self.base, 'needle', replacement, True)
            self.assertEqual(target.read_text(), 'needle')
        self.assert_no_staging_files()

    def test_preflight_cancellation_cleans_disk_without_committing(self):
        for name in ('a', 'b'):
            (self.base / name).write_text('needle')
        cancel = threading.Event()
        original = search._prepare_file
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            cancel.set()
            return result
        with patch.object(search, '_prepare_file', side_effect=prepare), self.assertRaises(search.WorkspaceOperationCancelled):
            search.replace_workspace_all(self.base, 'needle', 'done', True, cancel=cancel)
        self.assertTrue(all(p.read_text() == 'needle' for p in self.base.iterdir()))
        self.assert_no_staging_files()

    def test_write_conflict_reports_partial_outcome_and_cleans_staging(self):
        for name in ('a', 'b'):
            (self.base / name).write_text('needle')
        original = search.os.replace
        def save(source, destination):
            original(source, destination)
            remaining = self.base / ('b' if Path(destination).name == 'a' else 'a')
            remaining.write_text('external change')
        with patch.object(search.os, 'replace', side_effect=save):
            outcome = search.replace_workspace_all(self.base, 'needle', 'done', True)
        self.assertEqual(len(outcome['paths']), 1)
        self.assertEqual(len(outcome['errors']), 1)
        self.assertEqual(outcome['matchCount'], 1)
        self.assert_no_staging_files()

    def test_disconnect_after_first_write_finishes_prepared_operation(self):
        for name in ('a', 'b'):
            (self.base / name).write_text('needle')
        cancel = threading.Event()
        original = search.os.replace
        def save(source, destination):
            original(source, destination)
            cancel.set()
        with patch.object(search.os, 'replace', side_effect=save):
            outcome = search.replace_workspace_all(self.base, 'needle', '', True, cancel=cancel)
        self.assertEqual(len(outcome['paths']), 2)
        self.assertTrue(all(p.read_text() == '' for p in self.base.iterdir()))
        self.assert_no_staging_files()

    def test_preserves_permissions_and_reports_progress(self):
        target = self.base / 'text'
        target.write_text('needle\r\nneedle\r')
        target.chmod(0o640)
        updates = []
        outcome = search.replace_workspace_all(self.base, 'needle', 'done', True, progress=updates.append)
        self.assertEqual(outcome['matchCount'], 2)
        self.assertEqual(target.read_bytes(), b'done\r\ndone\r')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(updates[-1]['phase'], 'write')
        self.assertEqual(updates[-1]['processedFiles'], 1)


class WorkspaceStreamTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, operation):
        response = workspace_operation_stream(operation)
        return [json.loads(chunk) async for chunk in response.body_iterator]

    async def test_completion_cannot_be_lost_when_progress_is_coalesced(self):
        def operation(*, progress, cancel):
            for i in range(100):
                progress({'phase':'scan', 'scannedFiles':i})
            return {'matchCount':72}
        messages = await self.collect(operation)
        self.assertEqual(messages[-1], {'type':'done', 'result':{'matchCount':72}})

    async def test_stream_reports_http_errors(self):
        def operation(**kwargs):
            raise HTTPException(409, '文件已变化')
        self.assertEqual((await self.collect(operation))[-1], {'type':'error','status':409,'detail':'文件已变化'})

    async def test_disconnect_cooperatively_stops_worker(self):
        stopped = threading.Event()
        def operation(*, progress, cancel):
            cancel.wait(2)
            stopped.set()
            raise search.WorkspaceOperationCancelled
        response = workspace_operation_stream(operation)
        iterator = response.body_iterator
        await anext(iterator)
        await asyncio.sleep(.02)
        await iterator.aclose()
        self.assertTrue(await asyncio.to_thread(stopped.wait, 2))

    async def test_existing_routes_support_stream_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            images = root / 'images'
            images.mkdir()
            with patch.dict(os.environ, {'DEVWISP_AUTH_FILE': str(root/'auth.json')}), patch('devwisp.app.tempfile.mkdtemp',return_value=str(images)):
                app = create_app(root)
            try:
                (root/'text').write_text('needle')
                endpoints = {route.path:route.endpoint for route in app.routes if getattr(route,'path','').endswith(('/workspace/search','/workspace/replace'))}
                normal = Request({'type':'http','headers':[]})
                streaming = Request({'type':'http','headers':[(b'accept',b'application/x-ndjson')]})
                body = WorkspaceSearchRequest(cwd=str(root),query='needle')
                result = await endpoints['/api/v1/codex/workspace/search'](body, normal)
                self.assertEqual(result['matchCount'],1)
                response = await endpoints['/api/v1/codex/workspace/search'](body, streaming)
                messages = [json.loads(chunk) async for chunk in response.body_iterator]
                self.assertEqual(messages[-1]['result']['matchCount'],1)
                response = await endpoints['/api/v1/codex/workspace/replace'](WorkspaceReplaceRequest(cwd=str(root),query='needle',replacement='done',allMatches=True),streaming)
                messages = [json.loads(chunk) async for chunk in response.body_iterator]
                self.assertEqual(messages[-1]['result']['matchCount'],1)
                self.assertEqual((root/'text').read_text(),'done')
            finally:
                app.state.shared_state.close()
                await app.state.claude_code.close()

if __name__ == '__main__':
    unittest.main()
