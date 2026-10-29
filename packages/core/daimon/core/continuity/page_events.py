"""Page hosts sign page events after ACL/CSRF checks; signing keys never reach browsers."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

MAX_EVENT_BYTES = 4096
FRESHNESS_S = 300
KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


@dataclass(frozen=True)
class SignedPageEvent:
    watch_id: uuid.UUID
    page_id: uuid.UUID
    key_id: str
    timestamp: int
    event_id: uuid.UUID
    body: bytes
    signature: str

    def headers(self) -> dict[str, str]:
        return {
            "X-Page-ID": str(self.page_id),
            "X-Watch-Key-ID": self.key_id,
            "X-Timestamp": str(self.timestamp),
            "X-Event-ID": str(self.event_id),
            "X-Signature": self.signature,
        }


def signature_for(
    *,
    key: str,
    watch_id: uuid.UUID,
    page_id: uuid.UUID,
    key_id: str,
    timestamp: int,
    event_id: uuid.UUID,
    body: bytes,
) -> str:
    if not 32 <= len(key.encode()) <= 256 or not KEY_ID.fullmatch(key_id):
        raise ValueError("Invalid page signing key or key identity")
    canonical = "\n".join(
        (
            "daimon-page-v1",
            str(watch_id),
            str(page_id),
            key_id,
            str(timestamp),
            str(event_id),
            hashlib.sha256(body).hexdigest(),
        )
    ).encode()
    return hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()


def sign_page_event(
    *,
    key: str,
    watch_id: uuid.UUID,
    page_id: uuid.UUID,
    key_id: str,
    body: bytes,
    now: datetime,
    event_id: uuid.UUID | None = None,
) -> SignedPageEvent:
    """Sign an authorized server-side event using the host's registered binding."""
    if not 0 < len(body) <= MAX_EVENT_BYTES:
        raise ValueError("Page event exceeds evidence bounds")
    stamp, event = int(now.timestamp()), event_id or uuid.uuid4()
    signature = signature_for(
        key=key,
        watch_id=watch_id,
        page_id=page_id,
        key_id=key_id,
        timestamp=stamp,
        event_id=event,
        body=body,
    )
    return SignedPageEvent(watch_id, page_id, key_id, stamp, event, body, signature)


def verify_page_event(event: SignedPageEvent, *, key: str, now: datetime) -> bool:
    if (
        not 0 < len(event.body) <= MAX_EVENT_BYTES
        or abs(now.timestamp() - event.timestamp) > FRESHNESS_S
    ):
        return False
    if re.fullmatch(r"[0-9a-f]{64}", event.signature) is None:
        return False
    return hmac.compare_digest(
        event.signature,
        signature_for(
            key=key,
            watch_id=event.watch_id,
            page_id=event.page_id,
            key_id=event.key_id,
            timestamp=event.timestamp,
            event_id=event.event_id,
            body=event.body,
        ),
    )
