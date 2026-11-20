"""Official local Harness runtime projected onto Dev Wisp's thread/event API."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import hashlib
import json
import os
import platform
import re
import threading
import time
import uuid
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .harness_interactions import HarnessQuestionBroker
from .harness_runtime import HarnessRuntime
from .harness_setup import HarnessSetup
from .harness_observability import HarnessObservability
from .local_usage import normalize_usage, track
from .native_engine_config import NativeEngineConfigError, NativeEngineConfigStore

from .claude_code import ClaudeCodeError, ClaudeCodeServer, ClaudeThreadBusyError, _now

PROTOCOL_APIS = {'responses': 'openai-responses', 'anthropic': 'anthropic-messages', 'chat': 'openai-completions'}


HARNESS_PERMISSION_MODES = {'read-only', 'workspace-write', 'full-access-ask', 'danger-full-access', 'plan'}
HARNESS_AGENT_PRESETS = ('standard', 'ptc', 'minimal', 'cordis')
DEFAULT_REASONING_EFFORTS = ('default', 'high', 'max')
MERCURY_REASONING_EFFORTS = ('default', 'instant', 'low', 'medium', 'high')


def reasoning_efforts(model: str, protocol: str | None) -> tuple[str, ...]:
    # Mercury's OpenAI-compatible API does not implement DeepSeek's `max`.
    if protocol == 'chat' and model.rsplit('/', 1)[-1].lower() == 'mercury-3':
        return MERCURY_REASONING_EFFORTS
    return DEFAULT_REASONING_EFFORTS


def harness_failure_message(reason: Any) -> str:
    """Classify native diagnostics without returning credential-bearing payloads."""
    error = reason.get('error', {}) if isinstance(reason, dict) else {}
    if not isinstance(error, dict):
        error = {}
    message = error.get('message') if isinstance(error.get('message'), str) else ''
    code = error.get('code') if isinstance(error.get('code'), str) else None
    if 'koffi.node' in message.lower() and any(term in message.lower() for term in ('module', 'snapshot')):
        return 'DeepSeek Harness 的 Windows 原生组件加载失败，请重启服务以加载运行目录修复。'
    match = re.match(r'^(?:HTTP\s+)?([45][0-9]{2})\b', message)
    status = int(match[1]) if match else None
    if (status in {400, 422} or code == 'INVALID_REQUEST') and any(
            field in message.lower() for field in ('reasoning_effort', 'reasoning.effort', 'reasoning effort')):
        return 'DeepSeek API 不支持所选思考等级，请切换到默认或此模型支持的等级后重试。'
    if status in {401, 403} or code in {'AUTHENTICATION_ERROR', 'PERMISSION_DENIED'}:
        return 'DeepSeek API 认证失败或无访问权限，请检查此配置的 API 密钥和模型权限。'
    if status == 404:
        return 'DeepSeek API 未找到模型或接口，请检查模型名称、API 地址和接口协议。'
    if code in {'CONTEXT_LENGTH_EXCEEDED', 'CONTEXT_WINDOW_EXCEEDED', 'context_window_exceeded', 'CONTEXT_OVERFLOW'} or (status in {400, 413} and any(term in message.lower() for term in ('context length', 'context window', 'too many tokens'))):
        return 'DeepSeek 对话超出模型上下文限制，请压缩对话或新建对话后重试。'
    if status in {408, 504} or code in {'TIMEOUT', 'ETIMEDOUT', 'REQUEST_TIMEOUT'}:
        return 'DeepSeek API 响应超时，请检查网络后重试；已有对话记录已保留。'
    if code in {'ECONNREFUSED', 'ENOTFOUND', 'NETWORK_ERROR', 'CONNECTION_ERROR'}:
        return 'DeepSeek API 连接失败，请检查服务地址和网络。'
    if status == 429 or code == 'RATE_LIMITED':
        return 'DeepSeek API 请求受限，请检查额度或稍后重试。'
    if status and status >= 500:
        return 'DeepSeek API 服务暂时不可用，请稍后重试。'
    if status in {400, 422} or code == 'INVALID_REQUEST':
        return 'DeepSeek API 拒绝了请求参数，请检查此模型支持的思考等级、接口协议和消息类型。'
    return 'DeepSeek 执行失败，请检查 API 配置、模型和权限。'


# The same agent-plane rows disabled by Harness's native Web composition, plus
# SDK-only model-facing helpers. Shared services and Dev Wisp MCP stay global.
_PRESET_HOST_ROWS = (
    'tool-plugin-manager', 'tool-bash', 'tool-pwsh', 'tool-jobs', 'tool-fs', 'tool-fs-search',
    'agent-instructions', 'skill-filesystem', 'tool-skill', 'command-goal', 'plan-mode',
    'compaction-basic', 'command-compact', 'tool-subagent-control', 'tool-subagent',
    'tool-subagent-fork', 'workflow-ptc', 'tool-workflow', 'tool-result-pruner',
    'tool-todo', 'tool-goal', 'tool-web', 'tool-executor-ptc', 'tool-office',
    'tool-ask-user', 'present', 'time-context', 'tool-agent-team', 'tool-working-directory',
)

@dataclass
class _HarnessLiveInput:
    client: Any
    session_id: str
    accepting: bool = True
    pending_receipts: set[str] = field(default_factory=set)



def provider_base_url(base_url: str, protocol: str) -> str:
    """Normalize API roots for the native clients' different version suffix rules."""
    root = base_url.rstrip('/')
    if protocol == 'anthropic':
        return root[:-3] if root.endswith('/v1') else root
    return root + '/v1' if not urlsplit(root).path else root


def is_deepseek_thread_id(thread_id: str) -> bool:
    try:
        return thread_id.startswith("deepseek:") and bool(uuid.UUID(thread_id.removeprefix("deepseek:")))
    except (ValueError, AttributeError):
        return False


