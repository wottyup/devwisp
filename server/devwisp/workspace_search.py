from __future__ import annotations

import codecs
import hashlib
import hmac
import os
import re
import stat
import tempfile
import time
from itertools import chain
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, BinaryIO

from fastapi import HTTPException

MAX_DISPLAY_MATCHES = 1_000
MAX_DISPLAY_FILES = 100
TEXT_CHUNK_SIZE = 64 * 1024
SKIP_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".pnpm", ".pnpm-store", ".venv", "venv",
    "__pycache__", ".cache", ".gradle", ".kotlin", ".cxx", ".next", ".nuxt", "dist", "build",
    ".devwisp", "artifacts",
})
Progress = Callable[[dict[str, Any]], None]


class WorkspaceOperationCancelled(Exception):
    pass


class _UnsupportedText(Exception):
    pass


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise WorkspaceOperationCancelled


def _candidate(base: Path, relative: str) -> Path:
    parts = relative.replace("\\", "/").split("/")
    if not relative or relative.startswith(("/", "\\")) or any(part in {"", ".", ".."} for part in parts) or "\x00" in relative:
        raise HTTPException(400, "文件路径无效")
    if len(relative) > 4096 or (len(relative) > 1 and relative[1] == ":"):
        raise HTTPException(400, "文件路径无效或过长")
    candidate = base
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise HTTPException(409, f"文件已变化，请重新查找：{relative}")
    if not candidate.resolve().is_relative_to(base):
        raise HTTPException(403, "不能访问工作目录以外的文件")
    return candidate


def _open_source(candidate: Path) -> BinaryIO:
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("Not a regular file")
    return os.fdopen(descriptor, "rb")


def _text_chunks(source: BinaryIO, digest: Any, cancel: Event | None, *, normalize: bool) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    pending_cr = ""
    while True:
        _check_cancel(cancel)
        raw = source.read(TEXT_CHUNK_SIZE)
        if b"\x00" in raw:
            raise _UnsupportedText
        digest.update(raw)
        text = decoder.decode(raw, final=not raw)
        if normalize:
            text = pending_cr + text
            pending_cr = "\r" if raw and text.endswith("\r") else ""
            if pending_cr:
                text = text[:-1]
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        yield text
        if not raw:
            break


def _scan_file(base: Path, relative: str, pattern: re.Pattern[str], details: int, cancel: Event | None) -> dict[str, Any]:
    candidate = _candidate(base, relative)
    digest = hashlib.sha256()
    matches: list[dict[str, Any]] = []
    count = 0
    buffer = ""
    cursor = 0
    base_line, base_column = 1, 1
    # Retain the whole possible boundary match and enough trailing snippet.
    overlap = len(pattern.pattern) + 100
    with _open_source(candidate) as source:
        initial_stat = os.fstat(source.fileno())
        has_bom = source.peek(3).startswith(b"\xef\xbb\xbf")
        chunks = _text_chunks(source, digest, cancel, normalize=True)
        # One empty sentinel flushes the trailing overlap after decoding EOF.
        for chunk in chain(chunks, [None]):
            final = chunk is None
            buffer += chunk or ""
            cutoff = len(buffer) if final else max(cursor, len(buffer) - overlap)
            consumed = cutoff
            line, column = base_line, base_column
            previous = 0
            for match in pattern.finditer(buffer, cursor):
                if match.start() >= cutoff:
                    break
                if count % 256 == 0:
                    _check_cancel(cancel)
                count += 1
                consumed = max(consumed, match.end())
                if len(matches) >= details:
                    continue
                prefix = buffer[previous:match.start()]
                newlines = prefix.count("\n")
                if newlines:
                    line += newlines
                    column = len(prefix) - prefix.rfind("\n")
                else:
                    column += len(prefix)
                previous = match.start()
                before_start = max(buffer.rfind("\n", 0, match.start()) + 1, match.start() - 65)
                after_end = buffer.find("\n", match.end(), match.end() + 100)
                matches.append({
                    "line": line, "column": column,
                    "before": buffer[before_start:match.start()], "text": match.group(),
                    "after": buffer[match.end():after_end if after_end >= 0 else match.end() + 100],
                })
            drop = max(0, consumed - 65)
            prefix = buffer[:drop]
            newlines = prefix.count("\n")
            if newlines:
                base_line += newlines
                base_column = len(prefix) - prefix.rfind("\n")
            else:
                base_column += len(prefix)
            buffer = buffer[drop:]
            cursor = consumed - drop
        current_stat = os.fstat(source.fileno())
        if (initial_stat.st_size, initial_stat.st_mtime_ns) != (current_stat.st_size, current_stat.st_mtime_ns):
            raise OSError("File changed during search")
    return {"path": relative, "revision": digest.hexdigest(), "hasBom": has_bom,
            "matches": matches, "matchCount": count, "complete": True}


