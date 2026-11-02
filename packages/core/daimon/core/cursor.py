"""Opaque keyset cursors: where the next page of a list starts, as `(timestamp, id)`.

Pure. A cursor survives rows being added or removed between pages, which an offset
does not, and a client only ever hands back what it was given.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime

type Cursor = tuple[datetime, uuid.UUID]

#: The most rows a list returns when the client asks for none in particular.
DEFAULT_PAGE = 100
#: The most rows a client may ask for in one page.
MAX_PAGE = 200


def encode_cursor(cursor: Cursor) -> str:
    at, key = cursor
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{key}".encode()).decode().rstrip("=")


def decode_cursor(text: str) -> Cursor:
    """The cursor `text` was made from. Raises `ValueError` for anything else."""
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()
        at, _, key = raw.partition("|")
        return datetime.fromisoformat(at), uuid.UUID(key)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError("not a cursor") from None
