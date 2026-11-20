"""Validate model-proposed work before granting any development turn."""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from .collaboration_groups import CollaborationError

DEVELOP_REQUEST = '<devwisp:develop/>'
TASK_COMPLETE = '<devwisp:task-complete/>'
RESERVED = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}


def protocol_result(text: str) -> tuple[str, set[str]]:
    # A protocol line inside a quoted code block is ordinary discussion.
    visible, markers, fenced = [], set(), False
    for line in text.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        if not fenced and line.strip() in {DEVELOP_REQUEST, TASK_COMPLETE}:
            markers.add(line.strip())
        else:
            visible.append(line)
    return '\n'.join(visible).strip(), markers


def discussion_result(text: str) -> tuple[str, bool]:
    visible, markers = protocol_result(text)
    return visible, DEVELOP_REQUEST in markers


def file_path(value: str) -> str:
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or any(ord(char) < 32 for char in value) or re.search(r'[*?:]', value)):
        raise CollaborationError('分工中的文件路径无效，请使用项目内的具体相对文件名。')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or any(part in RESERVED for part in path.parts):
        raise CollaborationError('分工不能修改项目外的路径或依赖、Git 内部目录。')
    return str(path)


def parse_plan(text: str, members: list[str]) -> dict:
    raw = text.strip()
    if raw.startswith('```') and raw.endswith('```'):
        raw = '\n'.join(raw.splitlines()[1:-1])
    try:
        plan = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise CollaborationError('尚未形成可执行的分工，继续讨论后再开发。') from error
    if not isinstance(plan, dict) or not isinstance(plan.get('action'), str) or plan['action'] not in {'discuss', 'develop'}:
        raise CollaborationError('尚未形成可执行的分工，继续讨论后再开发。')
    summary = plan.get('summary')
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 8000:
        raise CollaborationError('分工缺少明确的方案说明，请继续讨论。')
    result = {'action': plan['action'], 'summary': summary.strip(), 'tasks': []}
    if plan['action'] == 'discuss':
        return result
    tasks = plan.get('tasks')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12:
        raise CollaborationError('开发分工需要 1 至 12 个明确任务。')
    for task in tasks:
        if not isinstance(task, dict):
            raise CollaborationError('开发分工格式无效。')
        key, member, goal = task.get('id'), task.get('member'), task.get('goal')
        files, dependencies = task.get('files'), task.get('dependsOn', [])
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', key)
                or member not in members or not isinstance(goal, str) or not goal.strip() or len(goal) > 8000
                or not isinstance(files, list) or not 1 <= len(files) <= 64
                or not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies)):
            raise CollaborationError('每项分工必须包含当前回复成员、具体文件范围和明确任务。')
        result['tasks'].append({'id': key, 'member': member, 'goal': goal.strip(),
                                'files': list(dict.fromkeys(file_path(value) for value in files)),
                                'dependsOn': list(dict.fromkeys(dependencies))})
    by_id = {task['id']: task for task in result['tasks']}
    if len(by_id) != len(tasks):
        raise CollaborationError('开发任务编号不能重复。')
    for task in result['tasks']:
        if any(dep not in by_id or dep == task['id'] for dep in task['dependsOn']):
            raise CollaborationError('分工引用了不存在或自身的前置任务。')
    remaining, done, ordered = list(result['tasks']), set(), []
    while remaining:
        ready = next((task for task in remaining if set(task['dependsOn']) <= done), None)
        if ready is None:
            raise CollaborationError('分工存在循环依赖，请调整任务顺序。')
        done.add(ready['id'])
        ordered.append(ready)
        remaining.remove(ready)
    result['tasks'] = ordered
    return result


def next_batch(tasks: list[dict], completed: set[str]) -> list[dict]:
    """Topological order; overlapping files and a member's own tasks serialize."""
    pending = [task for task in tasks if task['id'] not in completed]
    ready = [task for task in pending if set(task['dependsOn']) <= completed]
    selected, occupied, members = [], set(), set()
    for task in ready:
        files = set(task['files'])
        # Also reserve earlier pending tasks' files. A later overlapping task
        # cannot overtake its predecessor while that predecessor awaits a dep.
        earlier = pending[:pending.index(task)]
        if (task['member'] in members or files & occupied
                or any(files & set(previous['files']) for previous in earlier)):
            continue
        selected.append(task)
        occupied.update(files)
        members.add(task['member'])
    return selected
