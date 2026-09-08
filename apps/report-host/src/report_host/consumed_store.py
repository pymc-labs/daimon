"""Durable registry of burned single-use capability-token jtis.

`PUT /publish/{capability_token}` is authenticated by a capability token
whose `jti` must be usable exactly once. Recording that in memory (a
process-local set) means a token captured inside its TTL survives a host
restart and can be replayed — the burned set vanished with the process that
burned it. This file is the fix: burned jtis persist to disk, keyed to the
token's own `exp` so the set can be pruned instead of growing for the life
of a host designed to run indefinitely.

Mirrors `notebook_host.consumed_store` byte-for-byte in shape (the two apps
do not share code — report-host is a standalone app with no dependency on
notebook-host or daimon-core). Pure functions only — the single I/O is the
file read/write (no clock, no process control, no network); `burn_jti` and
`prune_consumed` take `now` as an explicit parameter rather than reading the
clock. A malformed file is treated as empty rather than fatal, and writes
are atomic (tmp + rename).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_REGISTRY_MODE = 0o600
"""Explicit mode for consumed.json — host-owned process state, not meant to
be readable by anything else on the volume."""


def load_consumed(path: Path) -> dict[str, int]:
    """Read the registry as {jti: exp}. Returns an empty dict if missing or malformed.

    A malformed file means a previous instance died mid-write. We can't trust
    partial state, so we forget it and let the next burn rebuild it.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for jti_obj, exp_obj in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(jti_obj, str) or not isinstance(exp_obj, int):
            continue
        out[jti_obj] = exp_obj
    return out


def save_consumed(path: Path, records: dict[str, int]) -> None:
    """Atomically rewrite the registry (tmp + rename on the same filesystem).

    The tmp file is locked to `_REGISTRY_MODE` (0600) before the rename, not
    after — no window where a freshly written registry is world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(records, indent=2))
    os.chmod(tmp, _REGISTRY_MODE)
    os.replace(tmp, path)


def prune_consumed(records: dict[str, int], *, now: int) -> dict[str, int]:
    """Return a new dict without entries whose `exp` has passed.

    Pure — takes `now` explicitly rather than reading the clock. Does not
    mutate `records`.
    """
    return {jti: exp for jti, exp in records.items() if exp > now}


def is_consumed(path: Path, jti: str) -> bool:
    """Whether `jti` has already been burned."""
    return jti in load_consumed(path)


def burn_jti(path: Path, jti: str, *, exp: int, now: int) -> bool:
    """Atomically check-and-burn `jti`. Returns True if this call burned it.

    Loads, prunes expired entries (bounding the file's steady-state size),
    then inserts `jti` with `exp` if it wasn't already present. One call does
    the check and the write, so there is no window between them — the caller
    must place this before reading the request body so two concurrent
    replays of the same token cannot both observe it as unused.
    """
    records = prune_consumed(load_consumed(path), now=now)
    already_burned = jti in records
    if not already_burned:
        records[jti] = exp
    save_consumed(path, records)
    return not already_burned
