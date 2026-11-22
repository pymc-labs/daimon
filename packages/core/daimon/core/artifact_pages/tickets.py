"""Short-lived tickets the shell mints into its frame and source URLs.

A content frame has an opaque origin and so no session: the ticket says which page,
version and viewer the shell admitted. The content route re-admits from it on every
load (the person still admitted, or the link still current). Fernet gives the MAC,
the expiry and crypto key rotation.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.github_credentials import build_multifernet
from pydantic import BaseModel, ConfigDict, ValidationError

TicketPurpose = Literal["content", "source"]


class Ticket(BaseModel):
    model_config = ConfigDict(frozen=True)

    purpose: TicketPurpose
    artifact_id: uuid.UUID
    version: int
    person_id: uuid.UUID | None = None
    # A public page's link as admitted: replacing the link retires its tickets.
    link_digest: str | None = None


def fernet_of(crypto_keys: Sequence[str]) -> MultiFernet:
    return build_multifernet(tuple(crypto_keys))


def link_digest(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()[:32]


def seal(fernet: MultiFernet, ticket: Ticket, *, now: datetime) -> str:
    return fernet.encrypt_at_time(ticket.model_dump_json().encode(), int(now.timestamp())).decode()


def open_ticket(
    fernet: MultiFernet, token: str, *, purpose: TicketPurpose, ttl: timedelta, now: datetime
) -> Ticket | None:
    """The ticket, or None when it is forged, expired or minted for another purpose."""
    try:
        plain = fernet.decrypt_at_time(
            token.encode(), int(ttl.total_seconds()), int(now.timestamp())
        )
        ticket = Ticket.model_validate_json(plain)
    except (InvalidToken, ValidationError):
        return None
    return ticket if ticket.purpose == purpose else None
