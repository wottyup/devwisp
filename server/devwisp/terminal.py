from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import secrets
import shutil
import signal
import struct
import subprocess
import time
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)

TMUX_SOCKET_NAME = "devwisp"
TMUX_SESSION_PREFIX = "devwisp-"
TMUX_KEEPER_SESSION_NAME = "devwisp-keeper"
TMUX_LAST_USED_OPTION = "@devwisp_last_used"
TMUX_HISTORY_LIMIT = 5_000
TMUX_IDLE_TIMEOUT_SECONDS = 30 * 60
TMUX_MAX_DETACHED_SESSIONS = 10
TMUX_CLEANUP_INTERVAL_SECONDS = 60
TMUX_ANDROID_COMPACT_PROMPT_OPTION = "@devwisp_android_compact_prompt"


def tmux_session_name_for_thread(thread_id: str) -> str:
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
    return f"{TMUX_SESSION_PREFIX}{digest}"


async def _run_tmux(*arguments: str, check: bool = True) -> str:
    executable = shutil.which("tmux")
    if not executable:
        raise RuntimeError("本机未安装 tmux，无法保留终端会话。请安装 tmux 后重启 DevWisp。")
    environment = os.environ.copy()
    environment.pop("TMUX", None)
    # uvloop uses socket pairs for subprocess PIPEs. A cold-started tmux
    # daemon keeps those open after its client exits, so communicate() never
    # sees EOF. Use ordinary subprocess pipes off the event-loop thread.
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [executable, "-L", TMUX_SOCKET_NAME, *arguments],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as error:
        # run() kills and reaps its own client before raising.
        raise RuntimeError("tmux 命令超时。") from error
    output = result.stdout.decode("utf-8", errors="replace")
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "tmux 命令执行失败。")
    return output


async def _set_tmux_last_used(session_name: str, timestamp: int | None = None) -> None:
    await _run_tmux(
        "set-option",
        "-t",
        session_name,
        TMUX_LAST_USED_OPTION,
        str(timestamp if timestamp is not None else int(time.time())),
    )


async def cleanup_tmux_sessions(protected_session_names: set[str] | None = None) -> None:
    if not shutil.which("tmux"):
        return
    protected = protected_session_names or set()
    now = int(time.time())
    try:
        listing = await _run_tmux(
            "list-sessions",
            "-F",
            f"#{{session_name}}|#{{session_attached}}|#{{{TMUX_LAST_USED_OPTION}}}",
            check=False,
        )
    except (OSError, RuntimeError) as error:
        logger.warning("Could not inspect tmux terminal sessions: %s", error)
        return

    detached: list[tuple[int, str]] = []
    for row in listing.splitlines():
        fields = row.split("|", 2)
        if len(fields) != 3:
            continue
        session_name, attached_text, last_used_text = fields
        if not session_name.startswith(TMUX_SESSION_PREFIX) or session_name == TMUX_KEEPER_SESSION_NAME:
            continue
        try:
            attached = int(attached_text)
        except ValueError:
            attached = 0
        if attached > 0 or session_name in protected:
            with suppress(OSError, RuntimeError):
                await _set_tmux_last_used(session_name, now)
            continue

        try:
            last_used = int(last_used_text)
        except ValueError:
            last_used = now
            with suppress(OSError, RuntimeError):
                await _set_tmux_last_used(session_name, last_used)
        if now - last_used >= TMUX_IDLE_TIMEOUT_SECONDS:
            with suppress(OSError, RuntimeError):
                await _run_tmux("kill-session", "-t", session_name)
            continue
        detached.append((last_used, session_name))

    detached.sort()
    for _, session_name in detached[:-TMUX_MAX_DETACHED_SESSIONS]:
        with suppress(OSError, RuntimeError):
            await _run_tmux("kill-session", "-t", session_name)


