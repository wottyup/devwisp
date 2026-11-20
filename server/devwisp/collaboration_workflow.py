"""Bounded discussion -> assignment -> isolated development -> summary rounds."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .collaboration_groups import CollaborationError
from .collaboration_plan import TASK_COMPLETE, discussion_result, next_batch, parse_plan, protocol_result
from . import collaboration_workspace as workspace

logger = logging.getLogger(__name__)
ACTIVE = {'running', 'waiting'}


def is_active(service, group_id):
    return not service._closed and service.group(group_id)['status'] in ACTIVE


def set_stage(service, group_id, stage):
    group = service.group(group_id)
    group['run']['stage'] = stage
    group['phase'] = stage
    service.store.save(group)


def finish(service, group_id, reason=''):
    group = service.group(group_id)
    group['lastRoundRecords'] = [{'role': entry['role'], 'text': discussion_result(entry['text'])[0]}
                                  for key, entry in group['run']['turns'].items()
                                  if key != '_plan' and entry.get('status') == 'completed']
    group.update(status='idle', run=None, phase='', activeTurn=None, activeTurns={}, reason=reason)
    service.store.save(group)
    if reason:
        service.store.event(group_id, 'system', reason)


def results(group):
    records = group['run']['turns']
    return '\n\n'.join(f'[{group["members"][entry["role"]]["name"]}]\n{discussion_result(entry["text"])[0]}'
                        for entry in records.values() if entry.get('status') == 'completed')[-64000:]


async def turn(service, group_id, key, role, prompt, readonly=True, member=None, publish=True):
    group = service.group(group_id)
    previous = group['run']['turns'].get(key)
    if previous and previous['status'] == 'completed':
        return previous['text']
    if not is_active(service, group_id):
        return None
    if previous is None:
        native = member or group['members'][role]
        if not native.get('threadId'):
            thread_id = await service.create_member(native, group['cwd'], native['name'])
            group = service.group(group_id)
            group['members'][role]['threadId'] = thread_id
            service.store.save(group)
            native = group['members'][role]
        if not is_active(service, group_id):
            return None
        group = service.group(group_id)
        native = {**native, 'groupInput': group['run'].get('input', {}), 'groupWorkflow': True,
                  'groupCwd': native.get('cwd') or group['cwd']}
        previous = {'role': role, 'id': None,
                    'member': {key: value for key, value in native.items() if key != 'groupInput'},
                    'status': 'dispatching'}
        group['run']['turns'][key] = previous
        group.setdefault('activeTurns', {})[key] = previous.copy()
        service.store.save(group)
        turn_id = await service.send(native, prompt, readonly)
        if not turn_id:
            raise CollaborationError('成员派发未返回回合编号，不能安全重发，请打开成员会话核实。')
        group = service.group(group_id)
        previous = {**previous, 'id': turn_id, 'status': 'running'}
        group['run']['turns'][key] = previous
        group['activeTurns'][key] = previous.copy()
        service.store.save(group)
    if previous['id'] is None:
        raise CollaborationError('上次成员派发未确认，请打开成员会话核实，不能自动重发。')
    if not is_active(service, group_id):
        return None
    completed = await service._wait_turn(group_id, previous)
    if completed is None:
        return None
    if completed.get('status') != 'completed':
        raise CollaborationError('成员回复被中断或失败，检查成员会话后可继续本轮。')
    text = '\n\n'.join(item.get('text', '') for item in completed.get('items', [])
                       if item.get('type') == 'agentMessage' and isinstance(item.get('text'), str)).strip()[-64000:]
    if not text:
        raise CollaborationError('成员没有返回内容，请检查成员会话后继续。')
    group = service.group(group_id)
    if group['status'] not in ACTIVE:
        return None
    service.store.connection.execute('SAVEPOINT team_turn')
    try:
        group['run']['turns'][key] = {**previous, 'status': 'completed', 'text': text}
        group['activeTurns'].pop(key, None)
        if publish:
            visible, _ = discussion_result(text)
            service.store.event(group_id, role, visible or '本轮分析已完成。')
        service.store.save(group)
    except BaseException:
        service.store.connection.execute('ROLLBACK TO team_turn')
        raise
    finally:
        service.store.connection.execute('RELEASE team_turn')
    return text


async def parallel(service, group_id, calls):
    outcomes = await asyncio.gather(*(turn(service, group_id, **call) for call in calls), return_exceptions=True)
    if not is_active(service, group_id):
        return
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


def planning_prompt(service, group, role):
    roster = '\n'.join(f'{key}: {group["members"][key]["name"]}' for key in group['run']['memberIds'])
    return (service._group_prompt(group, role) + '\n\n本轮所有成员的讨论已收齐：\n' + results(group)
            + '\n\n你现在仅负责只读整理方案，不执行开发。只有当前用户明确要求实施、且讨论足以形成可靠分工时，才能 action=develop。'
            '问候、测试、比较方案、咨询、仅讨论或仍有关键分歧时 action=discuss。历史里的实施要求不能代替当前用户授权。'
            '将实际所需变更拆成最多 12 个任务，可同一成员承担多项任务；只能分配给本轮被选中的成员。'
            'files 必须列出项目内具体相对文件名，包含新增文件；不能填目录、通配符、绝对路径或依赖目录。'
            'dependsOn 列出必须先完成并合入的任务编号。重叠文件的任务会自动串行。'
            '开发仅限本地代码和验证，不得安排提交、推送、部署、发布或删除项目。'
            '\n可分配成员编号：\n' + roster
            + '\n只返回一个 JSON 对象，不添加解释或协议标记。格式：'
            '{"action":"discuss","summary":"方案、分歧或需要补充的信息"} 或 '
            '{"action":"develop","summary":"讨论后的方案",'
            '"tasks":[{"id":"t1","member":"成员编号","goal":"具体实现及验证要求",'
            '"files":["src/file.ts"],"dependsOn":[]}]}')


def plan_text(group, plan):
    lines = ['方案与分工', plan['summary']]
    for task in plan['tasks']:
        name = group['members'][task['member']]['name']
        dependency = '；等待 ' + ', '.join(task['dependsOn']) if task['dependsOn'] else ''
        lines.append(f'- {name}（{task["id"]}）：{task["goal"]}\n  文件：{", ".join(task["files"])}{dependency}')
    return '\n\n'.join(lines)


async def prepare_job(service, group_id, task):
    group = service.group(group_id)
    job = group['run']['jobs'].get(task['id'])
    if job is None:
        for name in task['files']:
            workspace.checked_path(Path(group['cwd']).resolve(), name)
        lock = service._integration_locks.setdefault(str(Path(group['cwd']).resolve()), asyncio.Lock())
        async with lock:
            context = await asyncio.to_thread(workspace.prepare, group['cwd'])
        group = service.group(group_id)
        job = {'workspace': context, 'status': 'pending', 'workerKey': group['run']['requestId'] + ':' + task['id']}
        group['run']['jobs'][task['id']] = job
        original = group['members'][task['member']]
        settings = (group['run'].get('input', {}).get('collaborationSettings') or {}).get(original['engine'], {})
        group.setdefault('workerMembers', {})[job['workerKey']] = {
            **original, 'model': settings.get('model') or original.get('model', ''), 'threadId': None,
            'role': task['member'], 'cwd': context['cwd']}
        service.store.save(group)
    if not is_active(service, group_id):
        return None
    group = service.group(group_id)
    worker = group['workerMembers'][job['workerKey']]
    if not worker['threadId']:
        thread_id = await service.create_member(worker, job['workspace']['cwd'], worker['name'])
        group = service.group(group_id)
        group['workerMembers'][job['workerKey']]['threadId'] = thread_id
        service.store.save(group)
        worker = group['workerMembers'][job['workerKey']]
    return worker


async def develop(service, group_id):
    group = service.group(group_id)
    tasks = group['run']['plan']['tasks']
    done = {key for key, job in group['run']['jobs'].items() if job['status'] == 'merged'}
    while len(done) < len(tasks) and is_active(service, group_id):
        batch = next_batch(tasks, done)
        if not batch:
            raise CollaborationError('当前分工无法继续，请检查文件范围和前置任务。')
        calls = []
        for task in batch:
            if not is_active(service, group_id):
                return
            worker = await prepare_job(service, group_id, task)
            if worker is None:
                return
            group = service.group(group_id)
            prompt = (service._group_prompt(group, task['member'], worker['cwd'])
                      + '\n\n已确定的方案与分工：\n' + plan_text(group, group['run']['plan'])
                      + '\n\n前面阶段的结果（需要核实）：\n' + results(group)
                      + f'\n\n你的任务：{task["goal"]}\n允许修改的文件：{", ".join(task["files"])}'
                      '\n当前目录是独立开发快照，包含创建时的源码和已合入的前置任务。'
                      '只能在这个目录内修改指定文件，不要操作原项目、其他成员目录、Git 元数据或依赖目录。'
                      '不要自行提交、推送、部署或安装新依赖。运行相关验证，直接报告变更、验证及阻塞。'
                      '后台会检查实际修改范围及源码是否变化，再合入结果；无法完成时明确说明。'
                      '只有任务实际完成且相关验证通过时，另起一行添加 <devwisp:task-complete/>。'
                      '内部协议会隐藏。存在阻塞、失败验证或未完成工作时不要添加，后台会保留成果并暂停后续任务。')
            calls.append({'key': 'job:' + task['id'], 'role': task['member'], 'prompt': prompt,
                          'readonly': False, 'member': worker})
        await parallel(service, group_id, calls)
        if not is_active(service, group_id):
            return
        for task in batch:
            if not is_active(service, group_id):
                return
            group = service.group(group_id)
            job = group['run']['jobs'][task['id']]
            if TASK_COMPLETE not in protocol_result(group['run']['turns']['job:' + task['id']]['text'])[1]:
                job['status'] = 'blocked'
                service.store.save(group)
                raise CollaborationError('成员尚未确认任务完成或验证通过，后续分工已暂停；处理后可继续。')
            job['status'] = 'merging'
            service.store.save(group)
            lock = service._integration_locks.setdefault(str(Path(group['cwd']).resolve()), asyncio.Lock())
            async with lock:
                changed = await asyncio.to_thread(workspace.integrate, job['workspace'], task['files'])
            group = service.group(group_id)
            group['run']['jobs'][task['id']].update(status='merged', files=changed)
            service.store.save(group)
            service.store.event(group_id, 'system', f'{group["members"][task["member"]]["name"]}（{task["id"]}）'
                                + ('已合入：' + ', '.join(changed) if changed else '未产生文件变更。'))
            done.add(task['id'])
            try:
                await asyncio.to_thread(workspace.cleanup, job['workspace'])
            except Exception:
                logger.warning('Could not clean completed collaboration workspace', exc_info=True)


async def drive(service, group_id):
    try:
        while is_active(service, group_id):
            group = service.group(group_id)
            run, role = group['run'], group['run']['memberIds'][0]
            stage = run['stage']
            if stage == 'discussion':
                await parallel(service, group_id, [
                    {'key': member_id, 'role': member_id, 'prompt': service._group_prompt(group, member_id)}
                    for member_id in run['memberIds']])
                if not is_active(service, group_id):
                    return
                group = service.group(group_id)
                if not any(discussion_result(group['run']['turns'][key]['text'])[1] for key in run['memberIds']):
                    finish(service, group_id)
                    return
                set_stage(service, group_id, 'planning')
            elif stage == 'planning':
                text = await turn(service, group_id, '_plan', role, planning_prompt(service, group, role), publish=False)
                if text is None:
                    return
                try:
                    plan = parse_plan(text, run['memberIds'])
                except CollaborationError as error:
                    finish(service, group_id, str(error))
                    return
                service.store.event(group_id, 'system', plan_text(group, plan))
                if plan['action'] == 'discuss':
                    finish(service, group_id)
                    return
                group = service.group(group_id)
                group['run']['plan'] = plan
                service.store.save(group)
                set_stage(service, group_id, 'developing')
            elif stage == 'developing':
                await develop(service, group_id)
                if not is_active(service, group_id):
                    return
                set_stage(service, group_id, 'summarizing')
            elif stage == 'summarizing':
                prompt = (service._group_prompt(group, role) + '\n\n方案与分工：\n' + plan_text(group, run['plan'])
                          + '\n\n本轮各阶段的结果：\n' + results(group)
                          + '\n\n后台合入记录：\n' + str({key: job['files'] for key, job in run['jobs'].items()})
                          + '\n\n现在只读检查已合入的源码，汇总完成内容、验证情况及未解决问题。'
                          '成员的自述是待核实材料；不要把未通过的测试或未完成的任务说成成功。'
                          '不要改文件或追加任务，不要输出内部协议，直接回复用户。本轮到此结束。')
                text = await turn(service, group_id, '_summary', role, prompt)
                if text is not None:
                    group = service.group(group_id)
                    group['lastResult'] = text
                    service.store.save(group)
                    finish(service, group_id)
                return
            else:
                raise CollaborationError('无法识别当前协同阶段，请检查群记录。')
    except CollaborationError as error:
        if is_active(service, group_id):
            group = service.group(group_id)
            retained = [job['workspace']['cwd'] for job in group['run']['jobs'].values() if job['status'] != 'merged']
            service._stop(group, 'paused', str(error) + ('\n保留的开发目录：' + ', '.join(retained) if retained else ''))


async def resume(service, group):
    # Inspect every outstanding native turn before changing any durable state.
    retry = []
    for key, active in group.get('activeTurns', {}).items():
        if active['id'] is None:
            raise CollaborationError('上次成员派发未确认，不能安全重发。请打开成员会话核实结果。')
        thread = await service.read(active['member'])
        native = next((entry for entry in thread.get('turns', []) if entry.get('id') == active['id']), None)
        if native is None:
            raise CollaborationError('无法确认上次成员回合状态，请先打开成员会话恢复历史后重试。')
        if native.get('status') in {'failed', 'interrupted', 'cancelled', 'error'}:
            retry.append(key)
    for key in retry:
        group['activeTurns'].pop(key)
        group['run']['turns'].pop(key, None)
    for key, job in group['run']['jobs'].items():
        if job['status'] == 'blocked':
            group['run']['turns'].pop('job:' + key, None)
            job['status'] = 'pending'
