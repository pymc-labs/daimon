"""Durable registry of read-only notebooks the host starts on demand.

Every read-only notebook is recorded here: blogs (``expires_at`` None, kept
forever) and scratch notebooks (kept until ``expires_at``). The host does not
keep their processes running. A visit starts one (``lazy_spawn``), and the
sweep stops it again once it has gone unvisited for the warm window, so a
notebook that lives for months does not hold a port and a kernel for months.
The registry is what lets a stopped notebook come back under the same link,
and it lives on the same persistent volume as the notebook source files. The
file keeps its historical name, ``blogs.json``, so existing hosts read their
blogs without a migration.

Pure functions only — the single I/O is the file read/write (no clock, no
process control, no network). Mirrors pids_store.py's posture: a malformed file
is treated as empty rather than fatal, and writes are atomic (tmp + rename).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from notebook_host.files import write_private_file

_REGISTRY_MODE = 0o600
"""Explicit mode for blogs.json. It lists every blog's slug and its marimo
access token (together, the blog's shareable link) — a default-umask 0644
would let any jailed process read it and open every other blog, now that
``data_dir`` is traversable (0711)."""


class BlogRecord(BaseModel):
    slug: str
    created_at: float  # unix epoch seconds (matches NotebookProcess.started_at)
    title: str | None = None
    # The blog's marimo session token, persisted so a respawned blog keeps
    # the link it was published under. None only for records written before
    # tokens existed; the respawn mints and records one.
    access_token: str | None = None
    # Unix epoch seconds after which the sweep deletes the notebook. None for a
    # blog, which is kept until someone deletes it.
    expires_at: float | None = None


def is_expired(record: BlogRecord, *, now: float) -> bool:
    """Whether the notebook has outlived its lifetime. A blog never does."""
    return record.expires_at is not None and now >= record.expires_at


def load_blogs(path: Path) -> dict[str, BlogRecord]:
    """Read the registry. Returns an empty dict if missing or malformed.

    A malformed file means a previous instance died mid-write. We can't trust
    partial state, so we forget it and let the next register rebuild — same
    posture as load_pids.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, BlogRecord] = {}
    for slug_obj, entry in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(slug_obj, str):
            continue
        try:
            out[slug_obj] = BlogRecord.model_validate(entry)
        except (ValueError, TypeError):
            continue
    return out


def save_blogs(path: Path, records: dict[str, BlogRecord]) -> None:
    """Atomically rewrite the registry (tmp + rename on the same filesystem).

    Written through ``files.write_private_file``: the tmp file is created at
    ``_REGISTRY_MODE`` (0600) with ``O_EXCL | O_NOFOLLOW``, so it is never
    readable by another uid, even for the moment before the rename.
    """
    payload = {slug: rec.model_dump() for slug, rec in records.items()}
    write_private_file(path, json.dumps(payload, indent=2).encode(), mode=_REGISTRY_MODE)


def register_blog(path: Path, record: BlogRecord) -> None:
    """Add or overwrite a blog's record, preserving all other entries."""
    records = load_blogs(path)
    records[record.slug] = record
    save_blogs(path, records)


def unregister_blog(path: Path, slug: str) -> bool:
    """Drop a blog's record; True if one was there. A no-op if not registered."""
    records = load_blogs(path)
    if records.pop(slug, None) is None:
        return False
    save_blogs(path, records)
    return True