class TerminalSession:
    """One interactive shell attached to a browser terminal websocket."""

    def __init__(self, cwd: Path, columns: int, rows: int, thread_id: str | None = None, compact_prompt: bool = False) -> None:
        self.cwd = cwd
        self.columns = self._bounded_dimension(columns, 100, 20, 300)
        self.rows = self._bounded_dimension(rows, 30, 5, 120)
        self.argv = self._shell_argv()
        self.shell_name = Path(self.argv[0]).name
        self.tmux_session_name = (
            tmux_session_name_for_thread(thread_id)
            if thread_id and os.name != "nt"
            else None
        )
        self.compact_prompt = compact_prompt
        self._master_fd: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._windows_process: object | None = None
        self._handoff_id = secrets.token_hex(16)
        self._screen: object | None = None
        self._screen_stream: object | None = None
        self._closed = False

    @staticmethod
    def _bounded_dimension(value: int, default: int, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _shell_argv() -> list[str]:
        if os.name == "nt":
            shell = shutil.which("pwsh.exe") or shutil.which("pwsh")
            if shell:
                return [shell, "-NoLogo", "-NoExit"]
            shell = shutil.which("powershell.exe") or shutil.which("powershell")
            if shell:
                return [shell, "-NoLogo", "-NoExit"]
            return [os.environ.get("COMSPEC", "cmd.exe")]

        configured = os.environ.get("SHELL", "")
        shell = shutil.which(configured) if configured else None
        shell = shell or shutil.which("bash") or shutil.which("zsh") or "/bin/sh"
        return [shell, "-i"]

    @classmethod
    async def create(
        cls,
        cwd: Path,
        columns: int = 100,
        rows: int = 30,
        thread_id: str | None = None,
        compact_prompt: bool = False,
    ) -> TerminalSession:
        session = cls(cwd, columns, rows, thread_id, compact_prompt)
        try:
            if os.name == "nt":
                await session._start_windows()
            else:
                await session._start_posix()
        except Exception:
            await session.close()
            raise
        return session

    @property
    def persistent(self) -> bool:
        return self.tmux_session_name is not None

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["TERM"] = "xterm-256color"
        environment["COLORTERM"] = "truecolor"
        return environment

    async def _start_posix(self) -> None:
        import fcntl
        import pty
        import termios

        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.columns, 0, 0))
            environment = self._environment()
            if self.tmux_session_name:
                executable = shutil.which("tmux")
                if not executable:
                    raise RuntimeError("本机未安装 tmux，无法保留终端会话。请安装 tmux 后重启 DevWisp。")
                environment.pop("TMUX", None)
                # Cold starts have no socket yet. Await detached creation/reuse
                # before any configuration client or browser attachment races it.
                try:
                    await _run_tmux(
                        '-u', 'new-session', '-d', '-s', self.tmux_session_name,
                        '-c', str(self.cwd), '-x', str(self.columns), '-y', str(self.rows),
                    )
                except RuntimeError as creation_error:
                    # new-session -Ad tries to attach on tmux 3.4 when the
                    # session exists, which requires a TTY. Reuse it explicitly.
                    try:
                        await _run_tmux('has-session', '-t', f'={self.tmux_session_name}')
                    except RuntimeError:
                        raise creation_error

                await _run_tmux(
                    'set-window-option', '-t', self.tmux_session_name,
                    'history-limit', str(TMUX_HISTORY_LIMIT),
                )
                await _run_tmux('set-option', '-t', self.tmux_session_name, 'mouse', 'on')
                await _set_tmux_last_used(self.tmux_session_name)
                argv = [executable, '-L', TMUX_SOCKET_NAME, '-u',
                        'attach-session', '-t', f'={self.tmux_session_name}']
            else:
                argv = self.argv
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.cwd),
                env=environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
            )
            if self.tmux_session_name and self.compact_prompt:
                with suppress(OSError, RuntimeError, ValueError):
                    await self._compact_bash_prompt_if_idle()
        except Exception:
            if self._process is not None and self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
                self._process = None
            with suppress(OSError):
                os.close(master_fd)
            self._master_fd = None
            raise
        finally:
            os.close(slave_fd)

    async def _compact_bash_prompt_if_idle(self) -> None:
        session_name = self.tmux_session_name
        if not session_name:
            return
        # A reconnect may restore old screen text. Touch only this idle Bash
        # pane before the new Android client receives its ready event.
        for _ in range(10):
            details = await _run_tmux(
                "display-message", "-p", "-t", session_name,
                f"#{{pane_current_command}}|#{{session_attached}}|#{{pane_in_mode}}|#{{cursor_y}}|#{{{TMUX_ANDROID_COMPACT_PROMPT_OPTION}}}",
            )
            command, attached, in_mode, cursor_y, already_compact = details.strip().split("|", 4)
            if command != "bash" or attached != "1" or in_mode != "0" or already_compact == "1":
                return
            lines = (await _run_tmux("capture-pane", "-p", "-t", session_name)).splitlines()
            row = int(cursor_y)
            if 0 <= row < len(lines) and lines[row].rstrip().endswith(("#", "$")):
                break
            await asyncio.sleep(0.05)
        else:
            return
        await _run_tmux("send-keys", "-t", session_name, "-l", "PS1='\\W\\$ '")
        await _run_tmux("send-keys", "-t", session_name, "Enter")
        await _run_tmux("send-keys", "-t", session_name, "C-l")
        await _run_tmux("set-option", "-t", session_name, TMUX_ANDROID_COMPACT_PROMPT_OPTION, "1")

    async def _start_windows(self) -> None:
        try:
            from winpty import PtyProcess
            import pyte
        except ImportError as error:
            raise RuntimeError("Windows 本机终端依赖 pywinpty 和 pyte，请重新安装 server/requirements.txt。") from error

        self._screen = pyte.Screen(self.columns, self.rows)
        self._screen_stream = pyte.Stream(self._screen)

        # pywinpty's native blocking reader can hold the GIL when the
        # terminal has no output, preventing unrelated API requests.
        os.environ["PYWINPTY_BLOCK"] = "0"
        self._windows_process = await asyncio.to_thread(
            PtyProcess.spawn,
            self.argv,
            cwd=str(self.cwd),
            env=self._environment(),
            dimensions=(self.rows, self.columns),
        )

    async def read(self, size: int = 65_536) -> bytes:
        if self._closed:
            return b""
        if self._windows_process is not None:
            process = self._windows_process
            while not self._closed:
                try:
                    output = await asyncio.to_thread(process.read, size)  # type: ignore[attr-defined]
                except (EOFError, OSError, ValueError):
                    return b""
                if isinstance(output, str):
                    # pywinpty inserts this sentinel on each empty native read.
                    output = output.replace("0011Ignore", "")
                if output:
                    if self._screen_stream is not None:
                        self._screen_stream.feed(output if isinstance(output, str) else output.decode('utf-8', errors='replace'))
                    return output.encode("utf-8", errors="replace") if isinstance(output, str) else bytes(output)
                # Non-blocking pywinpty reads return an empty string while the
                # shell is idle. That is not an end-of-session signal.
                if not await asyncio.to_thread(process.isalive):  # type: ignore[attr-defined]
                    return b""
                await asyncio.sleep(0.05)
            return b""

        if self._master_fd is None:
            return b""
        try:
            return await asyncio.to_thread(os.read, self._master_fd, size)
        except OSError as error:
            if error.errno in {errno.EIO, errno.EBADF}:
                return b""
            raise

    async def write(self, value: str) -> None:
        if self._closed or not value:
            return
        if self._windows_process is not None:
            await asyncio.to_thread(self._windows_process.write, value)  # type: ignore[attr-defined]
            return
        if self._master_fd is not None:
            fd = self._master_fd
            data = memoryview(value.encode("utf-8"))

            def write_all() -> None:
                remaining = data
                while remaining:
                    written = os.write(fd, remaining)
                    remaining = remaining[written:]

            await asyncio.to_thread(write_all)

    @property
    def handoff_available(self) -> bool:
        return self.persistent or self._windows_process is not None and not self._closed

    async def capture_screen(self) -> str:
        if self._closed or self._screen is None:
            raise RuntimeError("终端已断开，请重新打开后再交给 AI。")
        return '\n'.join(line.rstrip() for line in self._screen.display).rstrip()

    async def handoff_identity(self) -> tuple[str, int] | None:
        """Bind grants to this PTY and its local shell or exact SSH process.

        Do not inspect command lines or collect typed credentials. A shell-to-SSH
        transition and SSH exit produce different identities and revoke grants.
        """
        process = self._windows_process
        if self._closed or process is None:
            return None

        def inspect() -> tuple[str, int] | None:
            import psutil
            try:
                root = psutil.Process(process.pid)
                if not root.is_running():
                    return None
                ssh = [child for child in root.children(recursive=True)
                       if child.is_running() and child.name().lower() in {'ssh', 'ssh.exe'}]
                if len(ssh) > 1:
                    raise RuntimeError("终端中存在多个 SSH 进程，无法安全确认接管目标。")
                target = ssh[0] if ssh else root
                scope = 'ssh' if ssh else 'local'
                return (f'{self._handoff_id}:{scope}:{target.create_time()}', target.pid)
            except psutil.NoSuchProcess:
                return None
            except psutil.AccessDenied as error:
                raise RuntimeError("无法确认终端进程身份，本次未执行操作。") from error

        return await asyncio.to_thread(inspect)

    async def resize(self, columns: int, rows: int) -> None:
        if self._closed:
            return
        self.columns = self._bounded_dimension(columns, self.columns, 20, 300)
        self.rows = self._bounded_dimension(rows, self.rows, 5, 120)
        if self._screen is not None:
            self._screen.resize(lines=self.rows, columns=self.columns)
        if self._windows_process is not None:
            await asyncio.to_thread(self._windows_process.setwinsize, self.rows, self.columns)  # type: ignore[attr-defined]
            return
        if self._master_fd is not None:
            import fcntl
            import termios

            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.columns, 0, 0))
            # The child has no controlling TTY, so TIOCSWINSZ alone does not
            # notify tmux (or the shell) to redraw at the new dimensions.
            process = self._process
            if process is not None and process.returncode is None:
                with suppress(OSError):
                    process.send_signal(signal.SIGWINCH)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._windows_process is not None:
            process = self._windows_process
            try:
                # terminate() marks PtyProcess.closed without closing its socket
                # pair; close(force=True) handles both resources and the child.
                await asyncio.to_thread(process.close, force=True)  # type: ignore[attr-defined]
            except (OSError, RuntimeError, ValueError):
                pass
            self._windows_process = None

        process = self._process
        if process is not None and process.returncode is None:
            try:
                if self.tmux_session_name:
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                with suppress(ProcessLookupError, PermissionError, OSError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.5)
            except TimeoutError:
                try:
                    if self.tmux_session_name:
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    with suppress(ProcessLookupError, PermissionError, OSError):
                        process.kill()
                await process.wait()
        self._process = None

        if self.tmux_session_name:
            with suppress(OSError, RuntimeError):
                await _set_tmux_last_used(self.tmux_session_name)

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
