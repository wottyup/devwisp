"""A conversation-owned native process with callbacks scoped to its active turn."""
from __future__ import annotations

import threading
from typing import Any


class HarnessRuntime:
    def __init__(self, signature: str) -> None:
        self.signature = signature
        self.child_parents: dict[str, str] = {}
        self.context_window: int | None = None
        self.runtime: Any = None
        self.session: Any = None
        self.callbacks: tuple[Any, Any] | None = None
        self.closed = threading.Event()
        self._close_lock = threading.Lock()
        self._handler: threading.Thread | None = None

    def question(self, request: Any, cancelled: threading.Event) -> Any:
        callbacks = self.callbacks
        return callbacks[1](request, cancelled) if callbacks else None

    def start_requests(self) -> None:
        if self._handler:
            return
        def requests() -> None:
            try:
                while not self.closed.is_set():
                    request = self.runtime.client.next_request()
                    if self.closed.is_set():
                        return
                    if request.method != 'approval/request':
                        self.runtime.client.respond_error(request.id, code=-32601, message='Unsupported client request')
                        continue
                    callbacks = self.callbacks
                    allowed = callbacks[0](request) if callbacks else False
                    if not self.closed.is_set():
                        self.runtime.client.respond(request.id, {'outcome': 'allowed-once' if allowed else 'rejected'})
            except Exception:
                return  # Closing the client releases its blocking request reader.
        self._handler = threading.Thread(target=requests, daemon=True, name='devwisp-deepseek-approvals')
        self._handler.start()

    def healthy(self) -> bool:
        process = getattr(self.runtime.client, '_proc', None)
        return not self.closed.is_set() and process is not None and process.poll() is None

    def close(self) -> None:
        with self._close_lock:
            if self.closed.is_set():
                return
            self.closed.set()
            self.callbacks = None
            process = getattr(self.runtime.client, '_proc', None)
            try:
                self.runtime.close()
            finally:
                # SDK rc2 reaps the process but can leave its pipes open.
                if process is not None:
                    for name, reader in [('stdout', '_reader_thread'), ('stderr', '_stderr_thread')]:
                        thread = getattr(self.runtime.client, reader, None)
                        stream = getattr(process, name, None)
                        if stream is not None and (thread is None or not thread.is_alive()):
                            stream.close()
                if self._handler and self._handler is not threading.current_thread():
                    self._handler.join(timeout=2)
