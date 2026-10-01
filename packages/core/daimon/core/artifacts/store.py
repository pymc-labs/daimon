"""Vendor-neutral artifact storage boundary."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """A private object plus its time-bounded retrieval capability."""

    key: str
    url: str
    expires_at: dt.datetime


class ArtifactStore(Protocol):
    """Storage boundary used by hosted adapters."""

    async def upload_and_presign(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str,
        ttl_seconds: int,
    ) -> StoredArtifact:
        """Store one private object and mint a short-lived GET URL."""
        ...
