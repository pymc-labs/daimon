"""Core-side opaque request-id source for log / Sentry correlation.

`daimon.core.scheduler` needs a fresh per-fire request id to bind into structlog
contextvars, but it cannot import the discord adapter's `generate_request_id`
(cross-adapter imports are forbidden, and that helper pulls in `python-ulid`,
which is not a core dependency). This module supplies a stdlib-only generator so
core can mint its own correlation ids with zero new dependency.

The discord adapter keeps its own ULID-based generator; the two are independent
opaque ids — both are only ever used as correlation tokens, never parsed.
"""

from __future__ import annotations

import uuid


def generate_request_id() -> str:
    """Return a fresh opaque request id (hex of a random UUID4)."""
    return uuid.uuid4().hex
