"""Expose a collaboration round to the existing per-conversation queue."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .collaboration_groups import CollaborationError, mention_recipients, payload_recipients

PREFIX = 'collaboration:'


class GroupEngineSettings(BaseModel):
    model: str | None = Field(default=None, max_length=200)
    effort: str | None = Field(default=None, max_length=50)
    approvalPolicy: Literal['on-request', 'untrusted', 'never'] | None = None
    claudePermissionMode: Literal['default', 'manual', 'acceptEdits', 'auto', 'bypassPermissions', 'dontAsk', 'plan'] | None = None
    deepseekPermissionMode: Literal['read-only', 'workspace-write', 'full-access-ask', 'danger-full-access', 'plan'] | None = None
    deepseekAgentPreset: Literal['standard', 'ptc', 'minimal', 'cordis', 'legacy'] | None = None


class CollaborationQueueEngine:
    def __init__(self, service, queue, send_member):
        self.service, self.queue, self.send_member = service, queue, send_member

    async def active_turn_id(self, thread_id):
        group = self.service.group(thread_id.removeprefix(PREFIX))
        if not group['members']:
            return 'awaiting-members'
        return group['run']['requestId'] if group.get('run') else None

    async def read_thread(self, thread_id, *, include_turns=True, tail_turns=None):
        group = self.service.group(thread_id.removeprefix(PREFIX))
        active = group.get('run')
        turns = [{'id': key, 'status': ('inProgress' if group['status'] in {'running', 'waiting'} else 'interrupted')
                  if active and key == active['requestId'] else 'completed', 'items': []}
                 for key in group['messageRequests']]
        return {'id': thread_id, 'cwd': group['cwd'], 'status': {'type': 'active' if active else 'idle'},
                'turns': (turns[-tail_turns:] if tail_turns else turns) if include_turns else []}

    async def queue_turn_snapshot(self, thread_id):
        return await self.read_thread(thread_id, tail_turns=1)

    async def interrupt(self, thread_id, turn_id=None):
        await self.service.pause(thread_id.removeprefix(PREFIX))

    def prepare_payload(self, thread_id, payload):
        group = self.service.group(thread_id.removeprefix(PREFIX))
        payload.pop('collaborationRecipientIds', None)
        recipients = mention_recipients(group, payload.get('originalText') or payload['text'])
        if recipients is not None:
            payload['collaborationRecipientIds'] = recipients
        return payload

    def delivery_block_reason(self, thread_id, payload, steer=False):
        try:
            group = self.service.group(thread_id.removeprefix(PREFIX))
            payload_recipients(group, payload)
            if steer and (payload.get('collaborationRecipientIds') or mention_recipients(group, payload.get('originalText') or payload['text'])):
                return '点名消息会在本轮结束后单独发送给指定成员。'
            if steer and (group.get('run') or {}).get('version') == 2:
                return '消息会在本轮讨论与开发结束后发送。'
        except CollaborationError as error:
            return str(error)
        return None

    async def deliver(self, thread_id, payload, steer):
        group_id = thread_id.removeprefix(PREFIX)
        reason = self.delivery_block_reason(thread_id, payload, steer)
        if reason:
            raise CollaborationError(reason)
        if steer:
            async with self.service._lock(group_id):
                group = self.service.group(group_id)
                active = group.get('activeTurn')
                if not group.get('run') or group['status'] not in {'running', 'waiting'} or not active or not active.get('id'):
                    raise CollaborationError('当前群回复尚未就绪，消息已保留，请稍后核对。')
                member = group['members'][active['role']]
                original = group['run'].get('input', {})
                if (len(original.get('images', [])) + len(payload.get('images', [])) > 8
                        or len(original.get('documents', [])) + len(payload.get('documents', [])) > 8):
                    raise CollaborationError('本轮附件已达到上限，请等本轮结束后再发送。')
                turn_id = await self.send_member(member, payload['requestText'], payload, True, group['cwd'])
                group = self.service.group(group_id)
                if group.get('run') is None or group.get('activeTurn') != active or turn_id != active['id']:
                    raise CollaborationError('群成员回合已变化，请先核对追加消息是否送达。')
                group['run']['text'] += '\n\n用户追加：\n' + payload['requestText']
                # Later members receive the new request and the same attachments.
                original = group['run'].setdefault('input', {})
                original['images'] = [*original.get('images', []), *payload.get('images', [])]
                original['documents'] = [*original.get('documents', []), *payload.get('documents', [])]
                self.service.store.event(group_id, 'human', payload.get('originalText') or payload['text'], payload)
                self.service.store.save(group)
                return group['run']['requestId']
        await self.service.group_message(group_id, payload['id'], payload['requestText'], payload)
        return payload['id']
