"""Thread-scoped, cancellable native Harness questions and plan reviews."""
from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from typing import Any, Callable

from .claude_code import ClaudeCodeError


class HarnessQuestionBroker:
    def __init__(self, emit: Any, record_answer: Callable[[dict, dict], dict] | None = None) -> None:
        self.emit = emit
        self.record_answer = record_answer
        self.pending: dict[str, tuple[dict, asyncio.Future]] = {}

    def requests(self, thread_id: str | None = None) -> list[dict]:
        return [copy.deepcopy(request) for request, future in self.pending.values()
                if not future.done() and (thread_id is None or request['threadId'] == thread_id)]

    async def ask(self, thread_id: str, turn_id: str, questions: list[dict], cancelled: threading.Event,
                  *, after_item_id: str | None = None) -> dict:
        question_id = uuid.uuid4().hex
        request = {'threadId': thread_id, 'turnId': turn_id, 'questionId': question_id, 'questions': copy.deepcopy(questions)}
        if after_item_id is not None:
            request.update(afterItemId=after_item_id, timelineVersion=1)
        future = asyncio.get_running_loop().create_future()
        self.pending[question_id] = (request, future)
        try:
            await self.emit({'type': 'deepseek/question/request', 'params': request})
            while True:
                if future.done():
                    response, event = future.result()
                    if event is not None:
                        await self.emit(event)
                    return response
                if cancelled.is_set():
                    return {'cancelled': True}
                await asyncio.wait([future], timeout=.2)
        finally:
            self.pending.pop(question_id, None)
            if not future.done():
                future.cancel()
            await self.emit({'type': 'deepseek/question/resolved', 'params': {
                'threadId': thread_id, 'questionId': question_id}})

    def respond(self, thread_id: str, question_id: str, response: dict) -> None:
        entry = self.pending.get(question_id)
        if not entry or entry[0]['threadId'] != thread_id or entry[1].done():
            raise ClaudeCodeError('该问题已结束或不属于当前对话，请刷新后重试。')
        request, future = entry
        if response.get('cancelled') is True:
            future.set_result(({'cancelled': True}, None))
            return
        answers = response.get('answers')
        if not isinstance(answers, list) or len(answers) != len(request['questions']):
            raise ClaudeCodeError('请回答全部问题后再提交。')
        by_id = {a.get('id'): a for a in answers if isinstance(a, dict) and isinstance(a.get('id'), str)}
        if len(by_id) != len(answers):
            raise ClaudeCodeError('问题答案格式无效。')
        validated = []
        for question in request['questions']:
            answer = by_id.get(question['id'], {})
            selected = answer.get('selected', [])
            custom = answer.get('custom', '')
            options = {o['label'] for o in question.get('options', [])}
            intent = (question.get('intent') or {}).get('kind')
            if (not isinstance(selected, list) or any(not isinstance(s, str) or s not in options for s in selected)
                    or len(set(selected)) != len(selected) or not isinstance(custom, str) or len(custom) > 30_000
                    or (not question.get('multiSelect') and len(selected) + bool(custom.strip()) > 1)
                    or (intent == 'approval' and custom.strip()) or not (selected or custom.strip())):
                raise ClaudeCodeError('请选择有效答案或填写自定义回答。')
            validated.append({'id': question['id'], 'selected': selected, **({'custom': custom.strip()} if custom.strip() else {})})
        result = {'answers': validated}
        # Journal synchronously before acknowledging HTTP success or releasing
        # the native tool callback. A failed write leaves the request retryable.
        event = self.record_answer(request, result) if self.record_answer else None
        future.set_result((result, event))
