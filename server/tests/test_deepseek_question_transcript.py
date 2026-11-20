from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import patch

from devwisp.claude_code import ClaudeCodeError
from devwisp.deepseek_harness import DeepSeekHarnessServer
from test_deepseek_harness import _HarnessFixture, _RealHarnessFixture


class QuestionTranscriptTests(_HarnessFixture):
    async def question(self, thread_id, questions, turn_id='original-turn'):
        cancelled = threading.Event()
        task = asyncio.create_task(self.server.questions.ask(thread_id, turn_id, questions, cancelled))
        self.addAsyncCleanup(self.finish_task, task)
        await asyncio.sleep(0)
        return self.server.questions.requests(thread_id)[0], task, cancelled

    @staticmethod
    async def finish_task(task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def answer_items(self, thread_id):
        messages = json.loads(self.server._row(thread_id)['messages_json'])
        return [m for m in messages if m.get('id', '').startswith('user-question-')]

    async def test_success_is_durable_before_acknowledgement_and_matches_live_user_item(self):
        thread = await self.server.create_thread()
        other = await self.server.create_thread()
        questions = [
            {'id': 'multi', 'question': '选择两个模块', 'header': '模块', 'multiSelect': True,
             'options': [{'label': '终端'}, {'label': '文件'}]},
            {'id': 'custom', 'question': '还有什么反馈？'},
        ]
        request, task, _ = await self.question(thread['id'], questions)
        body = {'answers': [{'id': 'custom', 'custom': '  保留\n原来的布局  '},
                            {'id': 'multi', 'selected': ['终端', '文件'], 'custom': '  支持快捷键  '}]}
        self.server.questions.respond(thread['id'], request['questionId'], body)
        # The synchronous route must not acknowledge an answer that exists only
        # in the native waiting callback or a browser's local state.
        saved = self.answer_items(thread['id'])
        self.assertEqual(len(saved), 1)
        expected = '1. 选择两个模块\n答：终端、文件、支持快捷键\n\n2. 还有什么反馈？\n答：保留\n原来的布局'
        self.assertEqual(saved[0]['text'], expected)
        self.assertEqual(saved[0]['turnId'], 'original-turn')
        with self.assertRaises(ClaudeCodeError):
            self.server.questions.respond(thread['id'], request['questionId'], body)
        result = await task
        self.assertEqual(result['answers'][0]['id'], 'multi')
        self.assertNotIn('questionId', result)
        items = [e['params']['item'] for e in self.events if e.get('method') == 'item/started']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['type'], 'userMessage')
        self.assertEqual(items[0]['id'], saved[0]['id'])
        self.assertEqual(items[0]['content'], [{'type': 'text', 'text': expected}])
        self.assertEqual(self.answer_items(other['id']), [])
        reopened = DeepSeekHarnessServer(self.root, self.root / 'threads.sqlite3', self.server.emit)
        try:
            history = await reopened.read_thread(thread['id'])
            user = history['turns'][0]['items'][0]
            self.assertEqual(user['id'], saved[0]['id'])
            self.assertEqual(user['content'], items[0]['content'])
        finally:
            await reopened.close()
        self.assertEqual(len(self.answer_items(thread['id'])), 1)

    async def test_invalid_cancelled_and_wrong_thread_answers_do_not_create_messages(self):
        thread = await self.server.create_thread()
        other = await self.server.create_thread()
        question = [{'id': 'one', 'question': '继续？', 'options': [{'label': '继续'}]}]
        request, task, _ = await self.question(thread['id'], question)
        valid = {'answers': [{'id': 'one', 'selected': ['继续']}]}
        for tid, body in [(other['id'], valid), (thread['id'], {'answers': []}),
                          (thread['id'], {'answers': [{'id': 'one', 'selected': ['未知答案']}]})]:
            with self.assertRaises(ClaudeCodeError):
                self.server.questions.respond(tid, request['questionId'], body)
        self.assertEqual(self.answer_items(thread['id']), [])
        self.assertFalse(task.done())
        self.server.questions.respond(thread['id'], request['questionId'], {'cancelled': True})
        self.assertEqual(await task, {'cancelled': True})
        request, task, cancelled = await self.question(thread['id'], question)
        cancelled.set()
        self.assertEqual(await asyncio.wait_for(task, 1), {'cancelled': True})
        self.assertEqual(self.answer_items(thread['id']), [])
        self.assertFalse(any(e.get('method') == 'item/started' for e in self.events))

    async def test_failed_persistence_can_retry_and_accepted_answer_wins_stop_race(self):
        thread = await self.server.create_thread()
        request, task, cancelled = await self.question(thread['id'], [
            {'id': 'plan', 'question': '确认计划？', 'intent': {'kind': 'plan-review', 'approve': '执行'},
             'options': [{'label': '执行'}, {'label': '调整'}]}])
        body = {'answers': [{'id': 'plan', 'selected': ['调整']}]}
        with patch.object(self.server.questions, 'record_answer', side_effect=OSError('fixture storage failure')):
            with self.assertRaises(OSError):
                self.server.questions.respond(thread['id'], request['questionId'], body)
        self.assertFalse(task.done())
        self.assertEqual(self.answer_items(thread['id']), [])
        self.server.questions.respond(thread['id'], request['questionId'], body)
        cancelled.set()
        self.assertEqual(await task, body)
        self.assertEqual(len(self.answer_items(thread['id'])), 1)
        self.assertEqual(len([e for e in self.events if e.get('method') == 'item/started']), 1)


