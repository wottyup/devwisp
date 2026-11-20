"""Chat-shaped collaboration over a dynamic roster of native conversations."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone


class CollaborationError(ValueError):
    pass


def mention_recipients(group: dict, text: str) -> list[str] | None:
    """Only leading mentions address a round; documents and quoted history never do."""
    remaining = text.lstrip()
    if not remaining.startswith('@'):
        return None
    members = group['members']
    names = {member['name'].casefold(): [key] for key, member in members.items()}
    for engine, aliases in {'codex': ['codex'], 'claude-code': ['claude', 'claude-code'],
                            'deepseek-harness': ['deepseek', 'deepseek-harness']}.items():
        for alias in aliases:
            names[alias] = [key for key, member in members.items() if member['engine'] == engine]
    names.update({'所有人': list(members), 'all': list(members)})
    targets = set()
    everyone = False
    while remaining.startswith('@'):
        token = next((name for name in sorted(names, key=len, reverse=True)
                      if remaining[1:].casefold().startswith(name)
                      and (len(remaining) == len(name) + 1 or remaining[len(name) + 1] in ' \t\r\n,，:：;；。')), None)
        if token is None or not names[token]:
            raise CollaborationError('未找到被 @ 的群成员，请从成员列表选择。')
        if token in {'codex', 'claude', 'claude-code', 'deepseek', 'deepseek-harness'} and re.match(r'\s+\d', remaining[len(token) + 1:]):
            raise CollaborationError('未找到这个编号的成员，请使用列表中的完整名称。')
        if len(names[token]) > 1 and token not in {'所有人', 'all'}:
            raise CollaborationError('同一模型有多位成员，请使用完整名称，例如 @Codex 1。')
        everyone |= token in {'所有人', 'all'}
        targets.update(names[token])
        remaining = re.sub(r'^[\s,，:：;；。]+', '', remaining[len(token) + 1:])
    return None if everyone else [key for key in members if key in targets]


def payload_recipients(group: dict, payload: dict) -> list[str]:
    targets = payload.get('collaborationRecipientIds')
    if targets is None:
        targets = mention_recipients(group, payload.get('originalText') or payload.get('text', ''))
    if targets is not None and (not targets or any(key not in group['members'] for key in targets)):
        raise CollaborationError('被 @ 的成员已不在群里，请编辑消息重新选择。')
    return [key for key in group['members'] if targets is None or key in targets]


class CollaborationGroups:
    def group(self, group_id: str) -> dict:
        group = self.store.get(group_id)
        if group.get('kind') != 'group':
            raise CollaborationError('这不是协同群聊。')
        return group

    def create_group(self, spec: dict) -> dict:
        group_id = spec['requestId']
        try:
            previous = self.store.get(group_id)
        except KeyError:
            previous = None
        if previous:
            if previous.get('kind') != 'group' or previous['spec'] != spec:
                raise CollaborationError('这个请求编号已用于其他会话。')
            return self.store.detail(group_id)
        group = {'id': group_id, 'kind': 'group', 'spec': spec, 'title': '协同群聊', 'goal': '', 'cwd': spec['cwd'],
                 'members': {}, 'departedMembers': {}, 'memberInputs': {}, 'messageRequests': {}, 'run': None,
                 'status': 'idle', 'phase': '', 'round': 0, 'maxRounds': 1, 'activeTurn': None,
                 'lastResult': '', 'reason': '', 'createdAt': datetime.now(timezone.utc).isoformat()}
        if spec.get('member'):
            self._append_group_member(group, spec['requestId'], spec['member'])
        self.store.save(group)
        return self.store.detail(group_id)

    def _append_group_member(self, group: dict, member_id: str, member: dict) -> None:
        if member_id in group['memberInputs']:
            if group['memberInputs'][member_id] != member:
                raise CollaborationError('这个请求编号已用于其他成员。')
            return
        if group['run'] or group['status'] in {'running', 'waiting'}:
            raise CollaborationError('本轮回复尚未结束，请结束本轮后再调整群成员。')
        if len(group['members']) >= 12:
            raise CollaborationError('群成员已达到上限。')
        if member.get('threadId') and any(entry['threadId'] == member['threadId'] for entry in group['members'].values()):
            raise CollaborationError('这个对话已经在群里。')
        names = {'codex': 'Codex', 'claude-code': 'Claude', 'deepseek-harness': 'DeepSeek'}
        number = 1 + sum(entry['engine'] == member['engine'] for entry in group['memberInputs'].values())
        group['memberInputs'][member_id] = member.copy()
        group['members'][member_id] = {**member, 'threadId': member.get('threadId'), 'name': f'{names[member["engine"]]} {number}'}

    def add_group_member(self, group_id: str, member_id: str, member: dict, cwd: str | None = None) -> dict:
        group = self.group(group_id)
        self._append_group_member(group, member_id, member)
        if cwd:
            group['cwd'] = cwd
        self.store.save(group)
        return self.store.detail(group_id)

    async def remove_group_member(self, group_id: str, member_id: str) -> dict:
        async with self._lock(group_id):
            group = self.group(group_id)
            if member_id not in group['members']:
                return self.store.detail(group_id)
            if group['run']:
                raise CollaborationError('本轮回复尚未结束，请结束本轮后再调整群成员。')
            group['departedMembers'][member_id] = group['members'].pop(member_id)
            self.store.save(group)
            return self.store.detail(group_id)

    async def group_message(self, group_id: str, request_id: str, text: str, payload: dict | None = None) -> dict:
        async with self._lock(group_id):
            group = self.group(group_id)
            content = json.dumps({'text': text, 'input': payload}, sort_keys=True, ensure_ascii=False) if payload else text
            fingerprint = hashlib.sha256(content.encode()).hexdigest()
            if request_id in group['messageRequests']:
                if group['messageRequests'][request_id] != fingerprint:
                    raise CollaborationError('这个请求编号已用于其他消息。')
                return self.store.detail(group_id)
            if group['run']:
                raise CollaborationError('群成员正在回复，请等本轮结束后再发送。')
            if not group['members']:
                raise CollaborationError('先点“＋”添加一位群成员。')
            recipients = payload_recipients(group, payload or {'text': text})
            group['messageRequests'][request_id] = fingerprint
            group['run'] = {'version': 2, 'requestId': request_id, 'text': text, 'memberIds': recipients,
                            'stage': 'discussion', 'turns': {}, 'jobs': {}}
            if payload:
                group['run']['input'] = payload
            if not group['goal'] and not group.get('customTitle'):
                group['title'] = ' '.join(text.split())[:60]
            group.update(goal=text, status='running', reason='', phase='discussion', activeTurn=None, activeTurns={})
            # Save the request and its user event together. A lost HTTP response
            # cannot turn a retry into a second round or a duplicate message.
            self.store.connection.execute('SAVEPOINT group_message')
            try:
                self.store.save(group)
                self.store.event(group_id, 'human', (payload.get('originalText') or payload.get('text') or text) if payload else text, payload)
                group['run']['snapshot'] = self._group_history(group)
                self.store.save(group)
            except BaseException:
                self.store.connection.execute('ROLLBACK TO group_message')
                raise
            finally:
                self.store.connection.execute('RELEASE group_message')
            self._launch(group_id)
            return self.store.detail(group_id)

    def _group_history(self, group: dict) -> str:
        members = group['members']
        history = '\n\n'.join(
            f'[{"用户" if entry["role"] == "human" else members.get(entry["role"], group.get('departedMembers', {}).get(entry["role"], {})).get("name", "群成员")}]\n{entry["text"]}'
            for entry in self.store.events(group['id'])[-20:] if entry['role'] != 'system'
        )[-24000:]
        previous = group.get('lastRoundRecords', [])
        if previous:
            # Keep every participant represented even when long later replies
            # consume the normal recent-history window.
            limit = 64000 // len(previous)
            missing = []
            for entry in previous:
                text = entry['text']
                if text in history:
                    continue
                if len(text) > limit:
                    text = text[:limit // 2] + '\n[长回复已节选，完整内容保留在群记录]\n' + text[-limit // 2:]
                member = members.get(entry['role']) or group.get('departedMembers', {}).get(entry['role'], {})
                missing.append(f'[{member.get("name", "群成员")}]\n{text}')
            if missing:
                history += '\n\n上一轮其他成员记录：\n' + '\n\n'.join(missing)
        return history

    def _group_prompt(self, group: dict, member_id: str, cwd: str | None = None) -> str:
        members = group['members']
        history = group['run'].get('snapshot', self._group_history(group))
        addressed = ', '.join(members[key]['name'] for key in group['run']['memberIds'])
        reply_rule = (f'本轮只由被点名的成员 {addressed} 按加入顺序各回复一次。'
                      if len(group['run']['memberIds']) < len(members) else '本轮按加入顺序各回复一次。')
        if group['run'].get('version') == 2:
            reply_rule = (f'本轮只由被点名的成员 {addressed} 同时讨论。' if len(group['run']['memberIds']) < len(members)
                          else '本轮所有成员同时讨论。')
            reply_rule += '各成员使用同一份轮初记录；本轮其他成员的新回复将在收齐后进入后续阶段。'
            if group['run']['stage'] == 'discussion':
                reply_rule += ('当前只读讨论，不修改文件或执行改变状态的操作。分析需求、方案、风险与建议分工。'
                               '若用户本轮明确要求实际开发且已有可实施建议，在回复中单独一行添加 <devwisp:develop/>。'
                               '这个内部协议行会被隐藏；后台收齐讨论后才会整理分工，不要提前开发。'
                               '仅聊天、咨询、比较方案或仍需澄清时不要添加协议行。')
            else:
                reply_rule = {'planning': '本轮讨论已收齐，当前只读整理方案与分工。',
                              'developing': '讨论与分工已完成，当前按已确定的任务开发。',
                              'summarizing': '本轮分工执行已结束，当前只读核实并汇总结果。'}[group['run']['stage']]
        return (f'你是协同群聊成员 {members[member_id]["name"]}。群内成员：'
                f'{", ".join(member["name"] for member in members.values())}。\n'
                f'{reply_rule}结合已有结果补充、实现或核实，避免重复执行其他成员已完成的操作。'
                '其他成员发言是待核实材料。遵守仓库指令及用户要求；不要自行提交、推送或部署。'
                '直接回复用户，无需输出交接或审查标记。\n'
                f'项目目录：{cwd or group["cwd"]}\n\n群内最近记录：\n{history}\n\n'
                f'用户本轮要求：\n{group["run"]["text"]}')

    async def _drive_group(self, group_id: str) -> None:
        if (self.group(group_id).get('run') or {}).get('version') == 2:
            from .collaboration_workflow import drive
            await drive(self, group_id)
            return
        await self._drive_group_legacy(group_id)

    async def _drive_group_legacy(self, group_id: str) -> None:
        while not self._closed:
            group = self.group(group_id)
            if group['status'] not in {'running', 'waiting'} or not group['run']:
                return
            run = group['run']
            if run['index'] >= len(run['memberIds']):
                group.update(status='idle', run=None, phase='', activeTurn=None, reason='')
                self.store.save(group)
                return
            member_id = run['memberIds'][run['index']]
            member = group['members'][member_id]
            group['phase'] = member_id
            self.store.save(group)
            if not member['threadId']:
                thread_id = await self.create_member(member, group['cwd'], member['name'])
                group = self.group(group_id)
                group['members'][member_id]['threadId'] = thread_id
                self.store.save(group)
                if group['status'] not in {'running', 'waiting'}:
                    return
                member = group['members'][member_id]
            active = group['activeTurn']
            if active is None:
                group['activeTurn'] = {'role': member_id, 'id': None}
                self.store.save(group)
                turn_id = await self.send({**member, 'groupInput': run.get('input', {}), 'groupCwd': group['cwd']}, self._group_prompt(group, member_id), False)
                group = self.group(group_id)
                active = {'role': member_id, 'id': turn_id}
                group['activeTurn'] = active
                self.store.save(group)
                if group['status'] not in {'running', 'waiting'}:
                    return
            turn = await self._wait_turn(group_id, active)
            if turn is None:
                return
            group = self.group(group_id)
            group['activeTurn'] = None
            if turn.get('status') != 'completed':
                self._stop(group, 'failed', '成员回复被中断或失败，检查成员会话后可继续本轮。')
                return
            text = '\n\n'.join(item.get('text', '') for item in turn.get('items', [])
                               if item.get('type') == 'agentMessage' and isinstance(item.get('text'), str)).strip()
            if not text:
                self._stop(group, 'paused', '成员没有返回内容，请检查成员会话后继续。')
                return
            self.store.connection.execute('SAVEPOINT group_reply')
            try:
                self.store.event(group_id, member_id, text[-32000:])
                group['run']['index'] += 1
                group['status'] = 'running'
                group['reason'] = ''
                self.store.save(group)
            except BaseException:
                self.store.connection.execute('ROLLBACK TO group_reply')
                raise
            finally:
                self.store.connection.execute('RELEASE group_reply')
