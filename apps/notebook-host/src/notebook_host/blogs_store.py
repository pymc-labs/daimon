"""Durable registry of persistent blogs.

A "blog" is a marimo subprocess the host keeps alive forever (run mode). This
file records which slugs are blogs so the host can (a) respawn them at startup,
(b) exempt them from TTL reaping, and (c) self-heal a dead one in the sweep. It
lives on the same persistent volume as the notebook source files.

Pure functions only — the single I/O is the file read/write (no clock, no
process control, no network). Mirrors pids_store.py's posture: a malformed file
is treated as empty rather than fatal, and writes are atomic (tmp + rename).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel


class BlogRecord(BaseModel):
    slug: str
    created_at: float  # unix epoch seconds (matches NotebookProcess.started_at)
    title: str | None = None


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
    """Atomically rewrite the registry (tmp + rename on the same filesystem)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {slug: rec.model_dump() for slug, rec in records.items()}
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def register_blog(path: Path, record: BlogRecord) -> None:
    """Add or overwrite a blog's record, preserving all other entries."""
    records = load_blogs(path)
    records[record.slug] = record
    save_blogs(path, records)


def unregister_blog(path: Path, slug: str) -> None:
    """Drop a blog's record. A no-op if the slug isn't registered."""
    records = load_blogs(path)
    if records.pop(slug, None) is not None:
        save_blogs(path, records)
