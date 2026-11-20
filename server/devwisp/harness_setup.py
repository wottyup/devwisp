"""Managed SDK setup in the backend's own Python environment."""
from __future__ import annotations

import asyncio
import importlib.util
import importlib
import json
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

SDK_VERSION = '0.2.2rc2'


class HarnessSetup:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / 'harness-runtime.json'
        self.task: asyncio.Task | None = None
        self.operation = 'idle'
        self.message = ''
        self.checked = False

    def executable(self) -> str | None:
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text(encoding='utf-8')).get('executable', '')
        if not isinstance(value, str):
            raise ValueError('Harness 程序路径配置无效。')
        return value or None

    def status(self) -> dict[str, Any]:
        try:
            sdk = version('deepseek-harness-sdk')
        except PackageNotFoundError:
            sdk = None
        try:
            executable = self.executable()
            bundled = importlib.util.find_spec('deepseek_harness_runtime') is not None
            error = None
        except (OSError, ValueError):
            executable, bundled, error = None, False, '无法读取 Harness 运行配置。'
        installed = sdk == SDK_VERSION and (Path(executable).is_file() if executable else bundled)
        return {'installed': installed, 'sdkVersion': sdk, 'requiredVersion': SDK_VERSION,
                'python': sys.executable, 'executable': executable or '', 'checked': self.checked,
                'busy': self.operation != 'idle' or (self.task is not None and not self.task.done()),
                'operation': self.operation, 'message': error or self.message}

    async def _run(self, *args: str, timeout: int) -> int:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, cwd=self.directory,
            **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
        try:
            return await asyncio.wait_for(proc.wait(), timeout)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def check(self, executable: str | None = None) -> None:
        selected = self.executable() if executable is None else executable or None
        # Fresh interpreter sees newly installed dependencies without reloading
        # modules used by other active chats. Context manager closes the SDK.
        script = (
            'import sys,tempfile; from deepseek_harness import DeepSeekHarness; '
            'd=tempfile.TemporaryDirectory(prefix="devwisp-setup-"); '
            'r=DeepSeekHarness(cwd=d.name,runtime_cwd=sys.argv[1],dsh_home=d.name,'
            'dsh_bin=sys.argv[2] or None,initialize_timeout_seconds=15); '
            '\ntry: r.start()\nfinally: r.close(); d.cleanup()'
        )
        result = await self._run(sys.executable, '-c', script, str(self.directory), selected or '', timeout=25)
        if result:
            raise ValueError('Harness 未能启动。请安装/修复组件；外部程序须支持当前 SDK 的 sdk 协议。')

    def start(self, operation: str) -> dict[str, Any]:
        if self.operation != 'idle' or (self.task and not self.task.done()):
            return self.status()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.operation, self.message, self.checked = operation, '正在准备运行组件…', False
        self.task = asyncio.create_task(self._work(operation))
        return self.status()

    async def _work(self, operation: str) -> None:
        try:
            if operation == 'install':
                code = await self._run(sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                    '--no-input', '--upgrade', '--force-reinstall',
                    f'deepseek-harness-sdk=={SDK_VERSION}', f'deepseek-harness-runtime-bin=={SDK_VERSION}', timeout=300)
                if code:
                    raise ValueError('安装失败，请检查网络代理、当前 Python 的 pip 及安装目录写入权限，再重试。')
                importlib.invalidate_caches()
            await self.check()
            self.checked = True
            self.message = ('安装及启动检查通过；若服务曾加载过旧 SDK，请结束当前任务后重启服务。'
                            if operation == 'install' else 'Harness 启动检查通过，可以配置 API 后聊天。')
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, TimeoutError):
            self.checked = False
            self.message = '安装或启动检查失败，请检查网络、Python 环境写入权限和外部程序路径；已有配置及会话未更改。'
        finally:
            self.operation = 'idle'

    async def save(self, executable: str) -> dict[str, Any]:
        if self.operation != 'idle' or (self.task and not self.task.done()):
            raise ValueError('请等待当前安装或检查完成。')
        selected = executable.strip()
        if selected:
            path = Path(selected).expanduser()
            if not path.is_absolute() or not path.is_file() or (os.name == 'nt' and path.suffix.lower() != '.exe'):
                raise ValueError('请选择已安装的原生 Harness 可执行文件的绝对路径（Windows 为 .exe），不支持命令或脚本。')
            selected = str(path.resolve())
        self.directory.mkdir(parents=True, exist_ok=True)
        # Validate before replacing the working choice.
        self.operation = 'check'
        try:
            await self.check(selected)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'executable': selected}), encoding='utf-8')
            temporary.replace(self.path)
            self.checked, self.message = True, '运行方式已保存，下一轮生效。'
        finally:
            self.operation = 'idle'
        return self.status()

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


if __name__ == '__main__':
    # Startup-only bootstrap: a missing SDK must not prevent the settings page
    # from opening if installation fails. Never replace an installed version
    # silently; its compatibility is reported by the settings card.
    import tempfile
    async def bootstrap() -> None:
        if importlib.util.find_spec('deepseek_harness') is not None and importlib.util.find_spec('deepseek_harness_runtime') is not None:
            return
        print('正在准备 DeepSeek Harness 运行组件…', flush=True)
        with tempfile.TemporaryDirectory(prefix='devwisp-bootstrap-') as directory:
            setup = HarnessSetup(Path(directory))
            await setup._work('install')
            print(setup.message, flush=True)
    asyncio.run(bootstrap())
