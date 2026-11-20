"""Authenticated HTTP surface and native-engine bindings for collaboration."""
from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .collaboration import CollaborationError, CollaborationService, CollaborationStore
from .claude_code import ClaudeCodeError, is_claude_thread_id
from .codex_app_server import CodexRuntimeError
from .deepseek_harness import is_deepseek_thread_id


class MemberInput(BaseModel):
    engine: Literal['codex', 'claude-code', 'deepseek-harness']
    model: str = Field(default='', max_length=160)
    threadId: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator('threadId')
    @classmethod
    def clean_thread_id(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(char) < 33 or char in '/\\' for char in value)):
            raise ValueError('对话编号无效')
        return value

    @field_validator('model')
    @classmethod
    def clean_model(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError('模型名称包含无效字符')
        return value.strip()


class CollaborationInput(BaseModel):
    requestId: UUID
    title: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=8000)
    cwd: str = Field(min_length=1, max_length=4096)
    coder: MemberInput
    reviewer: MemberInput
    maxRounds: int = Field(default=3, ge=1, le=5, strict=True)

    @field_validator('title', 'goal', 'cwd')
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or '\x00' in value:
            raise ValueError('内容不能为空或包含空字符')
        return value.strip()


class NoteInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)

    @field_validator('text')
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or '\x00' in value:
            raise ValueError('请填写补充要求')
        return value.strip()


class CollaborationNameInput(BaseModel):
    name: str = Field(min_length=1, max_length=72)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value: str) -> str:
        name = ' '.join(value.split())
        if not name or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError('对话名称不能为空或包含控制字符')
        return name


class GroupInput(BaseModel):
    requestId: UUID
    cwd: str | None = Field(default=None, max_length=4096)
    member: MemberInput | None = None


class GroupMemberInput(MemberInput):
    requestId: UUID


class GroupMessageInput(BaseModel):
    requestId: UUID
    text: str = Field(min_length=1, max_length=8000)

    @field_validator('text')
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or '\x00' in value:
            raise ValueError('请填写消息')
        return value.strip()


