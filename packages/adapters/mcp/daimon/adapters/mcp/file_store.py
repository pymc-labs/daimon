"""Disk-backed file store for media-tool outputs.

Handles are server-minted so two agents picking the same title cannot
collide. The agent threads the opaque handle id through ``send_message``;
the agent-supplied title survives as the user-visible display filename.

Single-process store: if the ``mcp`` adapter ever scales beyond one
instance, swap this module for a signed-URL backend.
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_META_SUFFIX = ".meta.json"
_TMP_SUFFIX = ".tmp"

# Default extension when mimetypes.guess_extension can't decide.
_FALLBACK_EXT = ".bin"


@dataclass(frozen=True)
class Handle:
    """Result of FileStore.put — what the tool returns to the agent.

    ``id`` is the only field the agent needs to carry forward; the rest
    is for surfacing to the user (display filename, expiry) in the same
    tool-result message.
    """

    id: str
    title: str
    mime_type: str
    created_at: datetime


@dataclass(frozen=True)
class FileHandle:
    """Result of FileStore.get — what the consumer (e.g. Discord) sees."""

    id: str
    data: bytes
    title: str
    display_filename: str
    content_type: str
    created_at: datetime


MAX_FILE_SIZE = 24_000_000  # 24 MB — Discord upload limit
MAX_TOTAL_SIZE = 100_000_000  # 100 MB
TTL_SECONDS = 600  # 10 minutes

_TITLE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _extension_for_mime(mime_type: str) -> str:
    # Handle ids must include an extension so Discord picks the right preview.
    ext = mimetypes.guess_extension(mime_type, strict=False)
    if ext in (None, ""):
        if mime_type == "image/jpeg":
            return ".jpg"
        return _FALLBACK_EXT
    if ext == ".jpe":
        return ".jpg"
    return ext


def _sanitize_title(title: str) -> str:
    cleaned = _TITLE_SLUG.sub("_", title).strip("._-")
    return cleaned or "file"


def _mint_id(mime_type: str) -> str:
    return f"{secrets.token_urlsafe(8)}{_extension_for_mime(mime_type)}"


class FileStore:
    """Disk-backed key-value store for file handles."""

    def __init__(
        self,
        *,
        base_dir: Path,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._cleanup_expired()

    @staticmethod
    def _validate_id(handle_id: str) -> None:
        if "/" in handle_id or "\\" in handle_id or ".." in handle_id:
            msg = f"Handle id {handle_id!r} contains invalid character (/, \\, or ..)"
            raise ValueError(msg)

    def _data_path(self, handle_id: str) -> Path:
        return self._base_dir / handle_id

    def _meta_path(self, handle_id: str) -> Path:
        return self._base_dir / f"{handle_id}{_META_SUFFIX}"

    def _tmp_path(self, handle_id: str) -> Path:
        return self._base_dir / f"{handle_id}{_TMP_SUFFIX}"

    def _write_meta(
        self,
        handle_id: str,
        *,
        content_type: str,
        title: str,
        display_filename: str,
        created_at: datetime,
    ) -> None:
        meta = {
            "content_type": content_type,
            "title": title,
            "display_filename": display_filename,
            "created_at": created_at.isoformat(),
        }
        self._meta_path(handle_id).write_text(json.dumps(meta))

    def _read_meta(self, handle_id: str) -> tuple[str, str, str, datetime]:
        try:
            raw = json.loads(self._meta_path(handle_id).read_text())
            return (
                raw["content_type"],
                raw.get("title", handle_id),
                raw.get("display_filename", handle_id),
                datetime.fromisoformat(raw["created_at"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            self._delete_pair(handle_id)
            raise KeyError(handle_id) from None

    def _delete_pair(self, handle_id: str) -> None:
        self._data_path(handle_id).unlink(missing_ok=True)
        self._meta_path(handle_id).unlink(missing_ok=True)

    def _list_data_files(self) -> list[str]:
        if not self._base_dir.exists():
            return []
        return [
            entry.name
            for entry in self._base_dir.iterdir()
            if entry.is_file()
            and not entry.name.endswith(_META_SUFFIX)
            and not entry.name.endswith(_TMP_SUFFIX)
        ]

    def _total_size(self) -> int:
        return sum(
            self._data_path(name).stat().st_size
            for name in self._list_data_files()
            if self._data_path(name).exists()
        )

    def _cleanup_expired(self) -> None:
        now = self._now()
        for entry in self._base_dir.iterdir():
            if entry.name.endswith(_TMP_SUFFIX):
                entry.unlink(missing_ok=True)
            elif entry.name.endswith(_META_SUFFIX):
                data_name = entry.name.removesuffix(_META_SUFFIX)
                if not self._data_path(data_name).exists():
                    entry.unlink(missing_ok=True)
        for name in self._list_data_files():
            meta_path = self._meta_path(name)
            if not meta_path.exists():
                self._data_path(name).unlink(missing_ok=True)
                continue
            try:
                raw = json.loads(meta_path.read_text())
                created_at = datetime.fromisoformat(raw["created_at"])
            except (json.JSONDecodeError, KeyError, ValueError):
                self._delete_pair(name)
                continue
            if (now - created_at).total_seconds() > TTL_SECONDS:
                self._delete_pair(name)

    def put(self, *, data: bytes, mime_type: str, title: str) -> Handle:
        """Store ``data`` under a server-minted handle id; return the Handle.

        ``title`` is preserved as metadata and shaped into a display filename
        for downstream consumers. The handle id is what the agent must thread
        through ``send_message`` — two agents passing the same title get
        distinct handles.
        """
        self._cleanup_expired()

        if len(data) > MAX_FILE_SIZE:
            msg = (
                f"File ({len(data) / 1_000_000:.1f} MB) exceeds the "
                f"{MAX_FILE_SIZE / 1_000_000:.0f} MB per-file limit."
            )
            raise ValueError(msg)

        new_total = self._total_size() + len(data)
        if new_total > MAX_TOTAL_SIZE:
            msg = (
                f"Storing this file ({len(data) / 1_000_000:.1f} MB) would exceed "
                f"the {MAX_TOTAL_SIZE / 1_000_000:.0f} MB total store limit. "
                f"Current usage: {self._total_size() / 1_000_000:.1f} MB."
            )
            raise ValueError(msg)

        handle_id = _mint_id(mime_type)
        # Collision is astronomically unlikely with token_urlsafe(8), but
        # the contract is "server-minted, no collisions" — re-roll if the
        # path already exists.
        while self._data_path(handle_id).exists():
            handle_id = _mint_id(mime_type)

        ext = _extension_for_mime(mime_type)
        display_filename = f"{_sanitize_title(title)}{ext}"
        created_at = self._now()

        tmp_path = self._tmp_path(handle_id)
        tmp_path.write_bytes(data)
        tmp_path.rename(self._data_path(handle_id))
        self._write_meta(
            handle_id,
            content_type=mime_type,
            title=title,
            display_filename=display_filename,
            created_at=created_at,
        )
        return Handle(
            id=handle_id,
            title=title,
            mime_type=mime_type,
            created_at=created_at,
        )

    def get(self, handle_id: str) -> FileHandle:
        self._validate_id(handle_id)
        self._cleanup_expired()
        data_path = self._data_path(handle_id)
        if not data_path.exists() or not self._meta_path(handle_id).exists():
            raise KeyError(handle_id)
        content_type, title, display_filename, created_at = self._read_meta(handle_id)
        return FileHandle(
            id=handle_id,
            data=data_path.read_bytes(),
            title=title,
            display_filename=display_filename,
            content_type=content_type,
            created_at=created_at,
        )

    def delete(self, handle_id: str) -> None:
        self._validate_id(handle_id)
        self._delete_pair(handle_id)

    def list_available(self) -> list[str]:
        self._cleanup_expired()
        return [name for name in self._list_data_files() if self._meta_path(name).exists()]
