"""Small, durable browser projection of native Harness notifications.

Only provider-returned reasoning and counters are projected. Child sessions have
their own output and accounting; neither is merged into the parent's answer.
"""
from __future__ import annotations

import math
from typing import Any


def count(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 2**53 - 1 and math.isfinite(value) and value == int(value):
        return int(value)
    return None


class HarnessObservability:
    def __init__(self, session_id: str, known_children: dict[str, str] | None = None) -> None:
        self.root = session_id
        self.phase = 'starting'
        self.continuity = 'new'
        self.reasoning = ''
        self.children: dict[str, dict] = {}
        self.known_children = dict(known_children or {})
        self.context: dict[str, int] = {}
        self._attempts: dict[str, int] = {}
        self._models: dict[tuple[str, int], str] = {}
        self._usage: dict[tuple[str, int], dict[str, int]] = {}
        self._reasoning_streamed: set[tuple[str, int]] = set()

    def usage_records(self) -> list[tuple[str, str | None, dict]]:
        attempts = set(self._usage)
        for sid, last in self._attempts.items():
            attempts.update((sid, attempt) for attempt in range(1, last + 1))
        return [(f'{sid}:{attempt}', self._models.get((sid, attempt)), self._usage.get((sid, attempt), {}))
                for sid, attempt in sorted(attempts)]

    def snapshot(self) -> dict:
        # Cache buckets are disjoint from native inputTokens. Never add reasoning
        # to output: the provider already includes it in outputTokens.
        usage = {}
        root_samples = [sample for (session, _), sample in self._usage.items() if session == self.root]
        if root_samples:
            for key in ('inputTokens', 'outputTokens', 'totalTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens'):
                if all(key in sample for sample in root_samples):
                    total = sum(sample[key] for sample in root_samples)
                    if count(total) is not None:
                        usage[key] = total
        partial = any((self.root, attempt) not in self._usage for attempt in range(1, self._attempts.get(self.root, 0) + 1))
        return {'continuity': self.continuity, 'phase': self.phase, 'reasoning': self.reasoning, 'usage': usage, 'usagePartial': partial,
                'context': self.context.copy(), 'children': [{**child, 'tools': [tool.copy() for tool in child['tools']]} for child in self.children.values()]}

    def observe(self, method: str, payload: dict) -> bool:
        before = self.snapshot()
        sid = payload.get('sessionId')
        if isinstance(sid, str) and sid in self.known_children and method in {'session.event', 'session.stream', 'session.status'}:
            self.children.setdefault(sid, self._child(sid, self.known_children[sid]))
        if method == 'subagent.started':
            child, parent = payload.get('childSessionId'), payload.get('parentSessionId')
            if isinstance(child, str) and (parent == self.root or parent in self.children):
                self.children.setdefault(child, self._child(child, parent))
                self.known_children[child] = parent
        elif method == 'subagent.finished':
            child = self.children.get(payload.get('childSessionId'))
            if child:
                status = payload.get('status')
                child['status'] = 'failed' if status in {'error', 'failed'} else 'stopped' if status in {'aborted', 'cancelled', 'stopped'} else 'completed'
                content = payload.get('lastAssistantMessage')
                if isinstance(content, list) and not child['text']:
                    child['text'] = ''.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str))[-16000:]
        elif sid == self.root or sid in self.children:
            child = self.children.get(sid)
            attempt = (sid, self._attempts.get(sid, 0))
            if method == 'session.stream':
                frame = payload.get('frame', {})
                if frame.get('type') == 'start':
                    if child:
                        child['status'] = 'running'
                    self._attempts[sid] = self._attempts.get(sid, 0) + 1
                    if not child:
                        self.phase = 'thinking'
                chunk = frame.get('chunk', {})
                attempt = (sid, self._attempts.get(sid, 0))
                text = chunk.get('text')
                if chunk.get('type') == 'reasoning-delta' and isinstance(text, str):
                    self._reasoning_streamed.add(attempt)
                    if child:
                        child['reasoning'] = (child['reasoning'] + text)[-16000:]
                    else:
                        self.reasoning = (self.reasoning + text)[-64000:]
                        self.phase = 'thinking'
                elif chunk.get('type') == 'text-delta' and isinstance(text, str):
                    if child:
                        child['text'] = (child['text'] + text)[-16000:]
                    else:
                        self.phase = 'responding'
                elif chunk.get('type') == 'usage':
                    self._record_usage(attempt, chunk.get('usage'))
            elif method == 'session.context' and not child:
                self.context = {key: count(payload[key]) for key in ('pressureTokens', 'contextWindow') if count(payload.get(key)) is not None}
            elif method == 'session.event':
                event = payload.get('event', {})
                data = event.get('data', {})
                if event.get('type') == 'request/context' and not child:
                    window = count(data.get('contextWindow'))
                    if window is not None:
                        self.context['contextWindow'] = window
                elif event.get('type') in {'tool/call', 'tool/result'}:
                    if not child:
                        self.phase = 'tool'
                    else:
                        message = data.get('message', {})
                        call_id = str(data.get('callId') or message.get('toolCallId') or message.get('callId') or data.get('id') or event.get('seq'))
                        tool = next((tool for tool in child['tools'] if tool['id'] == call_id), None)
                        if event['type'] == 'tool/call':
                            child['status'] = 'running'
                            child['tools'] = (child['tools'] + [{'id': call_id, 'name': str(data.get('name') or data.get('toolName') or 'tool'), 'status': 'running'}])[-30:]
                        elif tool:
                            tool['status'] = 'failed' if message.get('isError') else 'completed'
                elif event.get('type') == 'assistant/message':
                    message = data.get('message', data)
                    source = message.get('source', {})
                    if isinstance(source, dict) and isinstance(source.get('model'), str):
                        self._models[attempt] = source['model']
                    if isinstance(data.get('usage'), dict):
                        self._record_usage(attempt, data['usage'])
                    content = message.get('content', [])
                    if attempt not in self._reasoning_streamed:
                        reasoning = ''.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'reasoning' and isinstance(b.get('text'), str))
                        if child:
                            child['reasoning'] = (child['reasoning'] + reasoning)[-16000:]
                        else:
                            self.reasoning = (self.reasoning + reasoning)[-64000:]
                    if attempt not in self._usage:
                        for record in data.get('stream', []):
                            if isinstance(record, dict) and record.get('type') == 'chunk' and record.get('chunk', {}).get('type') == 'usage':
                                self._record_usage(attempt, record['chunk'].get('usage'))
                elif event.get('type') == 'turn/end' and child:
                    reason = data.get('reason', {}).get('kind')
                    child['status'] = 'failed' if reason == 'error' else 'stopped' if reason == 'aborted' else 'completed'
        return before != self.snapshot()

    @staticmethod
    def _child(child: str, parent: str) -> dict:
        return {'id': child, 'parentId': parent, 'status': 'running', 'text': '', 'reasoning': '', 'tools': []}

    def _record_usage(self, attempt: tuple[str, int], raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        usage = {key: count(raw[key]) for key in ('inputTokens', 'outputTokens', 'totalTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens') if count(raw.get(key)) is not None}
        if 'inputTokens' in usage and 'outputTokens' in usage:
            if not any(usage.values()):
                return  # Native drivers can return zero-filled defaults when a gateway omits usage.
            self._usage[attempt] = usage
            if attempt[0] == self.root and 'totalTokens' in usage and usage['totalTokens'] >= usage['outputTokens']:
                self.context['pressureTokens'] = usage['totalTokens'] - usage['outputTokens']
            child = self.children.get(attempt[0])
            if child:
                samples = [u for (sid, _), u in self._usage.items() if sid == attempt[0]]
                child['usage'] = {key: sum(u[key] for u in samples) for key in ('inputTokens', 'outputTokens', 'totalTokens') if all(key in u for u in samples)}
