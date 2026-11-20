from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, Mock

from devwisp.claude_code import ClaudeCodeError
from devwisp.harness_interactions import HarnessQuestionBroker


class HarnessQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.emit = AsyncMock()
        self.broker = HarnessQuestionBroker(self.emit)
        self.cancelled = threading.Event()
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def ask(self, questions):
        task = asyncio.create_task(self.broker.ask('deepseek:test', 'turn', questions, self.cancelled))
        self.tasks.append(task)
        await asyncio.sleep(0)
        return self.broker.requests()[0], task

    async def test_native_multiselect_custom_plan_approval_label(self):
        request, task = await self.ask([
            {'id': 'multiple', 'question': 'Pick', 'multiSelect': True, 'options': [{'label': 'A'}, {'label': 'B'}]},
            {'id': 'plan', 'question': 'Review', 'intent': {'kind': 'plan-review', 'approve': 'Approve'},
             'options': [{'label': 'Keep planning'}, {'label': 'Approve'}]},
        ])
        snapshot = self.broker.requests(); snapshot[0]['questions'].clear()
        self.assertEqual(len(self.broker.requests()[0]['questions']), 2)
        self.assertEqual(self.broker.requests('deepseek:other'), [])
        self.broker.respond(request['threadId'], request['questionId'], {'answers': [
            {'id': 'plan', 'selected': ['Keep planning']},
            {'id': 'multiple', 'selected': ['A', 'B'], 'custom': 'extra'},
        ]})
        result = await task
        self.assertEqual(result['answers'][1]['selected'], ['Keep planning'])
        self.assertEqual(self.broker.requests(), [])
        self.assertEqual(self.emit.call_args.args[0]['type'], 'deepseek/question/resolved')

    async def test_rejects_cross_thread_missing_duplicate_unknown_and_implicit_approval(self):
        request, task = await self.ask([{'id': 'approval', 'question': 'Confirm', 'intent': {'kind': 'approval'},
            'options': [{'label': 'Yes'}, {'label': 'No'}]}])
        with self.assertRaises(ClaudeCodeError): self.broker.respond('deepseek:other', request['questionId'], {'cancelled': True})
        for answers in [[], [{'id': 'wrong', 'selected': ['Yes']}], [{'id': 'approval', 'selected': ['Yes', 'No']}],
                        [{'id': 'approval', 'selected': ['unknown']}], [{'id': 'approval', 'selected': ['Yes', 'Yes']}],
                        [{'id': 'approval', 'custom': 'approve'}], [{'id': 'approval', 'selected': []}]]:
            with self.assertRaises(ClaudeCodeError): self.broker.respond(request['threadId'], request['questionId'], {'answers': answers})
            self.assertFalse(task.done())
        self.broker.respond(request['threadId'], request['questionId'], {'cancelled': True})
        self.assertEqual(await task, {'cancelled': True})
        with self.assertRaises(ClaudeCodeError): self.broker.respond(request['threadId'], request['questionId'], {'cancelled': True})

    async def test_sdk_cancel_cleans_pending_and_publishes_resolution(self):
        _, task = await self.ask([{'id': 'text', 'question': 'Explain'}])
        self.cancelled.set()
        self.assertEqual(await asyncio.wait_for(task, 1), {'cancelled': True})
        self.assertEqual(self.broker.requests(), [])

    async def test_journal_failure_keeps_answer_retryable_and_cancel_does_not_journal(self):
        record = Mock(side_effect=OSError('disk unavailable'))
        self.broker.record_answer = record
        request, task = await self.ask([{'id': 'choice', 'question': 'Pick', 'options': [{'label': 'A'}]}])
        response = {'answers': [{'id': 'choice', 'selected': ['A']}]}
        with self.assertRaises(OSError):
            self.broker.respond(request['threadId'], request['questionId'], response)
        self.assertFalse(task.done())
        self.assertEqual(len(self.broker.requests()), 1)
        self.broker.respond(request['threadId'], request['questionId'], {'cancelled': True})
        self.assertEqual(await task, {'cancelled': True})
        record.assert_called_once()

    async def test_image_only_custom_answer_continues_native_question(self):
        request, task = await self.ask([{'id': 'image', 'question': 'Show the issue',
            'options': [{'label': 'Other issue'}]}])
        answer = '已附加图片，请读取以下本机图片文件：\n"F:\\images\\screen.png"'
        self.broker.respond(request['threadId'], request['questionId'], {'answers': [
            {'id': 'image', 'selected': [], 'custom': answer}]})
        self.assertEqual((await task)['answers'][0]['custom'], answer)
