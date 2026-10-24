"""One bounded storage operation; blocked network I/O cannot hold process exit."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future
from typing import ParamSpec, TypeVar, cast

_P = ParamSpec("_P")
_T = TypeVar("_T")


class StorageWorker(Executor):
    def __init__(self, name: str) -> None:
        self.name = name
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.active: Future[object] | None = None
        self.closed = False

    @property
    def busy(self) -> bool:
        with self.lock:
            return self.active is not None and not self.active.done()

    def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
        with self.lock:
            if self.closed or (self.active is not None and not self.active.done()):
                raise RuntimeError("storage worker is unavailable")
            future: Future[_T] = Future()

            def run() -> None:
                if future.set_running_or_notify_cancel():
                    try:
                        result = fn(*args, **kwargs)
                    except BaseException as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(result)

            # Storage has no journal access. Its only in-flight snapshot remains
            # owned by upload(), which releases it when the call unwinds. Daemon
            # threads are deliberately not registered for an unbounded atexit join.
            self.active = cast(Future[object], future)
            self.thread = threading.Thread(target=run, name=self.name, daemon=True)
            self.thread.start()
            return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self.lock:
            self.closed = True
            thread = self.thread
            if cancel_futures and self.active is not None:
                self.active.cancel()
        if wait and thread is not None:
            thread.join()
