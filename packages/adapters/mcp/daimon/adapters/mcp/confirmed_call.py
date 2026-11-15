"""Whether this call is the one press-approved call its credential names.

Set by the host call gate (`middleware.host_call_gate`) only after it consumed the
approved record, so a tool's own card check accepts the press as its card.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daimon.adapters.mcp.auth.resolver import AuthIdentity

host_confirmed_call: ContextVar[uuid.UUID | None] = ContextVar("host_confirmed_call", default=None)


def card_satisfied(auth: AuthIdentity) -> bool:
    """True inside the one approved call; every card check asks this, never the claim alone."""
    confirmed = auth.confirmed_call_id
    return confirmed is not None and host_confirmed_call.get() == confirmed
