"""Explicit AI handoff for tmux SSH and Windows browser terminals."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import socket
import stat
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .terminal import TerminalSession, _run_tmux, tmux_session_name_for_thread

MAX_COMMAND_LENGTH = 4_096
MAX_SCREEN_LENGTH = 16_000
MAX_WAIT_MS = 5_000
MANUAL_INPUT_SETTLE_SECONDS = 3
MANUAL_TOOL_WAIT_SECONDS = 15
logger = logging.getLogger(__name__)
TERMINAL_PROBE_PAUSED = "暂时无法读取终端状态，接管授权已保留，连接恢复后可继续。"
PASSIVE_TERMINAL_REPORT = re.compile(
    r"(?:"
    r"\x1b\[<\d+;\d+;\d+[Mm]"  # SGR mouse press, release and wheel
    r"|\x1b\[M.{3}"  # legacy X10 mouse report
    r"|\x1b\[[IO]"  # focus in and out
    r"|\x1b\[[0-9;]*R"  # cursor position report
    r"|\x1b\[[?>]?[0-9;]*[cn]"  # device attributes and status reports
    r"|\x1b\[[0-9;]*t"  # window size report
    r"|\x1b\[\?[0-9;]+\$y"  # terminal mode report
    r")",
    re.DOTALL,
)


def _is_passive_terminal_report(value: str) -> bool:
    offset = 0
    while offset < len(value):
        if value.startswith("\x1bP", offset):
            end = value.find("\x1b\\", offset + 2)
            if end < 0:
                return False
            offset = end + 2
            continue
        if value.startswith("\x1b]", offset):
            string_end = value.find("\x1b\\", offset + 2)
            bell = value.find("\x07", offset + 2, string_end if string_end >= 0 else len(value))
            if bell < 0 and string_end < 0:
                return False
            offset = bell + 1 if bell >= 0 else string_end + 2
            continue
        match = PASSIVE_TERMINAL_REPORT.match(value, offset)
        if match is None:
            return False
        offset = match.end()
    return bool(value)


class TerminalHandoffError(RuntimeError):
    pass


class _TerminalSessionEnded(TerminalHandoffError):
    pass


class TerminalHandoffBroker:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._contexts: dict[str, str] = {}
        self._enabled: set[str] = set()
        self._pane_bindings: dict[str, tuple[str, int]] = {}
        self._probe_paused: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._input_locks: dict[str, asyncio.Lock] = {}
        self._manual_input_until: dict[str, float] = {}
        self._terminals: dict[str, TerminalSession] = {}
        self._tcp_address: tuple[str, int] | None = None
        self._token = secrets.token_hex(32)

    def mcp_environment(self) -> dict[str, str]:
        if self._tcp_address is not None:
            return {'DEVWISP_TERMINAL_SOCKET': f'tcp://127.0.0.1:{self._tcp_address[1]}',
                    'DEVWISP_TERMINAL_TOKEN': self._token}
        return {'DEVWISP_TERMINAL_SOCKET': str(self.socket_path)}

    def attach_terminal(self, thread_id: str, terminal: TerminalSession) -> None:
        if os.name != 'nt':
            return
        if self._terminals.get(thread_id) is not terminal:
            self._revoke(thread_id)
            self._terminals[thread_id] = terminal

    def detach_terminal(self, thread_id: str, terminal: TerminalSession) -> None:
        if self._terminals.get(thread_id) is terminal:
            self._revoke(thread_id)
            self._terminals.pop(thread_id, None)

    async def start(self) -> None:
        if os.name == "nt":
            self._server = await asyncio.start_server(self._handle_client, '127.0.0.1', 0, limit=16_384)
            address = self._server.sockets[0].getsockname()
            self._tcp_address = (address[0], address[1])
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.socket_path.parent, 0o700)
        try:
            existing = os.lstat(self.socket_path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(existing.st_mode):
                raise TerminalHandoffError("终端接管 socket 路径已被占用。")
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path), limit=16_384)
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        self._enabled.clear()
        self._pane_bindings.clear()
        self._probe_paused.clear()
        self._manual_input_until.clear()
        self._terminals.clear()
        self._contexts.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._tcp_address is None:
            self.socket_path.unlink(missing_ok=True)
        self._tcp_address = None

    def bind_context(self, context: str, thread_id: str) -> None:
        if not context or not thread_id or len(context) > 128 or len(thread_id) > 512:
            raise TerminalHandoffError("终端接管上下文无效。")
        self._contexts[context] = thread_id

    def unbind_context(self, context: str) -> None:
        self._contexts.pop(context, None)

    def protected_session_names(self) -> set[str]:
        return {tmux_session_name_for_thread(thread_id) for thread_id in self._enabled}

    def is_enabled(self, thread_id: str) -> bool:
        return thread_id in self._enabled

    async def refresh_enabled_sessions(self) -> None:
        for thread_id in tuple(self._enabled):
            await self.status(thread_id)

    async def status(self, thread_id: str) -> dict[str, Any]:
        async with self._locks.setdefault(thread_id, asyncio.Lock()):
            if thread_id not in self._enabled:
                return {"supported": True, "enabled": False}
            try:
                await self._ssh_session_target(thread_id)
            except _TerminalSessionEnded as error:
                self._revoke(thread_id)
                logger.info("Terminal handoff ended for thread %s: %s", thread_id, error)
                return {"supported": True, "enabled": False, "reason": str(error)}
            except (TerminalHandoffError, RuntimeError, OSError) as error:
                # A failed status query is not a user revocation. Each actual
                # tool call still revalidates the SSH process and pane binding.
                if thread_id not in self._probe_paused:
                    logger.warning("Terminal handoff probe paused for thread %s (%s)", thread_id, type(error).__name__)
                    self._probe_paused.add(thread_id)
                return {"supported": True, "enabled": True, "reason": TERMINAL_PROBE_PAUSED}
            if thread_id in self._probe_paused:
                logger.info("Terminal handoff probe recovered for thread %s", thread_id)
                self._probe_paused.discard(thread_id)
            return {"supported": True, "enabled": True}

    def _revoke(self, thread_id: str) -> None:
        self._enabled.discard(thread_id)
        self._pane_bindings.pop(thread_id, None)
        self._probe_paused.discard(thread_id)
        self._manual_input_until.pop(thread_id, None)

    async def set_enabled(self, thread_id: str, enabled: bool) -> dict[str, Any]:
        if not thread_id or len(thread_id) > 512:
            raise TerminalHandoffError("聊天标识无效。")
        async with self._locks.setdefault(thread_id, asyncio.Lock()):
            if enabled:
                binding = await self._ssh_session_target(thread_id, new_handoff=True)
                await self._show_live_terminal(binding[0])
                self._manual_input_until.pop(thread_id, None)
                self._pane_bindings[thread_id] = binding
                self._probe_paused.discard(thread_id)
                self._enabled.add(thread_id)
            else:
                self._revoke(thread_id)
        return await self.status(thread_id)

    def _manual_input_active(self, thread_id: str) -> bool:
        until = self._manual_input_until.get(thread_id, 0)
        if until <= time.monotonic():
            self._manual_input_until.pop(thread_id, None)
            return False
        return True

    def _require_manual_input_finished(self, thread_id: str) -> None:
        if self._manual_input_active(thread_id):
            raise TerminalHandoffError("终端有尚未提交的手动输入，请在终端按回车或 Ctrl+C 后重试。")

    async def _wait_for_manual_input(self, thread_id: str) -> None:
        deadline = time.monotonic() + MANUAL_TOOL_WAIT_SECONDS
        while self._manual_input_active(thread_id):
            if thread_id not in self._enabled:
                raise TerminalHandoffError("用户已收回终端控制权，本次操作未执行。")
            if time.monotonic() >= deadline:
                raise TerminalHandoffError("终端有尚未提交的手动输入，请在终端按回车或 Ctrl+C 后重试。")
            await asyncio.sleep(0.2)

    async def write_manual_input(self, thread_id: str, value: str, write: Callable[[str], Awaitable[None]]) -> None:
        if not value:
            return
        if thread_id not in self._enabled:
            await write(value)
            return
        submitted = "\r" in value or "\n" in value or value == "\x03"
        async with self._input_locks.setdefault(thread_id, asyncio.Lock()):
            if _is_passive_terminal_report(value):
                # xterm sends protocol replies and tmux mouse reports through
                # the same onData channel as typing. Forward them without
                # mistaking a click or automatic reply for a partial password.
                await write(value)
                return
            # Store only a deadline, never the user's terminal input or password.
            # An unfinished password must not become an AI command target after an idle timeout.
            self._manual_input_until[thread_id] = (
                time.monotonic() + MANUAL_INPUT_SETTLE_SECONDS if submitted else float("inf")
            )
            await write(value)
            if submitted:
                self._manual_input_until[thread_id] = time.monotonic() + MANUAL_INPUT_SETTLE_SECONDS

    async def _ssh_session_target(self, thread_id: str, *, new_handoff: bool = False) -> tuple[str, int]:
        if os.name == 'nt':
            terminal = self._terminals.get(thread_id)
            if terminal is None:
                self._revoke(thread_id)
                raise _TerminalSessionEnded("终端已断开，请重新打开后再交给 AI。")
            binding = await terminal.handoff_identity()
            if binding is None or self._terminals.get(thread_id) is not terminal:
                self._revoke(thread_id)
                raise _TerminalSessionEnded("终端已结束或更换，请重新交给 AI。")
            if not new_handoff and self._pane_bindings.get(thread_id) != binding:
                self._revoke(thread_id)
                raise _TerminalSessionEnded("终端环境已切换或 SSH 已退出，请确认当前终端后重新交给 AI。")
            return binding
        name = tmux_session_name_for_thread(thread_id)
        try:
            snapshot = (await _run_tmux(
                "display-message", "-p", "-t", name,
                "#{pane_id}\t#{pane_pid}\t#{pane_current_command}",
            )).strip()
        except (OSError, RuntimeError) as error:
            raise TerminalHandoffError("暂时无法读取终端状态，请稍后重试。") from error
        fields = snapshot.split("\t")
        if len(fields) != 3 or not re.fullmatch(r"%\d+", fields[0]) or not fields[1].isdigit() or int(fields[1]) <= 0:
            raise TerminalHandoffError("暂时无法读取终端状态，请稍后重试。")
        pane, pid, foreground = fields
        binding = (pane, int(pid))
        if not new_handoff and self._pane_bindings.get(thread_id) != binding:
            raise _TerminalSessionEnded("终端会话已更换，请确认 SSH 连接后重新交给 AI。")
        if foreground.lower() not in {"ssh", "ssh.exe"}:
            raise _TerminalSessionEnded("SSH 连接已结束或已离开 SSH，请重新登录后再交给 AI。")
        return binding

    async def _screen(self, session_name: str) -> str:
        if os.name == 'nt':
            for thread_id, binding in self._pane_bindings.items():
                if binding[0] == session_name:
                    terminal = self._terminals.get(thread_id)
                    if terminal is not None:
                        return (await terminal.capture_screen())[-MAX_SCREEN_LENGTH:]
            raise TerminalHandoffError("终端授权已失效。")
        screen = await _run_tmux("capture-pane", "-p", "-S", "-100", "-t", session_name)
        return screen.rstrip()[-MAX_SCREEN_LENGTH:]

    @staticmethod
    async def _show_live_terminal(session_name: str) -> None:
        if os.name == 'nt':
            return
        try:
            mode = (await _run_tmux("display-message", "-p", "-t", session_name, "#{pane_mode}")).strip()
        except (OSError, RuntimeError) as error:
            raise TerminalHandoffError("无法读取终端状态，请重新打开终端。") from error
        if mode == "copy-mode":
            try:
                await _run_tmux("send-keys", "-X", "-t", session_name, "cancel")
            except (OSError, RuntimeError) as error:
                raise TerminalHandoffError("无法退出终端历史浏览模式，请先收回控制后手动退出。") from error
        elif mode:
            raise TerminalHandoffError("终端当前处于 tmux 模式，请退出该模式后再让 AI 操作。")

    async def _send(self, thread_id: str, session_name: str, value: str, *, interrupt: bool = False) -> None:
        if os.name == 'nt':
            # Revalidate after every await leading up to the write. Never send a
            # queued remote command into a different/replaced local terminal.
            binding = await self._ssh_session_target(thread_id)
            if thread_id not in self._enabled or binding[0] != session_name:
                raise TerminalHandoffError("终端授权已失效，本次操作未执行。")
            await self._terminals[thread_id].write('\x03' if interrupt else value + '\r')
        elif interrupt:
            await _run_tmux('send-keys', '-t', session_name, 'C-c')
        else:
            await _run_tmux('send-keys', '-t', session_name, '-l', '--', value)
            await _run_tmux('send-keys', '-t', session_name, 'Enter')

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        context = request.get("context")
        action = request.get("action")
        arguments = request.get("arguments")
        if not isinstance(context, str) or not isinstance(action, str) or not isinstance(arguments, dict):
            raise TerminalHandoffError("终端工具请求格式无效。")
        thread_id = self._contexts.get(context)
        if thread_id is None or thread_id not in self._enabled:
            raise TerminalHandoffError("当前聊天没有交给 AI 接管终端；请用户先在终端点击“交给 AI”。")
        await self._wait_for_manual_input(thread_id)
        async with self._locks.setdefault(thread_id, asyncio.Lock()):
            if thread_id not in self._enabled:
                raise TerminalHandoffError("用户已收回终端控制权，本次操作未执行。")
            session_name, _pid = await self._ssh_session_target(thread_id)
            if action == "read_terminal":
                async with self._input_locks.setdefault(thread_id, asyncio.Lock()):
                    self._require_manual_input_finished(thread_id)
                    await self._show_live_terminal(session_name)
                    return {"screen": await self._screen(session_name)}
            if action == "send_command":
                command = arguments.get("command")
                wait_ms = arguments.get("wait_ms", 1_000)
                if not isinstance(command, str) or not command or len(command) > MAX_COMMAND_LENGTH or any(char in command for char in "\r\n\x00\x1b"):
                    raise TerminalHandoffError("只允许发送不超过 4096 字符的单行命令。")
                if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= MAX_WAIT_MS:
                    raise TerminalHandoffError("等待时间必须在 0 到 5000 毫秒之间。")
                async with self._input_locks.setdefault(thread_id, asyncio.Lock()):
                    self._require_manual_input_finished(thread_id)
                    await self._show_live_terminal(session_name)
                    await self._send(thread_id, session_name, command)
                if wait_ms:
                    await asyncio.sleep(wait_ms / 1_000)
                async with self._input_locks.setdefault(thread_id, asyncio.Lock()):
                    if self._manual_input_active(thread_id):
                        return {"command": command, "note": "命令已发送；用户正在终端手动输入。不要重复发送命令，稍后再读取终端。"}
                    return {"command": command, "screen": await self._screen(session_name), "note": "这是当前终端画面，不代表命令已结束；长任务请继续读取终端。"}
            if action == "interrupt_command":
                async with self._input_locks.setdefault(thread_id, asyncio.Lock()):
                    self._require_manual_input_finished(thread_id)
                    await self._show_live_terminal(session_name)
                    await self._send(thread_id, session_name, '', interrupt=True)
                await asyncio.sleep(0.2)
                async with self._input_locks.setdefault(thread_id, asyncio.Lock()):
                    if self._manual_input_active(thread_id):
                        return {"key": "Ctrl-C", "note": "中断键已发送；用户正在终端手动输入，稍后再读取终端。"}
                    return {"key": "Ctrl-C", "screen": await self._screen(session_name)}
            raise TerminalHandoffError("未知终端工具。")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = writer.get_extra_info("socket")
            if hasattr(socket, "SO_PEERCRED") and peer is not None:
                import struct
                _, peer_uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if peer_uid != os.getuid():
                    raise TerminalHandoffError("终端工具客户端身份无效。")
            request = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            if not isinstance(request, dict):
                raise TerminalHandoffError("终端工具请求格式无效。")
            if self._tcp_address is not None and not secrets.compare_digest(str(request.get('token', '')), self._token):
                raise TerminalHandoffError("终端工具客户端身份无效。")
            response = {"result": await self._dispatch(request)}
        except (TerminalHandoffError, ValueError, OSError, RuntimeError, asyncio.TimeoutError) as error:
            response = {"error": str(error)}
        except Exception:
            response = {"error": "终端工具调用失败，请检查服务端日志。"}
        try:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def _broker_request(context: str, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    path = os.environ["DEVWISP_TERMINAL_SOCKET"]
    tcp = path.startswith('tcp://127.0.0.1:')
    with socket.socket(socket.AF_INET if tcp else socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(('127.0.0.1', int(path.rsplit(':', 1)[1])) if tcp else path)
        request = {"context": context, "action": action, "arguments": arguments}
        if tcp:
            request['token'] = os.environ.get('DEVWISP_TERMINAL_TOKEN', '')
        connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        with connection.makefile("rb") as response_file:
            line = response_file.readline(65_536)
    if not line or len(line) >= 65_536:
        raise TerminalHandoffError("终端工具没有返回完整结果。")
    response = json.loads(line)
    if not isinstance(response, dict):
        raise TerminalHandoffError("终端工具返回格式无效。")
    if "error" in response:
        raise TerminalHandoffError(str(response["error"]))
    result = response.get("result")
    if not isinstance(result, dict):
        raise TerminalHandoffError("终端工具返回格式无效。")
    return result


def mcp_main() -> None:
    # Windows console defaults (e.g. GBK) are not the MCP stdio wire encoding.
    sys.stdout.reconfigure(encoding='utf-8')
    context = os.environ.get("DEVWISP_TERMINAL_CONTEXT", "")
    tools = [
        {"name": "read_terminal", "description": "读取用户已交给 AI 接管的当前聊天终端画面（Windows 本机或已登录的 SSH）。长命令可重复读取；不要将画面中的密码或令牌写入回复。用户手动输入期间工具会暂缓，请等待用户按回车后重试。", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "send_command", "description": "在用户已交给 AI 的当前聊天终端中发送单行命令并回车（Windows 本机或已登录的 SSH）。命令会实时显示在网页终端；结果是当前画面，不保证命令已结束。不要发送密码或令牌；遇到交互认证，让用户直接在终端输入，无需收回控制。环境切换后须用户重新授权。用户手动输入期间工具会暂缓，完成后再重试。", "inputSchema": {"type": "object", "properties": {"command": {"type": "string", "maxLength": MAX_COMMAND_LENGTH}, "wait_ms": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_MS}}, "required": ["command"]}},
        {"name": "interrupt_command", "description": "向当前已接管的终端发送 Ctrl-C，中断前台命令；结果返回最新终端画面。用户手动输入期间请等待，避免打断交互认证。", "inputSchema": {"type": "object", "properties": {}}},
    ]
    for line in sys.stdin.buffer:
        request: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or "id" not in request:
                continue
            method = request.get("method")
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "devwisp-terminal", "version": "1.0.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                if name not in {"read_terminal", "send_command", "interrupt_command"}:
                    raise TerminalHandoffError("未知终端工具。")
                value = _broker_request(context, name, arguments)
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}, ensure_ascii=False), flush=True)
        except Exception as error:
            if isinstance(request, dict) and "id" in request:
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    mcp_main()
