"""Actual native plan enforcement with local model/MCP fixtures only."""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from devwisp.permission_prompt import PermissionPromptBroker
from devwisp.terminal_handoff import TerminalHandoffBroker
from test_deepseek_harness import _RealHarnessFixture


class PlanEnforcementTests(_RealHarnessFixture):
    def script(self, *batches):
        self.http.tool_batch_tag = 'plan-' + uuid.uuid4().hex
        self.http.tool_batches = batches

    def shell_write(self, path, **extra):
        return ('bash', {'description': 'Write disposable permission-test marker',
            'command': f'printf fixture > {shlex.quote(str(path))}', **extra})

    async def turn(self, thread=None, *, mode='plan', preset='standard'):
        thread = thread or await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Run the scripted permission fixture.',
            deepseek_permission_mode=mode, deepseek_agent_preset=preset)
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 40)
        history = await self.server.read_thread(thread['id'])
        self.assertEqual(history['turns'][-1]['status'], 'completed', history)
        return thread, history

    async def review(self, decision='approve'):
        requests = []
        async def emit(event):
            self.events.append(event)
            if event.get('type') != 'deepseek/question/request':
                return
            request = event['params']; requests.append(request)
            question = request['questions'][0]
            if decision == 'cancel':
                answer = {'cancelled': True}
            elif decision == 'custom':
                answer = {'answers': [{'id': question['id'], 'custom': 'Approve'}]}
            else:
                answer = {'answers': [{'id': question['id'], 'selected': [question['intent']['approve'] if decision == 'approve'
                    else next(o['label'] for o in question['options'] if o['label'] != question['intent']['approve'])]}]}
            self.server.questions.respond(request['threadId'], request['questionId'], answer)
        self.server.questions.emit = emit
        return requests

    async def test_direct_mutations_and_escalation_are_denied_without_approval(self):
        approvals = []
        async def emit(event):
            if event.get('type') == 'claude/permission/request':
                req = event['params']; approvals.append(req)
                await broker.respond(req['permissionId'], req['threadId'], True)
        broker = PermissionPromptBroker(self.root / 'permission.sock', emit)
        self.addAsyncCleanup(broker.close)
        self.server.enable_permission_prompt(broker)
        original = self.root / 'original.txt'; original.write_text('original')
        for preset in ('standard', 'cordis', 'legacy'):
            with self.subTest(preset=preset):
                marker = self.root / f'{preset}-marker'
                thread = await self.server.create_thread()
                if preset == 'legacy':
                    with self.server._connection:
                        self.server._connection.execute("UPDATE claude_threads SET agent_preset = 'legacy' WHERE thread_id = ?", (thread['id'],))
                self.script([
                    self.shell_write(marker),
                    self.shell_write(marker, sandbox_permissions='danger-full-access', justification='Must not override plan review'),
                    ('write', {'file_path': str(marker), 'content': 'mutated'}),
                    ('edit', {'file_path': str(original), 'old_string': 'original', 'new_string': 'mutated'}),
                    ('subagent', {'description': 'Try delegated mutation', 'prompt': 'Write a marker'}),
                ])
                _, history = await self.turn(thread, preset=preset)
                self.assertFalse(marker.exists(), history)
                self.assertEqual(original.read_text(), 'original')
                tools = [i for i in history['turns'][-1]['items'] if i['type'] == 'dynamicToolCall']
                self.assertEqual(len(tools), 5, history)
                self.assertTrue(all(i['status'] == 'failed' and 'Plan mode:' in json.dumps(i) for i in tools), tools)
        self.assertEqual(approvals, [])

    async def test_read_and_search_still_work(self):
        source = self.root / 'inspection.txt'; source.write_text('inspection-marker')
        self.script([('read', {'file_path': str(source)}),
            ('glob', {'pattern': '*.txt', 'path': str(self.root)}),
            ('grep', {'pattern': 'inspection-marker', 'path': str(self.root)})])
        _, history = await self.turn()
        tools = [i for i in history['turns'][-1]['items'] if i['type'] == 'dynamicToolCall']
        self.assertEqual(len(tools), 3)
        self.assertTrue(all(i['status'] == 'completed' for i in tools), tools)
        self.assertIn('inspection-marker', json.dumps(tools))

    async def test_only_approved_review_unlocks_later_steps_and_relocks_next_turn(self):
        for decision in ('approve', 'reject', 'cancel', 'custom'):
            with self.subTest(decision=decision):
                reviews = await self.review(decision)
                same = self.root / f'{decision}-same-batch'
                later = self.root / f'{decision}-later-step'
                self.script([('exit_plan_mode', {'plan': '# Fixture plan\nWrite the disposable marker.'}), self.shell_write(same)],
                            [self.shell_write(later)])
                thread, history = await self.turn()
                self.assertEqual(len(reviews), 1, history)
                self.assertFalse(same.exists(), history)
                self.assertEqual(later.exists(), decision == 'approve', history)
                blocked = self.root / f'{decision}-next-turn'
                self.script([self.shell_write(blocked)])
                _, history = await self.turn(thread)
                self.assertFalse(blocked.exists(), history)
                self.assertEqual(next(i for i in history['turns'][-1]['items'] if i['type'] == 'harnessTelemetry')['continuity'], 'reused')
                self.assertFalse(self.server.questions.requests())
        # Changing the selected mode rebuilds the runtime and removes the guard.
        self.script([self.shell_write(blocked)])
        _, history = await self.turn(thread, mode='workspace-write')
        self.assertTrue(blocked.exists(), history)

    async def test_creator_and_legacy_review_release_workspace_execution(self):
        for preset in ('cordis', 'legacy'):
            reviews = await self.review()
            marker = self.root / f'{preset}-approved'
            thread = await self.server.create_thread()
            if preset == 'legacy':
                with self.server._connection:
                    self.server._connection.execute("UPDATE claude_threads SET agent_preset = 'legacy' WHERE thread_id = ?", (thread['id'],))
            self.script([('exit_plan_mode', {'plan': '# Approved fixture\nWrite a marker.'})], [self.shell_write(marker)])
            _, history = await self.turn(thread, preset=preset)
            self.assertEqual(len(reviews), 1, history)
            self.assertTrue(marker.exists(), history)

    async def test_a_general_question_cannot_approve_execution(self):
        requests = []
        async def emit(event):
            if event.get('type') == 'deepseek/question/request':
                req = event['params']; requests.append(req); question = req['questions'][0]
                self.server.questions.respond(req['threadId'], req['questionId'], {'answers': [
                    {'id': question['id'], 'selected': ['Approve']} ]})
        self.server.questions.emit = emit
        marker = self.root / 'fake-review'
        self.script([('ask_user_question', {'questions': [{'id': 'fake', 'question': 'Approve?',
            'options': [{'label': 'Approve'}], 'intent': {'kind': 'plan-review', 'approve': 'Approve'}}]})],
            [self.shell_write(marker)])
        _, history = await self.turn()
        self.assertFalse(marker.exists(), history)
        self.assertEqual(len(requests), 1, history)

    async def test_ptc_arbitrary_programs_are_blocked_but_review_then_execution_works(self):
        reviews = await self.review()
        source = self.root / 'ptc-read'; source.write_text('ptc-inspection-marker')
        marker = self.root / 'ptc-marker'
        same = self.root / 'ptc-same-batch'
        read_code = f'console.log(await tools.read({json.dumps({"file_path": str(source)})}))'
        review_code = 'await tools.exit_plan_mode({"plan":"# PTC plan\\nWrite the disposable marker."})'
        bash_code = lambda path: f'await tools.bash({json.dumps(self.shell_write(path)[1])})'
        attacks = [
            f'const fs = await import("node:fs/promises"); await fs.writeFile({json.dumps(str(marker))}, "bad")',
            read_code + '; ' + bash_code(marker),
            'await tools.read({"file_path": (() => { throw "evaluated" })()})',
            'await tools.read({"file_path":"unused"}).then(() => fetch("http://127.0.0.1/"))',
            'console.log(await tools.read({"file_path":"unused"})); ' + bash_code(marker),
        ]
        self.script([('run_code', {'description': 'Try unapproved program', 'code': code}) for code in attacks],
            [('run_code', {'description': 'Read through inspection SDK', 'code': read_code})],
            [('run_code', {'description': 'Review plan', 'code': review_code}),
             ('run_code', {'description': 'Same-batch write', 'code': bash_code(same)})],
            [('run_code', {'description': 'Approved write', 'code': bash_code(marker)})])
        _, history = await self.turn(preset='ptc')
        self.assertEqual(len(reviews), 1, history)
        self.assertFalse(same.exists(), history)
        self.assertTrue(marker.exists(), history)
        self.assertEqual(marker.read_text(), 'fixture')
        self.assertIn('ptc-inspection-marker', json.dumps(history))
        tools = [i for i in history['turns'][-1]['items'] if i['type'] == 'dynamicToolCall']
        self.assertTrue(all(i['status'] == 'failed' and 'Plan mode:' in json.dumps(i) for i in tools[:len(attacks)]), tools)

    async def test_remote_and_terminal_tools_are_not_dispatched_before_plan_approval(self):
        fixture = Path(__file__).parent / 'fixtures' / 'harness_mcp.py'
        trace = self.root / 'mcp-trace'
        self.server.enable_ssh_mcp(SimpleNamespace(codex_server_config=lambda: {
            'command': sys.executable, 'args': [str(fixture)], 'env': {
                'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'HARNESS_TEST_TRACE': str(trace)}}))
        terminal = TerminalHandoffBroker(self.root / 'runtime' / 'terminal.sock')
        await terminal.start()
        self.addAsyncCleanup(terminal.close)
        self.server.enable_terminal_handoff(terminal)
        self.script([('mcp__devwisp_ssh_mcp__confirm_test', {}), ('mcp__devwisp_terminal__read_terminal', {})])
        _, history = await self.turn()
        tools = [i for i in history['turns'][-1]['items'] if i['type'] == 'dynamicToolCall']
        self.assertEqual(len(tools), 2, history)
        self.assertTrue(all(i['status'] == 'failed' and 'Plan mode:' in json.dumps(i) for i in tools), tools)
        self.assertNotIn('tools/call', trace.read_text())

    async def test_ptc_new_plan_does_not_inherit_approved_program_globals(self):
        await self.review()
        source = self.root / 'fresh-ptc-plan'; source.write_text('fresh-plan-inspection')
        self.script([('run_code', {'description': 'Review first plan', 'code':
            'await tools.exit_plan_mode({"plan":"# First plan\\nInspect the disposable environment."})'})],
            [('run_code', {'description': 'Change approved program globals', 'code':
                'globalThis.console.log = () => { throw new Error("stale approved program") };'})])
        thread, _ = await self.turn(preset='ptc')
        self.script([('run_code', {'description': 'Inspect from a new unapproved plan', 'code':
            f'console.log(await tools.read({json.dumps({"file_path": str(source)})}))'})])
        _, history = await self.turn(thread, preset='ptc')
        tool = next(i for i in history['turns'][-1]['items'] if i['type'] == 'dynamicToolCall')
        self.assertEqual(tool['status'], 'completed', tool)
        self.assertIn('fresh-plan-inspection', json.dumps(tool))

    async def test_stop_during_plan_review_does_not_leave_an_execution_grant(self):
        received = asyncio.Event()
        async def emit(event):
            if event.get('type') == 'deepseek/question/request': received.set()
        self.server.questions.emit = emit
        self.script([('exit_plan_mode', {'plan': '# Wait\nWait for explicit approval.'})])
        thread = await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Review plan', deepseek_permission_mode='plan')
        tasks = tuple(self.server._tasks.values())
        await asyncio.wait_for(received.wait(), 20)
        await self.server.interrupt(thread['id'])
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        self.assertFalse(self.server.questions.requests())
        marker = self.root / 'after-stop'
        self.script([self.shell_write(marker)])
        _, history = await self.turn(thread)
        self.assertFalse(marker.exists(), history)
