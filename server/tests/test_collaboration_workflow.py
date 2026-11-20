from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from devwisp.collaboration import CollaborationError, CollaborationService, CollaborationStore
from devwisp.collaboration_plan import discussion_result, next_batch, parse_plan
from devwisp import collaboration_workspace as workspace


class PlanTests(unittest.TestCase):
    def task(self, key='a', member='m1', files=None, deps=None):
        return {'id': key, 'member': member, 'goal': 'Implement and verify', 'files': files or ['src/a.ts'], 'dependsOn': deps or []}

    def plan(self, tasks):
        return json.dumps({'action': 'develop', 'summary': 'Agreed implementation', 'tasks': tasks})

    def test_invalid_or_unbounded_plans_cannot_enable_development(self):
        cases = ['text', '[]', '{"action":{}}', self.plan([]), self.plan([self.task(member='outsider')]),
                 self.plan([self.task(files=['../outside'])]), self.plan([self.task(files=['/tmp/file'])]),
                 self.plan([self.task(files=['.git/config'])]), self.plan([self.task(files=['src/*.ts'])]),
                 self.plan([self.task(deps=['missing'])]), self.plan([self.task(deps=['a'])]),
                 self.plan([self.task(), self.task()]),
                 self.plan([self.task(deps=['b']), self.task('b', deps=['a'])])]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(CollaborationError):
                parse_plan(value, ['m1', 'm2'])

    def test_dependency_order_and_overlapping_files_are_serialized(self):
        tasks = [self.task('a', files=['shared.ts'], deps=['b']), self.task('b', 'm2', ['shared.ts']),
                 self.task('c', 'm1', ['other.ts'])]
        plan = parse_plan(self.plan(tasks), ['m1', 'm2'])
        self.assertEqual([task['id'] for task in next_batch(plan['tasks'], set())], ['b', 'c'])
        self.assertEqual([task['id'] for task in next_batch(plan['tasks'], {'b', 'c'})], ['a'])
        adjacent = [self.task('a'), self.task('b', 'm2'), self.task('c', 'm2', ['other.ts'])]
        self.assertEqual([task['id'] for task in next_batch(adjacent, set())], ['a', 'c'])

    def test_protocol_is_hidden_but_quoted_examples_do_not_start_development(self):
        self.assertEqual(discussion_result('Proposal\n<devwisp:develop/>\nHaha'), ('Proposal\nHaha', True))
        self.assertFalse(discussion_result('```\n<devwisp:develop/>\n```')[1])
        route = parse_plan(self.plan([self.task(files=['app/[id]/page.tsx'])]), ['m1'])
        self.assertEqual(route['tasks'][0]['files'], ['app/[id]/page.tsx'])


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'a.txt').write_text('original')
        (self.root / 'b.txt').write_text('other')

    def prepare(self):
        context = workspace.prepare(str(self.root))
        self.addCleanup(workspace.cleanup, context)
        return context, Path(context['cwd'])

    def test_snapshots_preserve_dirty_and_untracked_source_and_integrate_only_changes(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Fixture'], check=True)
        (self.root / 'a.txt').write_text('existing dirty change')
        (self.root / 'untracked.txt').write_text('preserve me')
        context, isolated = self.prepare()
        self.assertEqual((isolated / 'a.txt').read_text(), 'existing dirty change')
        self.assertEqual((isolated / 'untracked.txt').read_text(), 'preserve me')
        (isolated / 'b.txt').write_text('new work')
        self.assertEqual((self.root / 'b.txt').read_text(), 'other')
        self.assertEqual(workspace.integrate(context, ['b.txt']), ['b.txt'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'existing dirty change')
        self.assertEqual((self.root / 'b.txt').read_text(), 'new work')
        self.assertEqual(workspace.integrate(context, ['b.txt']), ['b.txt'])

    def test_out_of_scope_changes_and_external_edits_prevent_all_writes(self):
        context, isolated = self.prepare()
        (isolated / 'a.txt').write_text('worker a')
        (isolated / 'b.txt').write_text('worker b')
        with self.assertRaisesRegex(CollaborationError, '超出分工'):
            workspace.integrate(context, ['a.txt'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'original')
        (self.root / 'b.txt').write_text('external change')
        with self.assertRaisesRegex(CollaborationError, '已变化'):
            workspace.integrate(context, ['a.txt', 'b.txt'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'original')
        self.assertEqual((self.root / 'b.txt').read_text(), 'external change')
        self.assertTrue(isolated.exists())

    def test_new_deleted_binary_files_and_symlink_escape(self):
        context, isolated = self.prepare()
        (isolated / 'a.txt').unlink()
        (isolated / 'new.bin').write_bytes(b'\x00\xff')
        workspace.integrate(context, ['a.txt', 'new.bin'])
        self.assertFalse((self.root / 'a.txt').exists())
        self.assertEqual((self.root / 'new.bin').read_bytes(), b'\x00\xff')
        (isolated / 'b.txt').unlink()
        (isolated / 'b.txt').symlink_to(self.root / 'b.txt')
        with self.assertRaises(CollaborationError):
            workspace.integrate(context, ['b.txt'])

    def test_dependencies_are_reused_but_build_caches_and_source_stay_isolated(self):
        modules = self.root / 'node_modules'
        package = modules / 'fixture-package'
        package.mkdir(parents=True)
        (package / 'index.js').write_text('export default 1')
        (modules / '.tmp').mkdir()
        (modules / '.tmp' / 'cache').write_text('source cache')
        environment = self.root / '.venv'
        environment.mkdir()
        context, isolated = self.prepare()
        self.assertEqual((isolated / 'node_modules/fixture-package/index.js').read_text(), 'export default 1')
        self.assertEqual((isolated / '.venv').resolve(), environment)
        cache = isolated / 'node_modules/.tmp/cache'
        cache.parent.mkdir()
        cache.write_text('isolated cache')
        self.assertEqual((modules / '.tmp/cache').read_text(), 'source cache')
        (isolated / 'a.txt').write_text('new work')
        self.assertEqual(workspace.integrate(context, ['a.txt']), ['a.txt'])

    def test_new_git_project_without_commits_can_use_a_plain_snapshot(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        context, isolated = self.prepare()
        self.assertIsNone(context['repository'])
        (isolated / 'a.txt').write_text('first implementation')
        workspace.integrate(context, ['a.txt'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'first implementation')

    def test_nested_git_project_preserves_dirty_files_without_integrating_siblings(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        project = self.root / 'project'
        project.mkdir()
        (project / 'code.txt').write_text('base')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Fixture'], check=True)
        (project / 'code.txt').write_text('dirty')
        context = workspace.prepare(str(project))
        self.addCleanup(workspace.cleanup, context)
        isolated = Path(context['cwd'])
        self.assertEqual((isolated / 'code.txt').read_text(), 'dirty')
        (isolated / 'code.txt').write_text('new')
        (isolated.parent / 'a.txt').write_text('isolated sibling')
        workspace.integrate(context, ['code.txt'])
        self.assertEqual((project / 'code.txt').read_text(), 'new')
        self.assertEqual((self.root / 'a.txt').read_text(), 'original')


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'a.txt').write_text('a')
        (self.root / 'b.txt').write_text('b')
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.store = CollaborationStore(self.db)
        self.native, self.calls = {}, []
        self.discussing, self.developing = set(), set()
        self.discussion_barrier, self.development_barrier = asyncio.Event(), asyncio.Event()
        self.request_develop = True
        self.plan = {'action': 'develop', 'summary': 'Use separate files', 'tasks': [
            {'id': 'a', 'member': 'm1', 'goal': 'Change a and verify', 'files': ['a.txt'], 'dependsOn': []},
            {'id': 'b', 'member': 'm2', 'goal': 'Change b and verify', 'files': ['b.txt'], 'dependsOn': []}]}
        self.incomplete, self.outside = False, False
        self.service = CollaborationService(self.store, AsyncMock(side_effect=self.create), self.send, self.read, AsyncMock())
        self.addAsyncCleanup(self.service.close)
        self.group = self.service.create_group({'requestId': str(uuid.uuid4()), 'cwd': str(self.root), 'member': None})
        for key in ('m1', 'm2'):
            self.service.add_group_member(self.group['id'], key, {'engine': 'codex', 'model': ''})

    async def asyncTearDown(self):
        group = self.store.get(self.group['id'])
        for job in (group.get('run') or {}).get('jobs', {}).values():
            if Path(job['workspace']['root']).exists():
                workspace.cleanup(job['workspace'])

    async def create(self, member, cwd, title):
        return str(uuid.uuid4())

    async def send(self, member, prompt, readonly):
        group = self.store.get(self.group['id'])
        phase = group['run']['stage']
        self.calls.append((phase, member.copy(), prompt, readonly))
        if phase == 'discussion':
            self.assertTrue(readonly)
            self.discussing.add(member['name'])
            if len(self.discussing) == 2:
                self.discussion_barrier.set()
            await asyncio.wait_for(self.discussion_barrier.wait(), 2)
            text = member['name'] + ' proposal' + ('\n<devwisp:develop/>' if self.request_develop else '')
        elif phase == 'planning':
            self.assertTrue(readonly)
            self.assertIn('Codex 1 proposal', prompt)
            self.assertIn('Codex 2 proposal', prompt)
            text = json.dumps(self.plan)
        elif phase == 'developing':
            self.assertFalse(readonly)
            target = Path(member['groupCwd'])
            self.assertNotEqual(target, self.root)
            self.developing.add(member['name'])
            if len(self.developing) == 2:
                self.development_barrier.set()
            if not any(task['dependsOn'] for task in self.plan['tasks']) and self.plan['tasks'][0]['files'] != self.plan['tasks'][1]['files']:
                await asyncio.wait_for(self.development_barrier.wait(), 2)
            name = 'a.txt' if member['name'] == 'Codex 1' else 'b.txt'
            if self.plan['tasks'][1]['files'] == ['a.txt'] and member['name'] == 'Codex 2':
                self.assertEqual((target / 'a.txt').read_text(), 'changed by Codex 1')
                name = 'a.txt'
            (target / name).write_text('changed by ' + member['name'])
            if self.outside:
                (target / 'outside.txt').write_text('unexpected')
            text = 'Implemented and checked' + ('' if self.incomplete else '\n<devwisp:task-complete/>')
        else:
            self.assertEqual(phase, 'summarizing')
            self.assertTrue(readonly)
            self.assertIn('Implemented and checked', prompt)
            text = 'Verified the integrated changes'
        turn_id = str(uuid.uuid4())
        self.native[member['threadId']] = {'turns': [{'id': turn_id, 'status': 'completed',
                                                      'items': [{'type': 'agentMessage', 'text': text}]}]}
        return turn_id

    async def read(self, member):
        return self.native.get(member['threadId'], {'turns': []})

    async def run_group(self):
        request_id = str(uuid.uuid4())
        await self.service.group_message(self.group['id'], request_id, 'Implement both changes')
        await asyncio.wait_for(self.service._workers[self.group['id']], 8)
        return self.store.detail(self.group['id'])

    async def test_parallel_discussion_then_isolated_development_and_readonly_summary(self):
        group = await self.run_group()
        self.assertEqual(group['status'], 'idle', group['reason'])
        self.assertEqual([phase for phase, *_ in self.calls], ['discussion', 'discussion', 'planning', 'developing', 'developing', 'summarizing'])
        self.assertNotIn('Codex 1 proposal', self.calls[1][2])
        self.assertNotIn('Codex 2 proposal', self.calls[0][2])
        self.assertEqual((self.root / 'a.txt').read_text(), 'changed by Codex 1')
        self.assertEqual((self.root / 'b.txt').read_text(), 'changed by Codex 2')
        self.assertEqual(group['lastResult'], 'Verified the integrated changes')
        for event in group['events']:
            self.assertNotIn('<devwisp:', event['text'])
        for worker in group['workerMembers'].values():
            self.assertEqual(self.store.owner(worker['threadId']), group['id'])
            self.assertFalse(Path(worker['cwd']).exists())

    async def test_pure_discussion_is_bounded_and_next_round_reads_all_previous_results(self):
        self.request_develop = False
        group = await self.run_group()
        self.assertEqual(group['status'], 'idle')
        self.assertEqual(len(self.calls), 2)
        await self.run_group()
        for _, _, prompt, readonly in self.calls[2:]:
            self.assertTrue(readonly)
            self.assertIn('Codex 1 proposal', prompt)
            self.assertIn('Codex 2 proposal', prompt)

    async def test_long_replies_cannot_push_an_entire_participant_out_of_next_snapshot(self):
        group = self.store.get(self.group['id'])
        group['lastRoundRecords'] = [{'role': 'm1', 'text': 'first-participant-' + 'a' * 40000},
                                     {'role': 'm2', 'text': 'second-participant-' + 'b' * 40000}]
        self.store.event(group['id'], 'm1', group['lastRoundRecords'][0]['text'])
        self.store.event(group['id'], 'm2', group['lastRoundRecords'][1]['text'])
        self.store.save(group)
        self.request_develop = False
        await self.run_group()
        for _, _, prompt, _ in self.calls:
            self.assertIn('first-participant-', prompt)
            self.assertIn('second-participant-', prompt)

    async def test_overlapping_file_tasks_wait_for_integration_before_next_snapshot(self):
        self.plan['tasks'][1]['files'] = ['a.txt']
        group = await self.run_group()
        self.assertEqual(group['status'], 'idle', group['reason'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'changed by Codex 2')

    async def test_incomplete_work_never_integrates_or_starts_summary(self):
        self.incomplete = True
        group = await self.run_group()
        self.assertEqual(group['status'], 'paused')
        self.assertIn('尚未确认', group['reason'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'a')
        self.assertNotIn('summarizing', [phase for phase, *_ in self.calls])
        self.assertTrue(all(Path(job['workspace']['cwd']).exists() for job in group['run']['jobs'].values()))

    async def test_scope_violation_retains_work_and_pauses_without_changing_source(self):
        self.outside = True
        group = await self.run_group()
        self.assertEqual(group['status'], 'paused')
        self.assertIn('超出分工', group['reason'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'a')
        self.assertFalse((self.root / 'outside.txt').exists())

    async def test_planner_can_keep_discussing_without_any_development(self):
        self.plan = {'action': 'discuss', 'summary': 'Need to clarify the accepted behavior'}
        group = await self.run_group()
        self.assertEqual(group['status'], 'idle')
        self.assertEqual([phase for phase, *_ in self.calls], ['discussion', 'discussion', 'planning'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'a')

    async def test_unknown_parallel_dispatch_cannot_be_replayed_on_resume(self):
        original = self.service.send
        async def uncertain(member, prompt, readonly):
            if member['name'] == 'Codex 1':
                self.discussing.add(member['name'])
                if len(self.discussing) == 2:
                    self.discussion_barrier.set()
                raise RuntimeError('Lost native acknowledgement')
            return await original(member, prompt, readonly)
        self.service.send = uncertain
        with self.assertLogs('devwisp.collaboration', level='ERROR'):
            group = await self.run_group()
        self.assertEqual(group['status'], 'failed')
        with self.assertRaisesRegex(CollaborationError, '未确认'):
            await self.service.resume(group['id'])

    async def test_incomplete_task_can_continue_in_same_workspace_without_repeating_finished_tasks(self):
        self.incomplete = True
        group = await self.run_group()
        paths = {key: job['workspace']['cwd'] for key, job in group['run']['jobs'].items()}
        self.incomplete = False
        await self.service.resume(group['id'])
        await asyncio.wait_for(self.service._workers[group['id']], 8)
        # The other independent task was also incomplete; continue it explicitly.
        if self.store.get(group['id'])['status'] == 'paused':
            await self.service.resume(group['id'])
            await asyncio.wait_for(self.service._workers[group['id']], 8)
        group = self.store.get(group['id'])
        self.assertEqual(group['status'], 'idle', group['reason'])
        development = [member for phase, member, *_ in self.calls if phase == 'developing']
        self.assertTrue(all(member['groupCwd'] in paths.values() for member in development))

    async def test_invalid_plan_ends_round_with_explanation_without_writes(self):
        self.plan['tasks'][0]['member'] = 'not-selected'
        group = await self.run_group()
        self.assertEqual(group['status'], 'idle')
        self.assertTrue(group['reason'])
        self.assertEqual((self.root / 'a.txt').read_text(), 'a')