class _WorkspaceTextScanner:
    def __init__(self, base: Path, progress: Progress | None, cancel: Event | None):
        self.base = base
        self.progress = progress
        self.cancel = cancel
        self.scanned_files = self.skipped_files = self.file_count = self.match_count = 0
        self.last_report = 0.0

    def report(self, phase: str = "scan", *, force: bool = False, **values: Any) -> None:
        now = time.monotonic()
        if self.progress is not None and (force or now - self.last_report >= .2):
            self.last_report = now
            self.progress({"phase": phase, "scannedFiles": self.scanned_files, "skippedFiles": self.skipped_files,
                           "fileCount": self.file_count, "matchCount": self.match_count, **values})

    def paths(self) -> Iterator[str]:
        directories = [self.base]
        while directories:
            _check_cancel(self.cancel)
            directory = directories.pop()
            if directory.is_symlink() or not directory.resolve().is_relative_to(self.base):
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        _check_cancel(self.cancel)
                        self.report()
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in SKIP_DIRECTORIES:
                                    directories.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False) and not entry.name.startswith((".devwisp-", ".code-remote-")):
                                self.scanned_files += 1
                                yield Path(entry.path).relative_to(self.base).as_posix()
                        except OSError:
                            self.skipped_files += 1
            except OSError:
                self.skipped_files += 1

    def scan(self, relative: str, pattern: re.Pattern[str], details: int) -> dict[str, Any] | None:
        try:
            current = _scan_file(self.base, relative, pattern, details, self.cancel)
        except (OSError, ValueError, UnicodeError, _UnsupportedText, HTTPException):
            self.skipped_files += 1
            return None
        if current["matchCount"]:
            self.file_count += 1
            self.match_count += current["matchCount"]
        self.report()
        return current


def _search_pattern(query: str, match_case: bool) -> re.Pattern[str]:
    if not query or len(query) > 1000 or any(character in query for character in "\r\n\x00"):
        raise HTTPException(400, "请输入不超过 1,000 个字符的单行查找内容。")
    return re.compile(re.escape(query), 0 if match_case else re.IGNORECASE)


def _validate_replacement(replacement: str) -> None:
    if "\x00" in replacement:
        raise HTTPException(415, "文本内容不能包含二进制空字符")
    try:
        replacement.encode("utf-8")
    except UnicodeEncodeError as error:
        raise HTTPException(422, "替换内容不是有效的 UTF-8 文本。") from error


def search_workspace(base: Path, query: str, match_case: bool, *, progress: Progress | None = None, cancel: Event | None = None) -> dict[str, Any]:
    base = base.resolve()
    pattern = _search_pattern(query, match_case)
    scanner = _WorkspaceTextScanner(base, progress, cancel)
    files: list[dict[str, Any]] = []
    displayed = 0
    for relative in scanner.paths():
        available = MAX_DISPLAY_MATCHES - displayed if len(files) < MAX_DISPLAY_FILES else 0
        current = scanner.scan(relative, pattern, available)
        if current is not None and current["matches"]:
            displayed += len(current["matches"])
            current.pop("hasBom")
            files.append(current)
    scanner.report(force=True)
    files.sort(key=lambda group: group["path"].casefold())
    return {"cwd": str(base), "files": files, "matchCount": scanner.match_count, "fileCount": scanner.file_count,
            "scannedFiles": scanner.scanned_files, "skippedFiles": scanner.skipped_files, "truncated": False,
            "displayTruncated": displayed < scanner.match_count}


@dataclass
class _PreparedFile:
    path: str
    temporary: Path
    revision: str
    count: int