def install_collaboration(app, connection, codex, claude, deepseek, resolve_workspace, shared_queue, send_group_member=None) -> CollaborationService:
    def engine(member):
        return {'codex': codex, 'claude-code': claude, 'deepseek-harness': deepseek}[member['engine']]

    async def create_member(member, cwd, title):
        native = engine(member)
        if member['engine'] == 'codex':
            thread = await native.start_thread(model=member['model'] or None, cwd=Path(cwd))
        else:
            thread = await native.create_thread(Path(cwd))
        # The thread ID is returned before optional naming, so a naming failure never orphans a conversation.
        return thread['id']

    async def send(member, prompt, readonly):
        if 'groupInput' in member and send_group_member is not None:
            return await send_group_member(member, prompt, member['groupInput'], False, member['groupCwd'], readonly=readonly)
        native = engine(member)
        thread_id = member['threadId']
        model = member['model'] or None
        if member['engine'] == 'codex':
            # Historical threads may not be loaded in the current runtime.
            # Resume the same native ID, then apply the role policy per turn.
            task_id = service.store.owner(thread_id)
            cwd = Path(service.store.get(task_id)['cwd']) if task_id else None
            await native.resume_thread(thread_id, cwd=cwd)
            result = await native.start_turn(thread_id, prompt, model=model, approval_policy='untrusted',
                                             cwd=cwd,
                                             sandbox_policy={'type': 'readOnly'} if readonly else {'type': 'workspaceWrite'})
            turn_id = result.get('turn', {}).get('id')
            if not turn_id:
                raise CollaborationError('引擎没有返回回合编号')
            return turn_id
        options = {'model': model, 'permission_mode': 'plan' if readonly else 'default'}
        if member['engine'] == 'deepseek-harness':
            options['deepseek_permission_mode'] = 'read-only' if readonly else 'workspace-write'
        return await native.start_turn(thread_id, prompt, **options)

    async def read(member):
        return await engine(member).read_thread(member['threadId'], tail_turns=1)

    async def interrupt(member, turn_id):
        await engine(member).interrupt(member['threadId'], turn_id)

    service = CollaborationService(CollaborationStore(connection), create_member, send, read, interrupt, codex)
    service.on_group_changed = lambda group_id: shared_queue.changed('collaboration:' + group_id)
    router = APIRouter(prefix='/api/v1/collaboration')

    async def validate_existing(member, cwd):
        thread_id = member['threadId']
        actual_engine = 'deepseek-harness' if is_deepseek_thread_id(thread_id) else 'claude-code' if is_claude_thread_id(thread_id) else 'codex'
        if actual_engine != member['engine']:
            raise CollaborationError('所选对话与成员引擎不匹配，请重新选择。')
        if service.store.owner(thread_id):
            raise CollaborationError('这个对话已经属于协同任务，请选择其他单聊。')
        if shared_queue.store.queue(thread_id):
            raise CollaborationError('所选对话还有待发送消息，请先发送或移除后再加入。')
        if (actual_engine == 'codex' and codex.handed_off) or app.state.terminal_handoff.is_enabled(thread_id):
            raise CollaborationError('所选对话正在终端中接管，请先结束接管再加入。')
        native = engine(member)
        try:
            summary = await native.read_thread(thread_id, include_turns=False)
            thread = await native.read_thread(thread_id, tail_turns=1)
            active = await shared_queue._active_turn_id(thread_id)
        except (ClaudeCodeError, CodexRuntimeError) as error:
            raise CollaborationError('无法读取所选对话，请刷新单聊列表并确认引擎可用。') from error
        bound_cwd = shared_queue.store.thread_workspace(thread_id) if actual_engine == 'codex' else None
        native_cwd = bound_cwd or summary.get('cwd') or thread.get('cwd')
        try:
            member_cwd = str(resolve_workspace(codex.workspace, native_cwd)) if native_cwd else None
        except HTTPException as error:
            raise CollaborationError('所选对话的项目目录不可用，请先在单聊中检查。') from error
        if not member_cwd or (cwd is not None and member_cwd != cwd):
            raise CollaborationError('所选对话与任务项目目录不同，请选择同一项目的单聊。')
        statuses = [summary.get('status'), thread.get('status')]
        turns = thread.get('turns', [])
        latest = turns[-1] if turns else None
        if latest:
            statuses.append(latest.get('status'))
        terminal = {'idle', 'notloaded', 'completed', 'complete', 'success', 'failed', 'interrupted', 'cancelled', 'error', 'stopped'}
        if active or any(str(value.get('type', '') if isinstance(value, dict) else value).lower() not in terminal
                         for value in statuses if value is not None):
            raise CollaborationError('所选对话仍在回复或等待审批，请处理完成后再加入。')
        barrier = shared_queue.store.barrier(thread_id)
        if barrier:
            if not latest or latest.get('id') != barrier:
                try:
                    thread = await native.read_thread(thread_id)
                except (ClaudeCodeError, CodexRuntimeError) as error:
                    raise CollaborationError('无法核实所选对话上次发送的结果，请先在单聊中检查。') from error
            waited = next((turn for turn in thread.get('turns', []) if turn.get('id') == barrier), None)
            if not waited or str(waited.get('status')).lower() not in {'completed', 'complete', 'success'}:
                raise CollaborationError('所选对话上次发送的结果尚未确认，请先在单聊中核实。')

        return member_cwd

    @router.get('/tasks')
    async def list_tasks():
        return {'tasks': [{key: value for key, value in task.items() if key not in {'spec', 'lastResult', 'activeTurn', 'activeTurns', 'lastRoundRecords', 'memberInputs', 'messageRequests', 'run'}}
                          for task in service.store.list()]}

    @router.post('/tasks', status_code=201)
    async def create_task(body: CollaborationInput):
        try:
            spec = body.model_dump(mode='json')
            spec['cwd'] = str(resolve_workspace(codex.workspace, body.cwd))
            if not Path(spec['cwd']).is_dir():
                raise HTTPException(422, '工作目录不存在或不是文件夹')
            # Omit unset IDs so retries of older new-member requests retain
            # their original persisted specification.
            for role in ('coder', 'reviewer'):
                if not spec[role]['threadId']:
                    spec[role].pop('threadId')
            members = [spec[role] for role in ('coder', 'reviewer') if spec[role].get('threadId')]
            ids = sorted(member['threadId'] for member in members)
            if len(ids) != len(set(ids)):
                raise CollaborationError('实现者和审查者不能使用同一个对话。')
            async with AsyncExitStack() as locks:
                for thread_id in ids:
                    await locks.enter_async_context(service.member_lock(thread_id))
                    await locks.enter_async_context(shared_queue.delivery_lock(thread_id))
                try:
                    service.store.get(spec['requestId'])
                except KeyError:
                    for member in members:
                        await validate_existing(member, spec['cwd'])
                return service.create(spec)
        except CollaborationError as error:
            raise HTTPException(409, str(error)) from error

    def clean_member(member):
        result = member.model_dump(mode='json')
        result.pop('requestId', None)
        if result.get('threadId') is None:
            result.pop('threadId', None)
        return result

    @router.post('/groups', status_code=201)
    async def create_group(body: GroupInput):
        try:
            group_id = str(body.requestId)
            async with service._lock(group_id):
                try:
                    previous = service.store.get(group_id)
                except KeyError:
                    previous = None
                member = clean_member(body.member) if body.member else None
                spec = {'requestId': group_id, 'cwd': str(resolve_workspace(codex.workspace, body.cwd)), 'member': member}
                async with AsyncExitStack() as locks:
                    if member and member.get('threadId'):
                        await locks.enter_async_context(service.member_lock(member['threadId']))
                        await locks.enter_async_context(shared_queue.delivery_lock(member['threadId']))
                        if previous is None:
                            spec['cwd'] = await validate_existing(member, spec['cwd'] if body.cwd else None)
                        elif body.cwd is None:
                            spec['cwd'] = previous['spec']['cwd']
                    result = service.create_group(spec)
                    await codex.notify_threads_changed()
                    return result
        except CollaborationError as error:
            raise HTTPException(409, str(error)) from error

    @router.post('/groups/{group_id}/members', status_code=201)
    async def add_group_member(group_id: UUID, body: GroupMemberInput):
        try:
            key = str(group_id)
            member_id = str(body.requestId)
            member = clean_member(body)
            async with service._lock(key):
                group = service.group(key)
                if member_id in group['memberInputs']:
                    return service.add_group_member(key, member_id, member)
                if group['run']:
                    raise CollaborationError('本轮回复尚未结束，请结束本轮后再调整群成员。')
                async with AsyncExitStack() as locks:
                    cwd = None
                    if member.get('threadId'):
                        await locks.enter_async_context(service.member_lock(member['threadId']))
                        await locks.enter_async_context(shared_queue.delivery_lock(member['threadId']))
                        untouched = not group['goal'] and all(not item['threadId'] for item in group['members'].values())
                        cwd = await validate_existing(member, None if untouched else group['cwd'])
                    result = service.add_group_member(key, member_id, member, cwd)
                    await codex.notify_threads_changed()
                    return result
        except KeyError:
            raise HTTPException(404, '协同群聊不存在') from None
        except CollaborationError as error:
            raise HTTPException(409, str(error)) from error

    @router.delete('/groups/{group_id}/members/{member_id}')
    async def remove_group_member(group_id: UUID, member_id: UUID):
        try:
            result = await service.remove_group_member(str(group_id), str(member_id))
            await codex.notify_threads_changed()
            return result
        except KeyError:
            raise HTTPException(404, '协同群聊不存在') from None
        except CollaborationError as error:
            raise HTTPException(409, str(error)) from error

    @router.post('/groups/{group_id}/messages')
    async def group_message(group_id: UUID, body: GroupMessageInput):
        try:
            return await service.group_message(str(group_id), str(body.requestId), body.text)
        except KeyError:
            raise HTTPException(404, '协同群聊不存在') from None
        except CollaborationError as error:
            raise HTTPException(409, str(error)) from error

    @router.get('/tasks/{task_id}')
    async def read_task(task_id: UUID):
        try:
            return service.store.detail(str(task_id))
        except KeyError:
            raise HTTPException(404, '协同任务不存在') from None

    @router.put('/tasks/{task_id}/name')
    async def rename_task(task_id: UUID, body: CollaborationNameInput):
        try:
            return await service.rename(str(task_id), body.name)
        except KeyError:
            raise HTTPException(404, '协同任务不存在') from None

    @router.delete('/tasks/{task_id}')
    async def dissolve_task(task_id: UUID):
        try:
            queue_id = 'collaboration:' + str(task_id)
            async with shared_queue.delivery_lock(queue_id):
                await service.dissolve(str(task_id))
                shared_queue.store.delete_thread(queue_id)
            await shared_queue.changed(queue_id)
            await codex.notify_threads_changed()
            return {'ok': True}
        except CollaborationError as error:
            raise HTTPException(409, str(error)) from error

    @router.post('/tasks/{task_id}/control/{action}')
    async def control_task(task_id: UUID, action: Literal['pause', 'resume']):
        try:
            result = await (service.pause(str(task_id)) if action == 'pause' else service.resume(str(task_id)))
            if result.get('kind') == 'group':
                shared_queue.store.set_paused('collaboration:' + str(task_id), action == 'pause')
                await shared_queue.changed('collaboration:' + str(task_id))
            return result
        except KeyError:
            raise HTTPException(404, '协同任务不存在') from None
        except CollaborationError as error:
            raise HTTPException(409, str(error)) from error

    @router.post('/tasks/{task_id}/notes')
    async def post_note(task_id: UUID, body: NoteInput):
        try:
            return service.note(str(task_id), body.text)
        except KeyError:
            raise HTTPException(404, '协同任务不存在') from None
        except CollaborationError as error:
            raise HTTPException(409, str(error)) from error

    app.include_router(router)
    return service