class DeepSeekHarnessServer(ClaudeCodeServer):
    # Reuse the durable local-thread journal, not Claude's native CLI or history.
    engine = "deepseek-harness"
    engine_label = "DeepSeek Harness"
    thread_prefix = "deepseek:"
    effort_levels = ClaudeCodeServer.effort_levels | {'instant'}

    def __init__(self, workspace: Path, database_path: Path, emit: Any) -> None:
        self.settings_path = database_path.with_suffix('.json')
        self.setup = HarnessSetup(database_path.parent)
        self._settings_lock = threading.RLock()
        self.questions = HarnessQuestionBroker(emit, self._record_question_answer)
        self._live_inputs: dict[str, _HarnessLiveInput] = {}
        self._runtimes: dict[str, HarnessRuntime] = {}
        self._terminal_contexts: dict[str, str] = {}
        self._permission_modes: dict[str, str] = {}
        self._ssh_mcp_settings: Any = None
        self.home = Path(os.environ.get("DSH_HOME") or Path.home() / '.dsh').expanduser().resolve()
        super().__init__("dsh", workspace, database_path, emit)
        self._agent_selection_lock = asyncio.Lock()
        columns = {r['name'] for r in self._connection.execute('PRAGMA table_info(claude_threads)')}
        with self._connection:
            if 'agent_preset' not in columns:
                self._connection.execute('ALTER TABLE claude_threads ADD COLUMN agent_preset TEXT')
                # An old SDK session must retain its original global composition.
                self._connection.execute("UPDATE claude_threads SET agent_preset = 'legacy' WHERE started = 1 OR messages_json != '[]'")

    def _library(self) -> tuple[bytes | None, dict]:
        raw = NativeEngineConfigStore._read_bytes(self.settings_path)
        try:
            value = json.loads(raw) if raw is not None else {}
            if not isinstance(value, dict):
                raise ValueError()
            if value.get('version') == 1:
                if not isinstance(value.get('profiles'), list) or not isinstance(value.get('active'), dict):
                    raise ValueError()
                ids = set()
                for profile in value['profiles']:
                    if not isinstance(profile, dict) or any(not isinstance(profile.get(k), str) for k in ('id', 'name', 'model', 'baseUrl')):
                        raise ValueError()
                    if profile['id'] in ids or not isinstance(profile.get('apiKey', ''), str) or profile.get('protocol', 'anthropic') not in PROTOCOL_APIS:
                        raise ValueError()
                    ids.add(profile['id'])
                if value.get('activeProfileId') not in ids:
                    raise ValueError()
                return raw, value
            # Import lazily: reading settings must not overwrite an existing install.
            profile = {'id': 'local', 'name': '本机当前配置',
                       'model': 'deepseek-v4-flash',
                       'baseUrl': os.environ.get('DEEPSEEK_BASE_URL') or 'https://api.deepseek.com', **value}
            return raw, {'version': 1, 'activeProfileId': 'local', 'active': value, 'profiles': [profile]}
        except (ValueError, TypeError, UnicodeError):
            raise NativeEngineConfigError('已保存的 DeepSeek 配置格式无效，请在本机修复后重试。') from None

    def _settings(self) -> dict[str, Any]:
        with self._settings_lock:
            return self._library()[1]['active'].copy()

    def _write_library(self, raw: bytes | None, library: dict) -> None:
        NativeEngineConfigStore.atomic_write(self.settings_path, (json.dumps(library, ensure_ascii=False, indent=2) + '\n').encode(),
                                            NativeEngineConfigStore._revision(self.settings_path, raw))

    def profiles(self) -> dict:
        with self._settings_lock:
            raw, library = self._library()
            return {**self.status(), 'profilesRevision': NativeEngineConfigStore._revision(self.settings_path, raw),
                    'activeProfileId': library['activeProfileId'],
                    'activeProfileModified': any(p['id'] == library['activeProfileId'] and any(
                        p.get(k) != library['active'].get(k) for k in ('model', 'baseUrl', 'protocol', 'apiKey')
                    ) for p in library['profiles']),
                    'profiles': [{**{k: p[k] for k in ('id', 'name', 'model', 'baseUrl')},
                                  'protocol': p.get('protocol') or 'anthropic',
                                  'hasKey': bool(p.get('apiKey'))} for p in library['profiles']]}

    def _check_revision(self, raw: bytes | None, expected: str) -> None:
        if expected != NativeEngineConfigStore._revision(self.settings_path, raw):
            raise NativeEngineConfigError('配置已在其他页面或本机被修改，请重新读取后再保存。', conflict=True)

    def _profile_draft(self, library: dict, draft: dict) -> dict:
        existing = next((p for p in library['profiles'] if p['id'] == draft.get('profileId')), None)
        if draft.get('profileId') and existing is None:
            raise NativeEngineConfigError('该配置已被删除，请重新读取。', conflict=True)
        name = draft.get('name', '').strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise NativeEngineConfigError('请填写有效的配置名称。')
        try:
            settings = self._validated_settings(draft['model'], draft['baseUrl'], draft.get('apiKey'),
                                                draft.get('clearKey', False), draft['protocol'], existing or {})
        except ClaudeCodeError as error:
            raise NativeEngineConfigError(str(error)) from None
        return {**settings, 'id': draft.get('profileId') or uuid.uuid4().hex, 'name': name}

    def save_profile(self, draft: dict) -> dict:
        with self._settings_lock:
            raw, library = self._library()
            self._check_revision(raw, draft['expectedRevision'])
            profile = self._profile_draft(library, draft)
            if any(p['id'] != profile['id'] and p['name'].casefold() == profile['name'].casefold() for p in library['profiles']):
                raise NativeEngineConfigError('已有同名配置，请换一个配置名称。')
            library['profiles'] = [p for p in library['profiles'] if p['id'] != profile['id']] + [profile]
            if draft.get('apply', True):
                library['activeProfileId'] = profile['id']
                library['active'] = {k: v for k, v in profile.items() if k not in {'id', 'name'}}
            # A saved edit remains independent from the active snapshot until applied.
            self._write_library(raw, library)
            return {'profileId': profile['id'], 'applied': draft.get('apply', True), 'config': self.profiles()}

    def delete_profile(self, profile_id: str, expected: str) -> dict:
        with self._settings_lock:
            raw, library = self._library()
            self._check_revision(raw, expected)
            if library['activeProfileId'] == profile_id:
                raise NativeEngineConfigError('请先应用另一套配置，再删除当前正在使用的配置。')
            if not any(p['id'] == profile_id for p in library['profiles']):
                raise NativeEngineConfigError('该配置已被删除，请重新读取。', conflict=True)
            library['profiles'] = [p for p in library['profiles'] if p['id'] != profile_id]
            self._write_library(raw, library)
            return self.profiles()

    def connection_draft(self, draft: dict) -> dict:
        with self._settings_lock:
            raw, library = self._library()
            self._check_revision(raw, draft['expectedRevision'])
            profile = self._profile_draft(library, draft)
            profile['apiKey'] = profile.get('apiKey') or os.environ.get('DEEPSEEK_API_KEY') or ''
            if not profile['apiKey']:
                raise NativeEngineConfigError('请填写此配置的 API 密钥。')
            return {**profile, 'models': {}, 'authMode': 'api_key' if profile['protocol'] == 'anthropic' else 'bearer'}

    def status(self) -> dict[str, Any]:
        settings = self._settings()
        available = self.setup.status()['installed']
        error = None if available else "未安装 DeepSeek Harness，请安装 server/requirements.txt 中的依赖。"
        baseline = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'devwisp/harness/flock-0.1.2-baseline.node'
        if available and platform.system() == 'Linux' and platform.machine() == 'x86_64':
            try:
                avx = ' avx ' in ' ' + Path('/proc/cpuinfo').read_text().replace('\n', ' ') + ' '
            except OSError:
                avx = True
            if not avx and not baseline.is_file():
                available = False
                error = '此主机缺少 AVX，请先运行 .venv/bin/python scripts/repair-harness-flock.py 安装文件锁兼容组件。'
        return {"available": available,
                "error": error,
                "hasApiKey": bool(settings.get("apiKey") or os.environ.get("DEEPSEEK_API_KEY")),
                "model": settings.get("model") or "deepseek-v4-flash",
                "baseUrl": settings.get("baseUrl") or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
                "protocol": settings.get("protocol") or "anthropic",
                "home": str(self.home), "profile": "sdk",
                "reasoningEffortsByModel": {model: list(reasoning_efforts(model, settings.get('protocol')))
                    for model in (settings.get('model') or 'deepseek-v4-flash', 'deepseek-v4-flash', 'deepseek-v4-pro')},
                "agentPresets": list(HARNESS_AGENT_PRESETS) if available else []}

    @staticmethod
    def _validated_settings(model: str, base_url: str, api_key: str | None, clear_key: bool,
                            protocol: str | None, existing: dict) -> dict:
        base_url = base_url.strip().rstrip('/')
        try:
            parsed = urlsplit(base_url)
            valid_url = parsed.scheme in {'http', 'https'} and parsed.hostname and not (
                parsed.username or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in base_url))
            parsed.port  # Validate a supplied port as well as the hostname.
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ClaudeCodeError("DeepSeek API 地址必须是有效的 HTTP(S) 地址，不能包含认证信息或查询参数。")
        if protocol is not None and protocol not in PROTOCOL_APIS:
            raise ClaudeCodeError('DeepSeek 接口协议无效。')
        if parsed.path.endswith(('/responses', '/chat/completions', '/messages')):
            raise ClaudeCodeError('请填写 API 根地址（可带 /v1），不要填写 /responses、/messages 或 /chat/completions。')
        if not model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
            raise ClaudeCodeError("请填写有效的 DeepSeek 模型名称。")
        if api_key and any(ord(c) < 32 for c in api_key.strip()):
            raise ClaudeCodeError('API 密钥不能包含换行。')
        settings = existing.copy()
        settings.update(model=model.strip(), baseUrl=base_url)
        if protocol is not None:
            settings['protocol'] = protocol
        if clear_key:
            settings.pop("apiKey", None)
        elif api_key and api_key.strip():
            settings["apiKey"] = api_key.strip()
        return settings

    def save_settings(self, model: str, base_url: str, api_key: str | None, clear_key: bool = False,
                      protocol: str | None = None) -> dict[str, Any]:
        with self._settings_lock:
            raw, library = self._library()
            settings = self._validated_settings(model, base_url, api_key, clear_key, protocol, library['active'])
            library['active'] = settings
            for p in library['profiles']:
                if p['id'] == library['activeProfileId']:
                    name = p['name']
                    p.clear()
                    p.update(settings, id=library['activeProfileId'], name=name)
                    break
            self._write_library(raw, library)
            return self.status()

    def schedule_native_refresh(self) -> None:
        pass

    def _queue_snapshot_extra_fields(self) -> str:
        return "agent_preset, "

    @classmethod
    def _summary(cls, row: Any) -> dict[str, Any]:
        return {**super()._summary(row), 'deepseekAgentPreset': row['agent_preset'] or 'standard',
                'deepseekAgentPresetLocked': bool(row['agent_preset'])}

    def list_threads(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            'SELECT thread_id, name, preview, cwd, created_at, updated_at, running_turn_id, '
            'native_source, native_mtime_ns, agent_preset FROM claude_threads ORDER BY updated_at DESC').fetchall()
        return [self._summary(row) for row in rows]

    def _row(self, thread_id: str, *, include_messages: bool = True) -> Any:
        from .claude_code import ClaudeThreadNotFoundError
        if not self._is_thread(thread_id):
            raise ClaudeThreadNotFoundError('DeepSeek 对话编号无效。')
        fields = '*' if include_messages else (
            'thread_id, name, preview, cwd, created_at, updated_at, running_turn_id, '
            'native_source, native_mtime_ns, agent_preset')
        row = self._connection.execute(f'SELECT {fields} FROM claude_threads WHERE thread_id = ?', (thread_id,)).fetchone()
        if row is None:
            raise ClaudeThreadNotFoundError('找不到这条 DeepSeek 对话。')
        return row

    def enable_ssh_mcp(self, store: Any) -> None:
        self._ssh_mcp_settings = store

    @staticmethod
    def _content_blocks(prompt: str, image_paths: list[Path]) -> list[dict]:
        blocks: list[dict] = [{'type': 'text', 'text': prompt}]
        for path in image_paths:
            mime = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}.get(path.suffix.lower(), 'image/png')
            blocks.append({'type': 'image', 'data': base64.b64encode(path.read_bytes()).decode(), 'mimeType': mime})
        return blocks

    async def steer_turn(self, thread_id: str, prompt: str, **kwargs: Any) -> str:
        self._validate_agent_preset(thread_id, kwargs.get('deepseek_agent_preset'))
        if not prompt.strip() and not kwargs.get('image_paths') and not kwargs.get('attachment_paths'):
            raise ClaudeCodeError('请填写消息或附加文件。')
        turn_input = self._turn_inputs.get(thread_id)
        if turn_input:
            await turn_input.ready.wait()
            async def admit() -> str | None:
                async with turn_input.lock:
                    live = self._live_inputs.get(thread_id)
                    if live and live.accepting and self._row(thread_id)['running_turn_id'] == turn_input.turn_id:
                        request_id = kwargs.get('request_id')
                        if request_id and request_id in turn_input.request_ids:
                            return turn_input.turn_id
                        try:
                            receipt = await asyncio.to_thread(live.client.session_prompt, live.session_id,
                                self._content_blocks(prompt, kwargs.get('image_paths') or []))
                        except Exception:
                            raise ClaudeCodeError('DeepSeek 实时输入送达状态未确认，请先核对对话记录后重试。') from None
                        live.pending_receipts.add(receipt)
                        if request_id:
                            turn_input.request_ids.add(request_id)
                        temporary = kwargs.get('temporary_directory')
                        if temporary:
                            turn_input.temporary_directories.append(temporary)
                        row = self._row(thread_id)
                        messages = json.loads(row['messages_json'])
                        current = next((m for m in reversed(messages) if m.get('role') == 'user' and m.get('turnId') == turn_input.turn_id), {})
                        user_id, created = f'user-{request_id or uuid.uuid4()}', _now()
                        display = kwargs.get('display_prompt') if kwargs.get('display_prompt') is not None else prompt
                        messages.append({'id': user_id, 'role': 'user', 'text': display, 'createdAt': created,
                            'turnId': turn_input.turn_id, 'model': current.get('model', 'default'), 'effort': current.get('effort', 'default')})
                        with self._connection:
                            self._connection.execute('UPDATE claude_threads SET messages_json = ?, preview = ?, updated_at = ? WHERE thread_id = ?',
                                (json.dumps(messages, ensure_ascii=False), ' '.join(display.split())[:400], created, thread_id))
                        await self.emit({'type': 'notification', 'method': 'item/started', 'params': {
                            'threadId': thread_id, 'turnId': turn_input.turn_id, 'item': {'id': user_id, 'type': 'userMessage',
                            'status': 'completed', 'createdAt': created, 'content': [{'type': 'text', 'text': display}]}}})
                        return turn_input.turn_id
            admission = asyncio.create_task(admit())
            try:
                admitted = await asyncio.shield(admission)
            except asyncio.CancelledError:
                # An HTTP disconnect cannot roll back a prompt already admitted by
                # the native inbox. Finish its journal entry before releasing it.
                with suppress(Exception):
                    await asyncio.shield(admission)
                raise
            if admitted:
                return admitted
        task = self._tasks.get(thread_id)
        if task:
            await asyncio.gather(task, return_exceptions=True)
        return await self.start_turn(thread_id, prompt, **kwargs)

    async def start_turn(self, thread_id: str, prompt: str, **kwargs: Any) -> str:
        selected = kwargs.pop('deepseek_agent_preset', None)
        mode = kwargs.pop('deepseek_permission_mode', None) or ('danger-full-access' if kwargs.get('permission_mode') == 'bypassPermissions' else 'workspace-write')
        if mode not in HARNESS_PERMISSION_MODES:
            raise ClaudeCodeError('DeepSeek 权限模式无效。')
        settings = self._settings()
        selected_model = kwargs.get('model')
        if not selected_model or selected_model == 'default':
            selected_model = settings.get('model') or 'deepseek-v4-flash'
        supported_efforts = reasoning_efforts(selected_model, settings.get('protocol'))
        if supported_efforts == MERCURY_REASONING_EFFORTS and kwargs.get('effort') in {'max', 'xhigh'}:
            # Saved preferences and queued/resend requests may predate capability discovery.
            kwargs['effort'] = 'high'
        if kwargs.get('effort') == 'default':
            kwargs['effort'] = None
        if supported_efforts == MERCURY_REASONING_EFFORTS and kwargs.get('effort') and kwargs['effort'] not in supported_efforts:
            raise ClaudeCodeError('DeepSeek API 不支持所选思考等级，请切换到默认或此模型支持的等级后重试。')
        async with self._agent_selection_lock:
            previous_task = self._tasks.get(thread_id)
            if previous_task and previous_task is not asyncio.current_task() and not self._row(thread_id)['running_turn_id']:
                # Completion is emitted before its final cleanup. A queued turn
                # must not have its callbacks or terminal context cleared by it.
                await asyncio.shield(previous_task)
            preset = self._validate_agent_preset(thread_id, selected)
            if preset == 'minimal' and mode == 'plan':
                raise ClaudeCodeError('Minimal Agent 不提供计划模式，请选择其他授权模式或新建其他 Agent 对话。')
            previous = self._row(thread_id)['agent_preset']
            with self._connection:
                self._connection.execute('UPDATE claude_threads SET agent_preset = ? WHERE thread_id = ?', (preset, thread_id))
            try:
                result = await super().start_turn(thread_id, prompt, **kwargs)
            except BaseException:
                with self._connection:
                    self._connection.execute('UPDATE claude_threads SET agent_preset = ? WHERE thread_id = ?', (previous, thread_id))
                raise
            self._permission_modes[thread_id] = mode
        await self._notify_threads_changed()
        return result

    def _validate_agent_preset(self, thread_id: str, requested: str | None) -> str:
        current = self._row(thread_id)['agent_preset']
        if requested is not None and requested not in (*HARNESS_AGENT_PRESETS, 'legacy'):
            raise ClaudeCodeError('DeepSeek Agent 模式无效。')
        if current and requested and current != requested:
            raise ClaudeCodeError('此对话的 Agent 模式已锁定，请新建对话后切换。')
        if requested == 'legacy' and current != 'legacy':
            raise ClaudeCodeError('旧版 SDK 模式仅供已有对话继续使用。')
        return current or requested or 'standard'

    async def _run_turn(self, thread_id: str, turn_id: str, prompt: str, image_paths: list[Path],
                        attachment_paths: list[Path], temporary_directory: Path | None,
                        model: str | None, effort: str | None, permission_mode: str | None) -> None:
        try:
            await self._execute_turn(thread_id, turn_id, prompt, image_paths, attachment_paths,
                                     temporary_directory, model, effort, permission_mode)
        except (Exception, asyncio.CancelledError) as failure:
            stopped = isinstance(failure, asyncio.CancelledError)
            await self._release_runtime(thread_id)
            error = None if stopped else 'DeepSeek Harness 启动失败，请检查安装、配置和工作目录。'
            await self._finish_turn(thread_id, turn_id, f'assistant-{turn_id}', error or '', _now(),
                                    'stopped' if stopped else 'failed', model, effort, error)
        finally:
            lease = self._runtimes.get(thread_id)
            if lease and not lease.healthy():
                self._runtimes.pop(thread_id, None)
                await asyncio.to_thread(lease.close)
            turn_input = self._turn_inputs.get(thread_id)
            if turn_input:
                turn_input.ready.set()
                async with turn_input.lock:
                    self._turn_inputs.pop(thread_id, None)
                    import shutil
                    for directory in turn_input.temporary_directories:
                        if directory != temporary_directory:
                            shutil.rmtree(directory, ignore_errors=True)
            if thread_id not in self._runtimes:
                context = self._terminal_contexts.pop(thread_id, None)
                if context and self._terminal_handoff:
                    self._terminal_handoff.unbind_context(context)
            self._live_inputs.pop(thread_id, None)
            self._permission_modes.pop(thread_id, None)
            self._live_replies.pop(thread_id, None)
            self._live_terminal_items.pop(thread_id, None)
            self._interrupts.discard(thread_id)
            (temporary_directory or self.database_path.parent).joinpath(f'harness-stream-{turn_id}.json').unlink(missing_ok=True)
            if temporary_directory:
                import shutil
                shutil.rmtree(temporary_directory, ignore_errors=True)
            self._tasks.pop(thread_id, None)

    async def _execute_turn(self, thread_id: str, turn_id: str, prompt: str, image_paths: list[Path],
                        attachment_paths: list[Path], temporary_directory: Path | None,
                        model: str | None, effort: str | None, permission_mode: str | None) -> None:
        from deepseek_harness import DeepSeekHarness

        loop = asyncio.get_running_loop()
        row = self._row(thread_id)
        session_id = row['session_id']
        journal = json.loads(row['messages_json'])
        # A failed first startup can mark the web journal as started before
        # Harness has persisted a native session. Only recover empty/failed
        # histories; never silently replace a conversation with useful replies.
        can_initialize_missing_session = not any(
            message.get('role') == 'assistant' and message.get('status') not in {'failed', 'stopped'}
            for message in journal if isinstance(message, dict)
        )
        settings = self._settings()
        selected_model = model if model and model != 'default' else settings.get('model') or 'deepseek-v4-flash'
        protocol = settings.get('protocol')
        base_url = settings.get('baseUrl') or os.environ.get('DEEPSEEK_BASE_URL') or 'https://api.deepseek.com'
        # Keep old installations and native DeepSeek credentials on their existing route.
        third_party = protocol is not None and not (
            protocol == 'anthropic' and base_url.rstrip('/') in {'https://api.deepseek.com', 'https://api.deepseek.com/v1'})
        stopped = threading.Event()
        pending: set[Any] = set()
        pending_lock = threading.Lock()
        close_lock = threading.Lock()
        # Keep the standard full profile and append a tiny live-stream bridge.
        # YAML accepts JSON; no user home/plugin configuration is overwritten.
        patch_directory = temporary_directory or self.database_path.parent
        patch_path = patch_directory / f"harness-stream-{turn_id}.json"
        baseline = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'devwisp/harness/flock-0.1.2-baseline.node'
        patches = [{"insert": [{"id": "devwisp-assistant-stream",
            "name": str(Path(__file__).with_name("harness_stream.cjs")),
            "config": {"flockAddonPath": str(baseline)} if platform.system() == "Linux" and platform.machine() == "x86_64" and baseline.is_file() else {}}]}]
        patches.append({'id': 'permission', 'config': {'presets': {
            'read-only': {'sandbox': 'read-only', 'approval': 'ask'},
            'workspace-write': {'sandbox': 'workspace-write', 'approval': 'ask'},
            'full-access-ask': {'sandbox': 'danger-full-access', 'approval': 'ask'},
            'danger-full-access': {'sandbox': 'danger-full-access', 'approval': 'never'},
        }, 'defaultPreset': 'workspace-write'}})
        if self._permission_modes.get(thread_id) == 'plan':
            patches.append({'insert': [{'id': 'devwisp-plan-guard',
                'name': str(Path(__file__).with_name('harness_plan_guard.cjs')),
                'config': {'sessionId': session_id}}]})
        context = self._terminal_contexts.get(thread_id) or uuid.uuid4().hex
        mcp_servers = {}
        if self._terminal_handoff:
            self._terminal_handoff.bind_context(context, thread_id)
            self._terminal_contexts[thread_id] = context
            mcp_servers['devwisp_terminal'] = {'command': sys.executable, 'args': ['-m', 'devwisp.terminal_handoff'],
                'env': {'PYTHONPATH': str(Path(__file__).resolve().parent.parent),
                        **self._terminal_handoff.mcp_environment(), 'DEVWISP_TERMINAL_CONTEXT': context}}
        if self._ssh_mcp_settings:
            ssh = self._ssh_mcp_settings.codex_server_config()
            if ssh:
                ssh['env'].update({'DEVWISP_PERMISSION_THREAD': thread_id,
                    'DEVWISP_PERMISSION_SOCKET': str(self._permission_broker.socket_path) if self._permission_broker else ''})
                ssh['args'] = ['-m', 'devwisp.harness_mcp_proxy', '--', ssh['command'], *ssh.get('args', [])]
                ssh['command'] = sys.executable
                mcp_servers['devwisp_ssh_mcp'] = ssh
        if mcp_servers:
            patches.append({'insert': [{'id': f'devwisp-mcp-{name}', 'name': '@deepseek-ai/dsh-mcp-client',
                'config': {'transport': 'stdio', 'serverName': name, **config, 'cwd': row['cwd'],
                           'startupTimeoutMs': 20000, 'toolCallTimeoutMs': 610000, 'failOnStartupError': True}}
                for name, config in mcp_servers.items()]})
        if third_party:
            patches.append({'id': 'llm-pi-ai', 'config': {'providers': {'devwisp-deepseek': {
                'displayName': 'DeepSeek API', 'apiKeyEnv': 'DEVWISP_HARNESS_API_KEY',
                'api': PROTOCOL_APIS[protocol], 'baseURL': provider_base_url(base_url, protocol),
                'models': [{'id': selected_model, 'input': ['text', 'image'],
                            'reasoningEfforts': {('minimal' if level == 'instant' else level): level
                                                for level in reasoning_efforts(selected_model, protocol)
                                                if level != 'default'}}],
            }}}})
        agent_preset = row['agent_preset'] or 'standard'
        preset_paths: tuple[str, ...] = ()
        if agent_preset != 'legacy':
            patches.extend({'id': name, 'disabled': True} for name in _PRESET_HOST_ROWS)
            patches.append({'insert': [
                {'id': 'agent-preset-registry', 'name': '@deepseek-ai/dsh-agent-preset-registry',
                 'config': {'default': agent_preset}},
                {'id': 'devwisp-agent-presets', 'name': str(Path(__file__).with_name('harness_agent_presets.cjs')),
                 'config': {'preset': agent_preset}},
            ]})
            if agent_preset == 'cordis':
                patches.append({'insert': [{'id': 'cordis-host-runner', 'name': '@deepseek-ai/dsh-cordis-host-runner'},
                    {'id': 'cordis-inspect-providers',
                    'name': '@deepseek-ai/dsh-tool-cordis/host'}]})
            # A separate patch keeps native !!js expressions intact. Only the
            # selected definition mounts, without starting Harness's Web server.
            preset_paths = (str(Path(__file__).with_name('harness_presets') / f'{agent_preset}.yml'),)
        patch_path.write_text(json.dumps(patches))
        native_effort = 'minimal' if effort == 'instant' else effort or None
        if native_effort is None and reasoning_efforts(selected_model, protocol) == MERCURY_REASONING_EFFORTS:
            # An omitted override resumes the old native session's effort.
            # Explicitly restore this adapter's default when leaving instant/low.
            native_effort = 'high'
        # Fingerprint only; credentials and server settings never reach events.
        signature = hashlib.sha256(json.dumps({
            'executable': self.setup.executable(),
            'settings': settings, 'model': selected_model, 'effort': native_effort,
            'mode': self._permission_modes.get(thread_id), 'agent': agent_preset,
            'cwd': row['cwd'], 'home': str(self.home), 'mcp': mcp_servers,
            'sshProfiles': getattr(self._ssh_mcp_settings, 'list_profiles', lambda: [])(),
            'sshCredentialRevision': self._ssh_mcp_settings.credentials_path.stat().st_mtime_ns
                if self._ssh_mcp_settings and getattr(self._ssh_mcp_settings, 'credentials_path', Path('/nonexistent')).is_file() else None,
            'ambientKey': hashlib.sha256(os.environ.get('DEEPSEEK_API_KEY', '').encode()).hexdigest(),
        }, sort_keys=True).encode()).hexdigest()
        lease = self._runtimes.get(thread_id)
        if lease and (lease.signature != signature or not lease.healthy()):
            await asyncio.to_thread(lease.close)
            self._runtimes.pop(thread_id, None)
            lease = None
        reused = lease is not None
        if lease is None:
            lease = HarnessRuntime(signature)
            lease.runtime = DeepSeekHarness(
                dsh_bin=self.setup.executable(),
                dsh_home=str(self.home), profile="sdk", cwd=row['cwd'], patches=(str(patch_path), *preset_paths),
                # Packaged Windows native addons resolve their snapshot path
                # from the launch drive. Keep that separate from the agent's
                # actual workspace (cwd), which may be on a different drive.
                runtime_cwd=str(self.database_path.parent) if os.name == 'nt' else None,
                provider='devwisp-deepseek' if third_party else 'deepseek-official',
                model=selected_model, reasoning_effort=native_effort,
                api_key=None if third_party else settings.get('apiKey') or None,
                base_url=None if third_party else settings.get('baseUrl') or None,
                env={'DEVWISP_HARNESS_API_KEY': settings.get('apiKey') or os.environ.get('DEEPSEEK_API_KEY') or ''} if third_party else {},
                approval_requests=True, on_user_question=lease.question,
            )
            self._runtimes[thread_id] = lease
        runtime = lease.runtime
        telemetry = HarnessObservability(session_id, lease.child_parents)
        if lease.context_window is not None:
            telemetry.context['contextWindow'] = lease.context_window
        telemetry.continuity = 'reused' if reused else 'restored' if row['started'] else 'new'
        item_id = f'assistant-{turn_id}'
        tools: dict[str, dict[str, Any]] = {}
        text = ''
        attempt_text = ''
        seen_output = False
        segment_index = 0
        question_boundaries: asyncio.Queue[str] = asyncio.Queue()
        error = None
        status = 'completed'

        def submit(coroutine: Any) -> Any:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            with pending_lock:
                pending.add(future)
            try:
                if stopped.is_set():
                    future.cancel()
                    return None
                return future.result()
            finally:
                with pending_lock:
                    pending.discard(future)

        last_projection = 0.0
        saved_usage: dict[str, tuple[str | None, dict]] = {}

        async def project(phase: str | None = None, *, durable: bool = False, throttle: bool = False) -> None:
            nonlocal last_projection
            if phase:
                telemetry.phase = phase
            item = {'id': f'telemetry-{turn_id}', 'type': 'harnessTelemetry', **telemetry.snapshot()}
            self._live_terminal_items.setdefault(thread_id, {})[item['id']] = item
            if durable:
                self._save_tool(thread_id, turn_id, item)
            if throttle and time.monotonic() - last_projection < .08:
                return
            last_projection = time.monotonic()
            await self.emit({'type': 'notification', 'method': 'deepseek/turn/telemetry',
                'params': {'threadId': thread_id, 'turnId': turn_id, 'telemetry': telemetry.snapshot()}})

        await project()

        async def seal_assistant() -> str:
            nonlocal text, item_id, segment_index
            if not text:
                messages = json.loads(self._row(thread_id)['messages_json'])
                return next((message['id'] for message in reversed(messages)
                             if message.get('turnId') == turn_id and message.get('id')), '')
            previous_id = item_id
            self._save_assistant_segment(thread_id, turn_id, previous_id, text, model, effort)
            segment_index += 1
            item_id = f'assistant-{turn_id}-segment-{segment_index}'
            self._live_replies[thread_id] = {'id': item_id, 'turnId': turn_id, 'text': '', 'createdAt': _now()}
            previous_text = text
            text = ''
            await self.emit({'type': 'notification', 'method': 'item/completed', 'params': {
                'threadId': thread_id, 'turnId': turn_id, 'item': {'id': previous_id, 'type': 'agentMessage',
                    'text': previous_text, 'status': 'completed', 'model': model or 'default', 'effort': effort or 'default'}}})
            return previous_id

        async def notify(notification: Any) -> None:
            nonlocal text, attempt_text, error, seen_output
            payload = notification.payload
            if telemetry.observe(notification.method, payload):
                await project(throttle=notification.method == 'session.stream')
            # Model-only completion and a missing-usage attempt need not alter
            # the browser snapshot. Persist accounting independently of it.
            frame = payload.get('frame', {})
            event = payload.get('event', {})
            if (notification.method == 'session.stream' and (frame.get('type') == 'start' or frame.get('chunk', {}).get('type') == 'usage')
                    or notification.method == 'session.event' and event.get('type') == 'assistant/message'):
                for sample_id, usage_model, raw_usage in telemetry.usage_records():
                    current_usage = (usage_model, raw_usage)
                    if saved_usage.get(sample_id) != current_usage:
                        await track(self._usage_store, 'record', self.engine, thread_id, turn_id,
                                    sample_id, usage_model or model, normalize_usage(raw_usage, fresh_input=True))
                        saved_usage[sample_id] = (usage_model, raw_usage.copy())
            if payload.get('sessionId') != session_id:
                return  # Descendants are displayed separately from the root answer.
            if notification.method == 'session.stream':
                frame = payload.get('frame', {})
                if frame.get('type') == 'start':
                    attempt_text = ''
                chunk = frame.get('chunk', {})
                if frame.get('type') == 'chunk' and chunk.get('type') == 'text-delta':
                    delta = chunk.get('text', '')
                    if isinstance(delta, str):
                        seen_output = seen_output or bool(delta)
                        if not attempt_text and text:
                            delta = '\n\n' + delta
                        attempt_text += delta
                        text += delta
                        await self._emit_delta(thread_id, turn_id, item_id, delta)
                return
            event = payload.get('event', {})
            if not isinstance(event, dict):
                return
            kind, data = event.get('type'), event.get('data', {})
            if not isinstance(data, dict):
                return
            if kind == 'assistant/message':
                message = data.get('message', data)
                content = message.get('content', []) if isinstance(message, dict) else []
                committed = ''.join(str(b.get('text', '')) for b in content if isinstance(b, dict) and b.get('type') == 'text')
                if committed and not attempt_text:
                    seen_output = True
                    delta = ('\n\n' if text else '') + committed
                    text += delta
                    await self._emit_delta(thread_id, turn_id, item_id, delta)
                attempt_text = ''
            elif kind in {'assistant/delta', 'assistant/stream'}:
                chunk = data.get('chunk', data)
                if isinstance(chunk, dict) and chunk.get('type') == 'text-delta':
                    delta = chunk.get('text', '')
                    if isinstance(delta, str):
                        seen_output = seen_output or bool(delta)
                        text += delta
                        await self._emit_delta(thread_id, turn_id, item_id, delta)
            elif kind == 'turn/end' and isinstance(data.get('reason'), dict) and data['reason'].get('kind') == 'error':
                error = harness_failure_message(data['reason'])
            elif kind in {'tool/call', 'tool/result'}:
                message = data.get('message', {}) if isinstance(data.get('message', {}), dict) else {}
                call_id = str(data.get('callId') or message.get('toolCallId') or message.get('callId') or data.get('id') or event.get('seq'))
                if kind == 'tool/call':
                    name = data.get('name') or data.get('toolName') or 'tool'
                    if str(name).rsplit('/', 1)[-1] in {'ask_user_question', 'exit_plan_mode'}:
                        question_boundaries.put_nowait(await seal_assistant())
                    tool = {'id': f'tool-{turn_id}-{call_id}', 'type': 'dynamicToolCall',
                            'server': 'DeepSeek Harness', 'tool': data.get('name') or data.get('toolName') or 'tool',
                            'name': data.get('name') or data.get('toolName') or 'tool',
                            'arguments': data.get('input', data.get('arguments', {})), 'status': 'inProgress'}
                    tools[call_id] = tool
                else:
                    tool = tools.pop(call_id, None)
                    if not tool:
                        return
                    tool = {**tool, 'status': 'failed' if message.get('isError') else 'completed',
                            'result': message.get('content', data.get('result', data.get('output')))}
                    self._save_tool(thread_id, turn_id, tool)
                if kind == 'tool/call':
                    self._live_terminal_items.setdefault(thread_id, {})[tool['id']] = tool
                else:
                    self._live_terminal_items.get(thread_id, {}).pop(tool['id'], None)
                await self.emit({'type': 'notification', 'method': 'item/started' if kind == 'tool/call' else 'item/completed',
                                 'params': {'threadId': thread_id, 'turnId': turn_id, 'item': tool}})

        async def approval(request: Any) -> bool:
            if self._permission_broker is None:
                return False
            await project('waiting-approval')
            try:
                return await self._permission_broker.request(
                    thread_id, tool_name=str(request.payload.get('toolName', 'tool')),
                    reason=str(request.payload.get('reason') or 'DeepSeek Harness 请求执行此操作。'), cwd=row['cwd'])
            finally:
                await project('thinking')

        async def ask(request: Any, cancelled: threading.Event) -> Any:
            # Native callbacks and the ordered notification reader run on
            # different threads. Wait for the tool event so preceding deltas
            # are journalled before exposing its question to clients.
            try:
                after_item_id = await asyncio.wait_for(question_boundaries.get(), timeout=2)
            except TimeoutError:
                after_item_id = await seal_assistant()
            await project('waiting-answer')
            try:
                return await self.questions.ask(thread_id, turn_id, request.questions, cancelled, after_item_id=after_item_id)
            finally:
                await project('thinking')

        lease.callbacks = (lambda request: submit(approval(request)),
                           lambda request, cancelled: submit(ask(request, cancelled)))

        def close() -> None:
            lease.close()

        turn_input = self._turn_inputs[thread_id]

        async def observed(notification: Any, allow_idle: bool = True) -> bool:
            await notify(notification)
            async with turn_input.lock:
                live = self._live_inputs[thread_id]
                if notification.method == 'session.event' and notification.payload.get('sessionId') == session_id:
                    event = notification.payload.get('event', {})
                    if event.get('type') == 'agent/inbox/spliced':
                        for message in event.get('data', {}).get('inserted', []):
                            live.pending_receipts.discard(message.get('id'))
                if (allow_idle and notification.method == 'session.status' and notification.payload.get('sessionId') == session_id
                        and notification.payload.get('status') == 'idle' and not live.pending_receipts
                        and not any(child['status'] == 'running' for child in telemetry.children.values())):
                    live.accepting = False
                    return True
            return False

        async def ready(receipt: str | None, accepting: bool = True) -> None:
            self._live_inputs[thread_id] = _HarnessLiveInput(runtime.client, session_id, accepting=accepting,
                pending_receipts={receipt} if receipt else set())
            turn_input.ready.set()

        def run() -> Any:
            try:
                runtime.start()
                if stopped.is_set():
                    return None
                lease.start_requests()
                if reused:
                    session = lease.session
                elif row['started']:
                    try:
                        session = runtime.resume_session(session_id)
                    except Exception as failure:
                        detail = str(failure).lower()
                        if can_initialize_missing_session and 'session' in detail and 'not found' in detail and session_id in detail:
                            session = runtime.start_session(session_id)
                        else:
                            raise
                else:
                    session = runtime.start_session(session_id)
                lease.session = session
                if stopped.is_set():
                    return None
                if not reused:
                    session.set_working_directory(row['cwd'])
                submit(self._mark_started(thread_id))
                mode = self._permission_modes.get(thread_id, 'workspace-write')
                preset = 'read-only' if mode == 'plan' else mode
                if session.run(f'/permission {preset}').finish_reason == 'error':
                    raise ClaudeCodeError('DeepSeek 权限模式初始化失败。')
                if agent_preset != 'minimal' and session.run('/plan' if mode == 'plan' else '/plan off').finish_reason == 'error':
                    raise ClaudeCodeError('DeepSeek 计划模式初始化失败。')
                finish_reason = None
                with runtime.client.subscribe_session_notifications(session_id) as subscription:
                    receipt = runtime.client._query(session_id, self._content_blocks(prompt, image_paths))
                    command_id = getattr(receipt, 'command_id', None)
                    submit(ready(receipt if isinstance(receipt, str) else None, not bool(command_id)))
                    while not stopped.is_set():
                        notification = subscription.next()
                        if notification.method == 'session.event' and notification.payload.get('sessionId') == session_id:
                            event = notification.payload.get('event', {})
                            if event.get('type') == 'turn/end':
                                finish_reason = event.get('data', {}).get('reason', {}).get('kind')
                        idle = submit(observed(notification, not bool(command_id)))
                        if command_id:
                            if (notification.method == 'session.query.completed' and notification.payload.get('sessionId') == session_id
                                    and notification.payload.get('commandId') == command_id):
                                return SimpleNamespace(final_response=notification.payload.get('finalResponse', ''),
                                                       finish_reason=notification.payload.get('finishReason'))
                        elif idle:
                            return SimpleNamespace(final_response='', finish_reason=finish_reason)
                return None
            finally:
                stopped.set()
                with pending_lock:
                    for future in pending:
                        future.cancel()
                lease.callbacks = None

        worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            result = await asyncio.shield(worker)
            if result:
                final = result.final_response
                if final and not text and not seen_output:
                    text = final
                    await self._emit_delta(thread_id, turn_id, item_id, text)
                if result.finish_reason in {'error', 'aborted'}:
                    status = 'failed' if result.finish_reason == 'error' else 'stopped'
                    error = (error or harness_failure_message(None)) if status == 'failed' else None
                elif result.finish_reason == 'max-tokens':
                    status, error = 'failed', '模型达到本次输出上限，任务可能尚未完成；已保留回复和上下文。'
                elif result.finish_reason in {'blocked', 'interrupted'}:
                    status, error = 'stopped', '任务被阻止或中断，尚未确认完成；请查看权限或运行详情。'
                elif result.finish_reason != 'completed':
                    status, error = 'failed', '未收到明确的任务完成结果，已保留回复和上下文。'
        except asyncio.CancelledError:
            status = 'stopped'
            stopped.set()
            with pending_lock:
                for future in pending:
                    future.cancel()
            await asyncio.to_thread(close)
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(worker), timeout=35)
        except Exception:
            # SDK exceptions can embed credential-bearing HTTP diagnostics.
            status, error = 'failed', 'DeepSeek Harness 运行失败，请在设置中检查 API 密钥、地址与模型；已有会话不会自动重建。'
        finally:
            for tool in tools.values():
                tool = {**tool, 'status': 'failed'}
                self._save_tool(thread_id, turn_id, tool)
                await self.emit({'type': 'notification', 'method': 'item/completed',
                                 'params': {'threadId': thread_id, 'turnId': turn_id, 'item': tool}})
            if status != 'completed':
                await asyncio.to_thread(close)
                self._runtimes.pop(thread_id, None)
            lease.child_parents = telemetry.known_children.copy()
            lease.context_window = telemetry.context.get('contextWindow')
            for child in telemetry.children.values():
                if child['status'] == 'running':
                    child['status'] = 'stopped'
            await project(status, durable=True)
            await self._finish_turn(thread_id, turn_id, item_id, text or error or '', _now(), status, model, effort, error)
            self._live_replies.pop(thread_id, None)
            self._live_terminal_items.pop(thread_id, None)
            self._interrupts.discard(thread_id)
            patch_path.unlink(missing_ok=True)
            if temporary_directory:
                import shutil
                shutil.rmtree(temporary_directory, ignore_errors=True)

    async def delete_thread(self, thread_id: str) -> None:
        await super().delete_thread(thread_id)
        await self._release_runtime(thread_id)

    async def _release_runtime(self, thread_id: str) -> None:
        lease = self._runtimes.pop(thread_id, None)
        if lease:
            await asyncio.to_thread(lease.close)
        context = self._terminal_contexts.pop(thread_id, None)
        if context and self._terminal_handoff:
            self._terminal_handoff.unbind_context(context)

    async def close(self) -> None:
        await self.setup.close()
        await super().close()
        await asyncio.gather(*(self._release_runtime(tid) for tid in tuple(self._runtimes)))

    async def _mark_started(self, thread_id: str) -> None:
        with self._connection:
            self._connection.execute('UPDATE claude_threads SET started = 1 WHERE thread_id = ?', (thread_id,))

    def _save_assistant_segment(self, thread_id: str, turn_id: str, item_id: str, text: str,
                                model: str | None, effort: str | None) -> None:
        row = self._row(thread_id)
        messages = json.loads(row['messages_json'])
        created = _now()
        if not any(message.get('id') == item_id for message in messages):
            messages.append({'id': item_id, 'role': 'assistant', 'text': text, 'turnId': turn_id,
                'createdAt': created, 'completedAt': created, 'status': 'completed',
                'model': model or 'default', 'effort': effort or 'default'})
            with self._connection:
                self._connection.execute('UPDATE claude_threads SET messages_json = ?, preview = ?, updated_at = ? WHERE thread_id = ?',
                    (json.dumps(messages, ensure_ascii=False), ' '.join(text.split())[:400], created, thread_id))

    def _record_question_answer(self, request: dict, response: dict) -> dict:
        thread_id, turn_id = request['threadId'], request['turnId']
        answers = {answer['id']: answer for answer in response['answers']}
        paragraphs = []
        for index, question in enumerate(request['questions']):
            answer = answers[question['id']]
            values = [*answer['selected']]
            if answer.get('custom'):
                values.append(answer['custom'])
            paragraphs.append(f"{index + 1}. {question['question']}\n答：" + '、'.join(values))
        text, created = '\n\n'.join(paragraphs), _now()
        item_id = f"user-question-{request['questionId']}"
        row = self._row(thread_id)
        messages = json.loads(row['messages_json'])
        if not any(message.get('id') == item_id for message in messages):
            messages.append({'id': item_id, 'role': 'user', 'text': text, 'createdAt': created, 'turnId': turn_id})
            with self._connection:
                self._connection.execute('UPDATE claude_threads SET messages_json = ?, updated_at = ? WHERE thread_id = ?',
                    (json.dumps(messages, ensure_ascii=False), created, thread_id))
        item = {'id': item_id, 'type': 'userMessage', 'status': 'completed', 'createdAt': created,
                'content': [{'type': 'text', 'text': text}]}
        if 'afterItemId' in request:
            item['afterItemId'] = request['afterItemId']
        return {'type': 'notification', 'method': 'item/started', 'params': {
            'threadId': thread_id, 'turnId': turn_id, 'item': item}}

    def _save_tool(self, thread_id: str, turn_id: str, tool: dict[str, Any]) -> None:
        row = self._row(thread_id)
        messages = json.loads(row['messages_json'])
        messages.append({'id': tool['id'], 'role': 'tool', 'item': tool, 'turnId': turn_id, 'createdAt': _now()})
        with self._connection:
            self._connection.execute('UPDATE claude_threads SET messages_json = ? WHERE thread_id = ?',
                                     (json.dumps(messages, ensure_ascii=False), thread_id))
