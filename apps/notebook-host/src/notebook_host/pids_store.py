"""Persistent record of running marimo subprocesses.

The host writes ``{slug: PidRecord}`` to a JSON file on every spawn /
kill so a restarted host can reap orphans the previous instance left
behind. ``spawn_marimo`` uses ``start_new_session=True``, which means
SIGKILL'ing the host doesn't take the children with it — without
this sweep, restarting the host leaves zombie marimo processes
holding ports from the pool until the OS reaps them or the machine
restarts.

Pure functions only — I/O is the file read/write, but no clock, no
process control, no network. ``main.py`` calls ``reap_orphans`` at
lifespan startup; admin.py calls ``save_pids`` after register/unregister.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class PidRecord(BaseModel):
    slug: str
    pid: int
    port: int
    started_at: float  # unix epoch seconds (matches NotebookProcess.started_at)


def load_pids(path: Path) -> dict[str, PidRecord]:
    """Read the pids file. Returns empty dict if missing or malformed.

    A malformed file means the previous instance died mid-write — we
    can't trust any of its claims, so the safest move is to forget
    everything and let the OS clean up. Operators who care can keep
    backups; we don't.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, PidRecord] = {}
    for slug_obj, entry in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(slug_obj, str):
            continue
        try:
            out[slug_obj] = PidRecord.model_validate(entry)
        except (ValueError, TypeError):
            continue
    return out


def save_pids(path: Path, records: dict[str, PidRecord]) -> None:
    """Atomically rewrite the pids file (tmp + rename).

    The rename is atomic on the same filesystem, so a host crash mid-write
    leaves either the old file or the new — never a truncated file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {slug: rec.model_dump() for slug, rec in records.items()}
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    """Return True if `pid` is a live process. kill(pid, 0) is the standard probe."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but we can't signal it (different user). Conservative:
        # treat as alive — we'll fall through to SIGTERM which will raise
        # again and the caller's contextlib.suppress will swallow it.
        return True
    return True


def reap_orphans(path: Path, *, term_wait_seconds: float = 5.0) -> list[PidRecord]:
    """Read pids file, SIGTERM each live entry (escalating to SIGKILL), clear file.

    Returns the list of records that were live at sweep time (caller may
    log them). Records whose PID is already dead are skipped silently.
    Called exactly once at host startup before any new spawn — leaves the
    pids file empty on exit so the next register starts from a clean slate.
    """
    records = load_pids(path)
    if not records:
        # Still ensure the file doesn't linger as stale on a clean boot
        # with no prior state. No-op if missing.
        return []

    reaped: list[PidRecord] = []
    for rec in records.values():
        if not _pid_alive(rec.pid):
            continue
        reaped.append(rec)
        try:
            os.kill(rec.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        deadline = time.monotonic() + term_wait_seconds
        while time.monotonic() < deadline and _pid_alive(rec.pid):
            time.sleep(0.1)
        if _pid_alive(rec.pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(rec.pid, signal.SIGKILL)

    save_pids(path, {})
    return reaped


def record_from_process(slug: str, pid: int, port: int, started_at: float | datetime) -> PidRecord:
    """Build a PidRecord from the values AdminState already has on hand."""
    ts = started_at.timestamp() if isinstance(started_at, datetime) else started_at
    return PidRecord(slug=slug, pid=pid, port=port, started_at=ts)