class NativeQuestionTranscriptTests(_RealHarnessFixture):
    async def test_two_questions_split_assistant_text_in_live_and_reopened_history(self):
        self.server.save_settings('fixture-model', f'http://127.0.0.1:{self.http.server_port}',
                                  'fixture-placeholder', protocol='chat')
        self.http.tool_enabled = True
        self.http.tools_each_turn = True
        self.http.tool_name = 'ask_user_question'
        self.http.question_intros = ['先确定第一个问题。', '收到，再确定第二个问题。']
        self.http.tool_arguments = {'questions': [{'id': 'q', 'question': '如何继续？',
                                                  'options': [{'label': '继续'}]}]}
        boundaries = []

        async def emit(event):
            self.events.append(event)
            if event.get('type') == 'deepseek/question/request':
                request = event['params']
                history = await self.server.read_thread(request['threadId'])
                turn = history['turns'][-1]
                assistants = [i for i in turn['items'] if i['type'] == 'agentMessage' and i.get('text')]
                self.assertEqual(turn['status'], 'inProgress')
                self.assertEqual(assistants[-1]['text'], self.http.question_intros[len(boundaries)])
                self.assertEqual(request['afterItemId'], assistants[-1]['id'])
                self.assertEqual(request['timelineVersion'], 1)
                boundaries.append(request['afterItemId'])
                self.server.questions.respond(request['threadId'], request['questionId'],
                    {'answers': [{'id': 'q', 'selected': ['继续']}]})

        self.server.questions.emit = emit
        thread = await self.server.create_thread()
        await self.server.start_turn(thread['id'], 'Ask twice')
        await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
        history = await self.server.read_thread(thread['id'])
        turn = history['turns'][-1]
        self.assertEqual(turn['status'], 'completed', history)
        conversation = [i for i in turn['items'] if i['type'] in {'userMessage', 'agentMessage'}]
        self.assertEqual([i.get('text') or i['content'][0]['text'] for i in conversation], [
            'Ask twice', self.http.question_intros[0], '1. 如何继续？\n答：继续',
            self.http.question_intros[1], '1. 如何继续？\n答：继续', 'Hello Harness'])
        self.assertEqual(len({i['id'] for i in conversation}), len(conversation))
        self.assertEqual(boundaries, [conversation[1]['id'], conversation[3]['id']])
        live_order = [e['params']['item']['id'] for e in self.events
                      if e.get('method') in {'item/started', 'item/completed'}
                      and e.get('params', {}).get('item', {}).get('type') in {'userMessage', 'agentMessage'}]
        self.assertEqual(live_order, [i['id'] for i in conversation])
        deltas = [e['params'] for e in self.events if e.get('method') == 'item/agentMessage/delta']
        for item in conversation:
            if item['type'] == 'agentMessage':
                self.assertEqual(''.join(d['delta'] for d in deltas if d['itemId'] == item['id']), item['text'])
        reopened = DeepSeekHarnessServer(self.root, self.root / 'threads.sqlite3', self.server.emit)
        try:
            self.assertEqual((await reopened.read_thread(thread['id']))['turns'], history['turns'])
        finally:
            await reopened.close()
        self.assertEqual(self.server.questions.requests(), [])

    async def test_answers_resume_native_tool_and_survive_reused_runtime_history(self):
        self.server.save_settings('fixture-model', f'http://127.0.0.1:{self.http.server_port}',
                                  'fixture-placeholder', protocol='chat')
        self.http.tool_enabled = self.http.tools_each_turn = True
        self.http.tool_name = 'ask_user_question'
        self.http.tool_arguments = {'questions': [{'id': 'q', 'question': '哪里会断开？',
                                                  'options': [{'label': '等待再次交给 AI'}]}]}
        requests = []
        async def emit(event):
            self.events.append(event)
            if event.get('type') == 'deepseek/question/request':
                request = event['params']
                requests.append(request)
                self.server.questions.respond(request['threadId'], request['questionId'],
                    {'answers': [{'id': request['questions'][0]['id'], 'selected': ['等待再次交给 AI']}]})
        self.server.questions.emit = emit
        thread = await self.server.create_thread()
        lease = None
        for prompt in ['First question', 'Second question']:
            turn_id = await self.server.start_turn(thread['id'], prompt)
            await asyncio.wait_for(asyncio.gather(*tuple(self.server._tasks.values())), 35)
            history = await self.server.read_thread(thread['id'])
            turn = history['turns'][-1]
            self.assertEqual(turn['id'], turn_id)
            self.assertEqual(turn['status'], 'completed', history)
            users = [i for i in turn['items'] if i['type'] == 'userMessage']
            self.assertEqual(len(users), 2, history)
            self.assertEqual(users[1]['content'][0]['text'], '1. 哪里会断开？\n答：等待再次交给 AI')
            self.assertEqual(requests[-1]['afterItemId'], users[0]['id'])
            if lease:
                self.assertIs(self.server._runtimes[thread['id']], lease)
            lease = self.server._runtimes[thread['id']]
        self.assertEqual(len(history['turns']), 2)
        self.assertEqual(len(requests), 2)
        answer_events = [e for e in self.events if e.get('method') == 'item/started'
                         and e['params']['item']['id'].startswith('user-question-')]
        self.assertEqual(len(answer_events), 2)
        self.assertEqual([e['params']['turnId'] for e in answer_events], [r['turnId'] for r in requests])
        self.assertEqual(self.server.questions.requests(), [])
