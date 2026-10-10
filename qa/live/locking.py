"""Serialize timer and manual live sessions; do not wait behind paid runs."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from qa.live.types import Pending


@contextmanager
def live_run_lock(path: Path) -> Iterator[None]:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Pending("another live QA session holds the shared timer/manual lock") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
