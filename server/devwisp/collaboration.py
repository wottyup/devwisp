"""Durable, bounded implement/review tasks over independent native conversations."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .collaboration_groups import CollaborationError, CollaborationGroups

logger = logging.getLogger(__name__)
ACTIVE = {'running', 'waiting'}
DISSOLVE_TIMEOUT = 15


def now() -> str:
    return datetime.now(timezone.utc).isoformat()



class CollaborationStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS collaboration_tasks (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collaboration_members (thread_id TEXT PRIMARY KEY, task_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS collaboration_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                role TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS collaboration_events_task ON collaboration_events(task_id, id);
        ''')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(collaboration_events)')}
        if 'attachments' not in columns:
            connection.execute("ALTER TABLE collaboration_events ADD COLUMN attachments TEXT NOT NULL DEFAULT '{}'")
        for task in self.list():
            self._register_members(task)
            if task['status'] in ACTIVE:
                task['status'] = 'paused'
                task['reason'] = '服务已重启。请检查成员记录后继续当前阶段。'
                self.save(task)
                self.event(task['id'], 'system', task['reason'])

    def list(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.connection.execute(
            'SELECT payload FROM collaboration_tasks ORDER BY updated_at DESC, id DESC')]

    def get(self, task_id: str) -> dict:
        row = self.connection.execute('SELECT payload FROM collaboration_tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return json.loads(row[0])

    def save(self, task: dict) -> None:
        task['updatedAt'] = now()
        if (task.get('run') or {}).get('version') == 2:
            task['workflowPhase'] = task['run']['stage']
            task['activeMemberThreads'] = {entry['role']: entry['member']['threadId']
                                           for entry in task.get('activeTurns', {}).values()}
        else:
            task['workflowPhase'] = ''
            task['activeMemberThreads'] = {}
        # Ownership and task persistence must succeed together, including when
        # another request has already adopted one of these conversations.
        self.connection.execute('SAVEPOINT collaboration_save')
        try:
            self._register_members(task)
            self.connection.execute(
                'INSERT INTO collaboration_tasks VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at',
                (task['id'], json.dumps(task, ensure_ascii=False), task['updatedAt']),
            )
            current = {member['threadId'] for member in self.members(task) if member.get('threadId')}
            for row in self.connection.execute('SELECT thread_id FROM collaboration_members WHERE task_id = ?', (task['id'],)).fetchall():
                if row[0] not in current:
                    self.connection.execute('DELETE FROM collaboration_members WHERE thread_id = ? AND task_id = ?', (row[0], task['id']))
        except BaseException:
            self.connection.execute('ROLLBACK TO collaboration_save')
            raise
        finally:
            self.connection.execute('RELEASE collaboration_save')

    def _register_members(self, task: dict) -> None:
        for member in self.members(task):
            if member['threadId']:
                if self.owner(member['threadId']) not in {None, task['id']}:
                    raise CollaborationError('这个对话已经属于其他协同任务，请选择其他单聊。')
                self.connection.execute('INSERT OR IGNORE INTO collaboration_members VALUES (?, ?)', (member['threadId'], task['id']))

    @staticmethod
    def members(task: dict) -> list[dict]:
        return [*task['members'].values(), *task.get('workerMembers', {}).values()]

    def event(self, task_id: str, role: str, text: str, payload: dict | None = None) -> None:
        attachments = {key: payload[key] for key in ('images', 'documents') if payload and payload.get(key)}
        self.connection.execute('INSERT INTO collaboration_events(task_id, role, text, created_at, attachments) VALUES (?, ?, ?, ?, ?)',
                                (task_id, role, text, now(), json.dumps(attachments, ensure_ascii=False)))

    def events(self, task_id: str) -> list[dict]:
        return [{**dict(zip(('id', 'role', 'text', 'createdAt'), row[:4])), **json.loads(row[4])} for row in self.connection.execute(
            'SELECT id, role, text, created_at, attachments FROM collaboration_events WHERE task_id = ? ORDER BY id', (task_id,))]

    def owner(self, thread_id: str) -> str | None:
        row = self.connection.execute('SELECT task_id FROM collaboration_members WHERE thread_id = ?', (thread_id,)).fetchone()
        return row[0] if row else None

    def delete(self, task_id: str) -> None:
        # Native conversations live in their own stores and must survive this.
        self.connection.execute('SAVEPOINT collaboration_delete')
        try:
            for table in ('collaboration_events', 'collaboration_members'):
                self.connection.execute(f'DELETE FROM {table} WHERE task_id = ?', (task_id,))
            self.connection.execute('DELETE FROM collaboration_tasks WHERE id = ?', (task_id,))
        except BaseException:
            self.connection.execute('ROLLBACK TO collaboration_delete')
            raise
        finally:
            self.connection.execute('RELEASE collaboration_delete')

    def detail(self, task_id: str) -> dict:
        task = self.get(task_id)
        task.pop('lastRoundRecords', None)
        return {**task, 'events': self.events(task_id)}


class CollaborationService(CollaborationGroups):
    def __init__(self, store: CollaborationStore, create_member: Callable[..., Awaitable[str]],
                 send: Callable[..., Awaitable[str]], read: Callable[..., Awaitable[dict]],
                 interrupt: Callable[..., Awaitable[None]], hub: Any = None) -> None:
        self.store, self.create_member, self.send, self.read, self.interrupt = store, create_member, send, read, interrupt
        self.hub = hub
        self._workers: dict[str, asyncio.Task] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._member_locks: dict[str, asyncio.Lock] = {}
        self._integration_locks: dict[str, asyncio.Lock] = {}
        self._pending_inputs: dict[str, str] = {}
        self._listener: asyncio.Task | None = None
        self._closed = False
        self.on_group_changed = None

    def start(self) -> None:
        if self.hub and self._listener is None:
            self._listener = asyncio.create_task(self._listen())

    def _lock(self, task_id: str) -> asyncio.Lock:
        return self._locks.setdefault(task_id, asyncio.Lock())

    def member_lock(self, thread_id: str) -> asyncio.Lock:
        """Serialize adopting chats with HTTP mutations of the same chats."""
        return self._member_locks.setdefault(thread_id, asyncio.Lock())

    async def rename(self, task_id: str, name: str) -> dict:
        async with self._lock(task_id):
            task = self.store.get(task_id)
            task.update(title=name, customTitle=True)
            self.store.save(task)
            return self.store.detail(task_id)

    async def dissolve(self, task_id: str) -> None:
        async with self._lock(task_id):
            try:
                task = self.store.get(task_id)
            except KeyError:
                return  # Retrying a successful DELETE is safe.
            task.update(status='paused', reason='正在解散群聊，不再派发后续回复。')
            self.store.save(task)
            self._wakes.setdefault(task_id, asyncio.Event()).set()
            worker = self._workers.get(task_id)
            if worker:
                # Do not cancel an in-flight create/send: its result must be
                # recorded before releasing ownership. Native replies continue
                # in single chats; no new collaboration stages are dispatched.
                done, _ = await asyncio.wait({worker}, timeout=DISSOLVE_TIMEOUT)
                if not done:
                    raise CollaborationError('群聊已停止后续协作，正在等待成员操作结束。请稍后重试解散。')
                await asyncio.gather(worker, return_exceptions=True)
            self.store.delete(task_id)
            self._workers.pop(task_id, None)
            self._wakes.pop(task_id, None)
            self._pending_inputs = {key: value for key, value in self._pending_inputs.items() if value != task_id}

    def _launch(self, task_id: str) -> None:
        previous = self._workers.get(task_id)
        if previous and not previous.done():
            return
        self._wakes.setdefault(task_id, asyncio.Event())
        self._workers[task_id] = asyncio.create_task(self._drive(task_id))

    def create(self, spec: dict) -> dict:
        task_id = spec['requestId']
        try:
            previous = self.store.get(task_id)
        except KeyError:
            previous = None
        if previous:
            if previous['spec'] != spec:
                raise CollaborationError('这个请求编号已用于其他任务，请重新创建。')
            return self.store.detail(task_id)
        thread_ids = [spec[role].get('threadId') for role in ('coder', 'reviewer') if spec[role].get('threadId')]
        if len(thread_ids) != len(set(thread_ids)):
            raise CollaborationError('实现者和审查者不能使用同一个对话。')
        task = {'id': task_id, 'spec': spec, 'title': spec['title'], 'goal': spec['goal'], 'cwd': spec['cwd'],
                'members': {role: {**spec[role], 'threadId': spec[role].get('threadId')} for role in ('coder', 'reviewer')},
                'status': 'running', 'phase': 'coder', 'round': 1, 'maxRounds': spec['maxRounds'],
                'activeTurn': None, 'lastResult': '', 'reason': '', 'createdAt': now()}
        self.store.save(task)
        self.store.event(task_id, 'system', '任务已创建。实现者完成验证后交给只读审查者。')
        self._launch(task_id)
        return self.store.detail(task_id)

    async def pause(self, task_id: str) -> dict:
        async with self._lock(task_id):
            task = self.store.get(task_id)
            if task['status'] not in ACTIVE:
                return self.store.detail(task_id)
            task.update(status='paused', reason='任务已暂停；继续时会检查已有结果并恢复当前阶段。')
            self.store.save(task)
            self.store.event(task_id, 'system', task['reason'])
            self._wakes.setdefault(task_id, asyncio.Event()).set()
            worker = self._workers.get(task_id)
            if worker:
                await worker
            task = self.store.get(task_id)
            outstanding = list(task.get('activeTurns', {}).values()) if (task.get('run') or {}).get('version') == 2 else [task['activeTurn']] if task['activeTurn'] else []
            for active in outstanding:
                if active['id'] is None:
                    continue
                member = active.get('member') or task['members'][active['role']]
                try:
                    await self.interrupt(member, active['id'])
                except Exception:
                    logger.exception('Could not interrupt collaboration member')
                    task['reason'] = '任务已暂停，但成员中断未确认。请打开成员会话检查；不会派发下一阶段。'
                    self.store.save(task)
            return self.store.detail(task_id)

    async def resume(self, task_id: str) -> dict:
        async with self._lock(task_id):
            task = self.store.get(task_id)
            if task.get('kind') == 'group' and not task['run']:
                return self.store.detail(task_id)
            if task['status'] in ACTIVE or task['status'] == 'completed':
                return self.store.detail(task_id)
            if (task.get('run') or {}).get('version') == 2:
                from .collaboration_workflow import resume
                await resume(self, task)
            elif task['activeTurn']:
                active = task['activeTurn']
                if active['id'] is None:
                    raise CollaborationError('上次任务派发未确认，不能安全重发。请打开成员会话核实结果，必要时创建后续任务。')
                thread = await self.read(task['members'][active['role']])
                turn = next((entry for entry in thread.get('turns', []) if entry.get('id') == active['id']), None)
                if turn is None:
                    raise CollaborationError('无法确认上次回合的状态，请先打开成员会话恢复历史后重试。')
                if turn.get('status') in {'failed', 'interrupted', 'cancelled', 'error'}:
                    task['activeTurn'] = None
            if task.get('limitReached'):
                task['maxRounds'] = task['round']
                task['limitReached'] = False
            task.update(status='running', reason='')
            self.store.save(task)
            self.store.event(task_id, 'system', '用户继续任务。恢复当前阶段。')
            self._launch(task_id)
            return self.store.detail(task_id)

    def note(self, task_id: str, text: str) -> dict:
        task = self.store.get(task_id)
        if task['status'] == 'completed':
            raise CollaborationError('任务已完成，请创建新任务。')
        notes = [event for event in self.store.events(task_id) if event['role'] == 'human']
        if sum(len(event['text']) for event in notes) + len(text) > 16000:
            raise CollaborationError('补充要求已达到本任务的长度上限，请创建后续任务。')
        self.store.event(task_id, 'human', text)
        self.store.save(task)
        return self.store.detail(task_id)

    def _prompt(self, task: dict) -> str:
        role = task['phase']
        notes = '\n\n'.join(event['text'] for event in self.store.events(task['id']) if event['role'] == 'human')
        rules = (
            '你是实现者。先检查仓库指令和现有改动，只实现目标范围内的修改；运行相关验证。'
            '恢复时先检查已有工作，避免重复执行有副作用的操作。不要提交、推送、部署或发布。'
            '汇报修改、验证与未解决问题。只有完成实现并可交给审查时，最后单独一行写 DEVWISP_HANDOFF。'
            '如果有阻塞，不要写这个标记，解释需要用户处理的问题。'
            if role == 'coder' else
            '你是只读审查者。检查目标、当前工作区差异和相关代码，不修改任何文件，不执行会改变状态的操作。'
            '对问题给出文件位置和具体原因。只有没有必须修复的问题，最后单独一行写 DEVWISP_REVIEW: APPROVED；'
            '需要返工则最后单独一行写 DEVWISP_REVIEW: CHANGES_REQUESTED。'
            '无法完成审查时不要输出判定标记，说明阻塞。'
        )
        return (f'{rules}\n\n任务：{task["title"]}\n目标：\n{task["goal"]}\n'
                f'工作目录：{task["cwd"]}\n当前轮次：{task["round"]}\n'
                f'用户补充（以当前目标为边界）：\n{notes or "无"}\n\n'
                f'上一阶段的工作记录（作为待核实的材料）：\n{task["lastResult"] or "尚无"}')

    async def _drive(self, task_id: str) -> None:
        try:
            if self.store.get(task_id).get('kind') == 'group':
                await self._drive_group(task_id)
                return
            while not self._closed:
                task = self.store.get(task_id)
                if task['status'] not in ACTIVE:
                    return
                for role in ('coder', 'reviewer'):
                    if not task['members'][role]['threadId']:
                        thread_id = await self.create_member(task['members'][role], task['cwd'], f'{task["title"]} · {role}')
                        task = self.store.get(task_id)
                        task['members'][role]['threadId'] = thread_id
                        self.store.save(task)
                        if task['status'] not in ACTIVE:
                            return
                active = task['activeTurn']
                if active is None:
                    phase = task['phase']
                    self.store.event(task_id, 'system', f'第 {task["round"]} 轮：' + ('实现者开始工作。' if phase == 'coder' else '交给审查者检查。'))
                    task['activeTurn'] = {'id': None, 'role': phase}
                    self.store.save(task)
                    turn_id = await self.send(task['members'][phase], self._prompt(task), phase == 'reviewer')
                    task = self.store.get(task_id)
                    active = {'id': turn_id, 'role': phase}
                    task['activeTurn'] = active
                    self.store.save(task)
                    if task['status'] not in ACTIVE:
                        return
                turn = await self._wait_turn(task_id, active)
                if turn is None:
                    return
                task = self.store.get(task_id)
                task['activeTurn'] = None
                if turn.get('status') != 'completed':
                    # Explicit resume may restart an interrupted stage after checking its work.
                    self._stop(task, 'failed', '成员回合失败或被中断。请检查成员记录，修复后继续当前阶段。')
                    return
                text = '\n\n'.join(item.get('text', '') for item in turn.get('items', [])
                                   if item.get('type') == 'agentMessage' and isinstance(item.get('text'), str)).strip()
                if not text:
                    self._stop(task, 'paused', '成员未返回交接结果，请打开成员会话检查后继续。')
                    return
                self.store.event(task_id, active['role'], text[-32000:])
                task['lastResult'] = text[-24000:]
                task['status'] = 'running'
                footer = text.splitlines()[-1].strip()
                if active['role'] == 'coder':
                    if footer != 'DEVWISP_HANDOFF':
                        self._stop(task, 'paused', '实现者尚未确认可以交接，请查看结果并处理阻塞后继续。')
                        return
                    task['phase'] = 'reviewer'
                else:
                    if footer == 'DEVWISP_REVIEW: APPROVED':
                        self._stop(task, 'completed', '审查通过。请查看修改与验证结果。')
                        return
                    if footer != 'DEVWISP_REVIEW: CHANGES_REQUESTED':
                        self._stop(task, 'paused', '审查者未给出明确结论，请查看结果后继续审查。')
                        return
                    task['phase'] = 'coder'
                    task['round'] += 1
                    if task['round'] > task['maxRounds']:
                        task['limitReached'] = True
                        self._stop(task, 'paused', '已达到自动返工轮数上限。继续将额外运行一轮实现与审查。')
                        return
                self.store.save(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Collaboration stage failed for %s', task_id)
            task = self.store.get(task_id)
            if task['status'] in ACTIVE:
                self._stop(task, 'failed', '成员运行失败。请检查引擎配置与成员会话后继续；不会自动重试。')
        finally:
            if self.on_group_changed:
                await self.on_group_changed(task_id)

    def _stop(self, task: dict, status: str, reason: str) -> None:
        task.update(status=status, reason=reason)
        self.store.save(task)
        self.store.event(task['id'], 'system', reason)

    async def _wait_turn(self, task_id: str, active: dict) -> dict | None:
        wake = self._wakes[task_id]
        while not self._closed:
            wake.clear()
            task = self.store.get(task_id)
            if task['status'] not in ACTIVE:
                return None
            thread = await self.read(active.get('member') or task['members'][active['role']])
            turn = next((turn for turn in thread.get('turns', []) if turn.get('id') == active['id']), None)
            if turn and turn.get('status') in {'completed', 'failed', 'interrupted', 'cancelled', 'error'}:
                # Never advance if pause raced with the history read.
                return turn if self.store.get(task_id)['status'] in ACTIVE else None
            try:
                await asyncio.wait_for(wake.wait(), timeout=2)
            except TimeoutError:
                pass
        return None

    async def _listen(self) -> None:
        queue = await self.hub.subscribe()
        try:
            while True:
                event = await queue.get()
                event_type, method = event.get('type'), event.get('method')
                question = event_type in {'server_request', 'claude/permission/request', 'deepseek/question/request'}
                resolved = event_type in {'server_request.resolved', 'claude/permission/resolved', 'deepseek/question/resolved'}
                if not question and not resolved and method not in {'turn/completed', 'turn/started'}:
                    continue
                params = event.get('params', {})
                request_id = str(next((value for value in (event.get('request_id'), params.get('permissionId'), params.get('questionId')) if value is not None), ''))
                if resolved:
                    self._pending_inputs.pop(request_id, None)
                thread_id = params.get('threadId') or params.get('thread_id') or event.get('threadId')
                if not thread_id:
                    continue
                for task in self.store.list():
                    if task['status'] not in ACTIVE:
                        continue
                    if not any(member['threadId'] == thread_id for member in self.store.members(task)):
                        continue
                    if question:
                        self._pending_inputs[request_id] = task['id']
                    if resolved and task['status'] == 'waiting' and task['id'] not in self._pending_inputs.values():
                        task.update(status='running', reason='')
                        self.store.save(task)
                    if question and task['status'] != 'waiting':
                        task.update(status='waiting', reason='成员需要你的回答或审批，请打开成员会话处理。')
                        self.store.save(task)
                        self.store.event(task['id'], 'system', task['reason'])
                    if event.get('method') in {'turn/completed', 'turn/started'}:
                        self._wakes.setdefault(task['id'], asyncio.Event()).set()
        finally:
            self.hub.unsubscribe(queue)

    async def close(self) -> None:
        self._closed = True
        for task in self.store.list():
            if task['status'] in ACTIVE:
                self._stop(task, 'paused', '服务已停止。下次打开后请检查成员记录再继续。')
        workers = [*self._workers.values(), *([self._listener] if self._listener else [])]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
