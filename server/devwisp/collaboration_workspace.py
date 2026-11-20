"""Isolated development snapshots with checked, repeatable file integration."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .collaboration_groups import CollaborationError
from .collaboration_plan import RESERVED, file_path

IGNORED = RESERVED | {'.cache', '.pytest_cache', 'dist', 'build', 'coverage'}


def git(cwd: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '-c', f'core.hooksPath={os.devnull}', '-C', str(cwd), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode:
        raise CollaborationError('无法准备独立开发目录，请检查项目 Git 状态。')
    return result.stdout


def checked_path(root: Path, name: str) -> Path:
    target = root / file_path(name)
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise CollaborationError(f'开发文件包含符号链接，未自动合入：{name}')
    if target.exists() and not target.is_file():
        raise CollaborationError(f'开发文件不是普通文件，未自动合入：{name}')
    return target


def fingerprint(path: Path) -> dict | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise CollaborationError('文件类型已变化，未自动合入。')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return {'sha256': digest.hexdigest(), 'mode': stat.S_IMODE(path.stat().st_mode)}


def files(root: Path, use_git: bool) -> list[str]:
    if use_git:
        names = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode('utf-8').split('\0')
        return sorted({name for name in names if name and not any(part in RESERVED for part in Path(name).parts)})
    names = []
    for directory, children, entries in os.walk(root, followlinks=False):
        children[:] = [name for name in children if name not in IGNORED]
        for name in children:
            if (Path(directory) / name).is_symlink():
                raise CollaborationError('项目包含目录符号链接，请先明确独立开发范围。')
        names.extend((Path(directory) / name).relative_to(root).as_posix() for name in entries if name != '.git')
    return sorted(names)


def reuse_dependencies(source: Path, target: Path) -> None:
    # Mirror package entries, keeping writable build caches local to each task.
    # No dependency installation or writes to the source environment are needed.
    for parent in (source, *source.parents):
        modules = parent / 'node_modules'
        if not modules.is_dir():
            continue
        destination = target / 'node_modules'
        destination.mkdir(exist_ok=True)
        for package in modules.iterdir():
            if package.name.startswith('.') and package.name not in {'.bin', '.pnpm'}:
                continue
            link = destination / package.name
            if (package.name.startswith('@') or package.name == '.bin') and package.is_dir() and not link.is_symlink():
                link.mkdir(exist_ok=True)
                for scoped in package.iterdir():
                    scoped_link = link / scoped.name
                    if not scoped_link.exists() and not scoped_link.is_symlink():
                        scoped_link.symlink_to(scoped.resolve(), target_is_directory=scoped.is_dir())
            elif not link.exists() and not link.is_symlink():
                link.symlink_to(package.resolve(), target_is_directory=package.is_dir())
    environment = source / '.venv'
    if environment.is_dir() and not (target / '.venv').exists():
        (target / '.venv').symlink_to(environment.resolve(), target_is_directory=True)


def prepare(cwd: str) -> dict:
    source = Path(cwd).resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix='devwisp-team-work-'))
    worktree, repository = root / 'work', None
    try:
        try:
            repository = Path(git(source, 'rev-parse', '--show-toplevel').decode().strip())
            git(source, 'rev-parse', '--verify', 'HEAD')
        except (CollaborationError, FileNotFoundError):
            repository = None
        if repository is not None:
            git(repository, 'worktree', 'add', '--detach', str(worktree), 'HEAD')
            target = worktree / source.relative_to(repository)
            target.mkdir(parents=True, exist_ok=True)
            # Remove HEAD's subtree files absent from the current working tree.
            for name in files(target, True):
                candidate = checked_path(target, name)
                if not (source / name).exists() and candidate.exists():
                    candidate.unlink()
        else:
            target = worktree
            target.mkdir()
        baseline = {}
        for name in files(source, repository is not None):
            origin = checked_path(source, name)
            if not origin.exists():
                continue
            destination = checked_path(target, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)
            baseline[name] = fingerprint(destination)
        reuse_dependencies(source, target)
        context = {'root': str(root), 'cwd': str(target), 'source': str(source),
                   'repository': str(repository) if repository is not None else None}
        (root / 'baseline.json').write_text(json.dumps(baseline), encoding='utf-8')
        return context
    except BaseException:
        cleanup({'root': str(root), 'repository': str(repository) if repository else None})
        raise


def integrate(context: dict, allowed: list[str]) -> list[str]:
    root, source = Path(context['cwd']), Path(context['source'])
    baseline = json.loads((Path(context['root']) / 'baseline.json').read_text(encoding='utf-8'))
    current = {name: fingerprint(checked_path(root, name)) for name in files(root, bool(context['repository']))
               if checked_path(root, name).exists()}
    changes = sorted(name for name in baseline.keys() | current.keys() if baseline.get(name) != current.get(name))
    outside = set(changes) - set(allowed)
    if outside:
        raise CollaborationError('成员修改超出分工范围，成果已保留，未合入：' + ', '.join(sorted(outside)[:8]))
    originals, replacements = {}, {}
    for name in changes:
        target = checked_path(source, name)
        observed, before, after = fingerprint(target), baseline.get(name), current.get(name)
        if observed == after:
            continue  # Recover an integration accepted before service shutdown.
        if observed != before:
            raise CollaborationError('源文件在开发期间已变化，成果已保留，未覆盖：' + name)
        originals[name] = (target.read_bytes(), observed['mode']) if observed else None
        replacements[name] = (checked_path(root, name).read_bytes(), after['mode']) if after else None
    written = []
    try:
        for name, replacement in replacements.items():
            target = checked_path(source, name)
            if fingerprint(target) != baseline.get(name):
                raise CollaborationError('源文件在合入时已变化，未覆盖：' + name)
            replace_file(target, replacement)
            written.append(name)
    except BaseException:
        for name in reversed(written):
            target = checked_path(source, name)
            if fingerprint(target) == current.get(name):
                replace_file(target, originals[name])
        raise
    return changes


def replace_file(target: Path, value: tuple[bytes, int] | None) -> None:
    if value is None:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.devwisp-merge-', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(value[0])
        temporary.chmod(value[1])
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def cleanup(context: dict) -> None:
    root = Path(context['root'])
    if root.parent != Path(tempfile.gettempdir()) or not root.name.startswith('devwisp-team-work-'):
        raise CollaborationError('独立开发目录位置无效，未执行清理。')
    if context.get('repository') and (root / 'work').exists():
        git(Path(context['repository']), 'worktree', 'remove', '--force', str(root / 'work'))
    shutil.rmtree(root, ignore_errors=True)
