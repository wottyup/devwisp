from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from threading import Event
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from .workspace_search import WorkspaceOperationCancelled

logger = logging.getLogger(__name__)


def workspace_operation_stream(operation: Callable[..., dict[str, Any]], *args: Any) -> StreamingResponse:
    async def events():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        cancelled = Event()

        def enqueue(message: dict[str, Any]) -> None:
            if cancelled.is_set():
                return
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

        def send(message: dict[str, Any]) -> None:
            if not cancelled.is_set():
                loop.call_soon_threadsafe(enqueue, message)

        def run() -> None:
            try:
                result = operation(*args, progress=lambda data: send({"type": "progress", **data}), cancel=cancelled)
                send({"type": "done", "result": result})
            except WorkspaceOperationCancelled:
                pass
            except HTTPException as error:
                send({"type": "error", "status": error.status_code, "detail": str(error.detail)})
            except OSError:
                send({"type": "error", "status": 403, "detail": "无法读写工作目录，请检查文件权限或磁盘空间。"})
            except Exception:
                logger.exception("Workspace search/replace failed")
                send({"type": "error", "status": 500, "detail": "目录查找或替换失败，请重试。"})

        worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            yield b'{"type":"progress","phase":"scan","scannedFiles":0,"skippedFiles":0,"fileCount":0,"matchCount":0}\n'
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=2)
                except TimeoutError:
                    yield b'{"type":"ping"}\n'
                    continue
                yield (json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
                if message["type"] in {"done", "error"}:
                    break
        finally:
            cancelled.set()
            # to_thread cooperatively stops scans/preflight on disconnect. The
            # commit phase deliberately completes and removes its staging files.
            worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        # Prevent GZipMiddleware from buffering tiny progress messages.
        "Content-Encoding": "identity",
    })
