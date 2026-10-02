"""Expire stale open wizard forms.

An open `wizard_session` row keeps a Discord or Teams message's buttons
dispatchable — a tap on it can still write answers or claim a submit. The
row's `expires_at` is derived at post time from the CDN signature expiry of
the images the form carries (`daimon.core.wizard.expiry.derive_expires_at`),
so a form's buttons and its pictures are meant to die together. Once
`expires_at` has passed, the form must stop being tappable even though
nobody interacted with it.

This sweeper flips those past-deadline open rows to `abandoned` rather than
deleting them: the row stays around for the tap handler to give the "this
form has expired" reply instead of silently doing nothing on an unknown
short_id. Erasure — not this sweep — is what actually removes the row
(`daimon.core.purge`); this sweep only bounds how long a stale, dispatchable
form can remain.

Shell-only: opens one session, delegates the state transition to the store's
single predicated UPDATE, commits, and logs the count. No decisions live
here and no try/except — the scheduler's call site owns the boundary catch,
exactly as it does for the existing sweepers (`pending_file_sweeper.py`,
`mcp_credential_sweep.py`). `now` is injected, never read from a clock inside
this module.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from daimon.core.stores.wizard_session import abandon_expired_wizard_sessions
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


async def sweep_expired_wizard_sessions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    limit: int = 500,
) -> int:
    """Flip up to `limit` open, past-expiry wizard_session rows to abandoned.

    Returns the number of rows flipped.
    """
    async with session_factory() as session, session.begin():
        count = await abandon_expired_wizard_sessions(session, now=now, limit=limit)
    _log.info("wizard_sweep.expired", count=count)
    return count