def _prepare_file(base: Path, relative: str, pattern: re.Pattern[str], replacement: str,
                  revision: str, expected_count: int, cancel: Event | None) -> _PreparedFile:
    candidate = _candidate(base, relative)
    temporary: Path | None = None
    try:
        replacement_bytes = replacement.encode("utf-8")
        digest = hashlib.sha256()
        count = 0
        buffer = ""
        with _open_source(candidate) as source, tempfile.NamedTemporaryFile(mode="wb", prefix=".devwisp-replace-", suffix=".tmp", dir=candidate.parent, delete=False) as output:
            temporary = Path(output.name)
            original_mode = stat.S_IMODE(os.fstat(source.fileno()).st_mode)
            if source.peek(3).startswith(b"\xef\xbb\xbf"):
                output.write(b"\xef\xbb\xbf")
            for chunk in chain(_text_chunks(source, digest, cancel, normalize=False), [None]):
                final = chunk is None
                buffer += chunk or ""
                cutoff = len(buffer) if final else max(0, len(buffer) - len(pattern.pattern))
                cursor = 0
                for match in pattern.finditer(buffer):
                    if match.start() >= cutoff:
                        break
                    if count % 256 == 0:
                        _check_cancel(cancel)
                    output.write(buffer[cursor:match.start()].encode("utf-8"))
                    output.write(replacement_bytes)
                    cursor = match.end()
                    count += 1
                consumed = max(cursor, cutoff)
                output.write(buffer[cursor:consumed].encode("utf-8"))
                buffer = buffer[consumed:]
            output.flush()
            os.fsync(output.fileno())
        if not hmac.compare_digest(digest.hexdigest(), revision):
            raise HTTPException(409, f"文件已变化，请重新查找：{relative}")
        if count != expected_count:
            raise HTTPException(409, f"匹配数量已变化，请重新查找：{relative}")
        os.chmod(temporary, original_mode)
        prepared = _PreparedFile(relative, temporary, revision, count)
        temporary = None
        return prepared
    except (UnicodeError, _UnsupportedText) as error:
        raise HTTPException(422, "替换内容或文件不是有效的 UTF-8 文本。") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cleanup(prepared: list[_PreparedFile]) -> None:
    for entry in prepared:
        entry.temporary.unlink(missing_ok=True)


def _save_prepared_replacements(base: Path, prepared: list[_PreparedFile], scanner: _WorkspaceTextScanner) -> dict[str, Any]:
    _check_cancel(scanner.cancel)
    result: dict[str, Any] = {"paths": [], "matchCount": 0, "errors": []}
    # Once committing starts, finish the prepared operation even if its client disconnects.
    # Each target is checked again immediately before its atomic replacement.
    for index, entry in enumerate(prepared):
        try:
            candidate = _candidate(base, entry.path)
            digest = hashlib.sha256()
            with _open_source(candidate) as source:
                for chunk in iter(lambda: source.read(TEXT_CHUNK_SIZE), b""):
                    digest.update(chunk)
            if not hmac.compare_digest(digest.hexdigest(), entry.revision):
                raise HTTPException(409, f"文件已变化，请重新查找：{entry.path}")
            os.replace(entry.temporary, candidate)
            result["paths"].append(entry.path)
            result["matchCount"] += entry.count
        except (HTTPException, OSError) as error:
            result["errors"].append({"path": entry.path, "message": str(error.detail) if isinstance(error, HTTPException) else "无法保存该文件"})
        scanner.report("write", processedFiles=index + 1, totalFiles=len(prepared))
    scanner.report("write", force=True, processedFiles=len(prepared), totalFiles=len(prepared))
    return result


def replace_workspace(base: Path, query: str, replacement: str, match_case: bool, files: list[dict[str, Any]], *, progress: Progress | None = None, cancel: Event | None = None) -> dict[str, Any]:
    base = base.resolve()
    pattern = _search_pattern(query, match_case)
    _validate_replacement(replacement)
    if not files or len({entry["path"] for entry in files}) != len(files):
        raise HTTPException(400, "替换文件列表无效，请重新查找。")
    scanner = _WorkspaceTextScanner(base, progress, cancel)
    prepared: list[_PreparedFile] = []
    try:
        for entry in files:
            prepared.append(_prepare_file(base, entry["path"], pattern, replacement, entry["revision"], entry["matchCount"], cancel))
            scanner.scanned_files += 1
            scanner.file_count += 1
            scanner.match_count += entry["matchCount"]
            scanner.report("prepare")
        return _save_prepared_replacements(base, prepared, scanner)
    finally:
        _cleanup(prepared)


def replace_workspace_all(base: Path, query: str, replacement: str, match_case: bool, *, progress: Progress | None = None, cancel: Event | None = None) -> dict[str, Any]:
    base = base.resolve()
    pattern = _search_pattern(query, match_case)
    _validate_replacement(replacement)
    scanner = _WorkspaceTextScanner(base, progress, cancel)
    prepared: list[_PreparedFile] = []
    try:
        for relative in scanner.paths():
            current = scanner.scan(relative, pattern, 0)
            if current is not None and current["matchCount"]:
                prepared.append(_prepare_file(base, relative, pattern, replacement, current["revision"], current["matchCount"], cancel))
                scanner.report("prepare")
        return _save_prepared_replacements(base, prepared, scanner)
    finally:
        _cleanup(prepared)
