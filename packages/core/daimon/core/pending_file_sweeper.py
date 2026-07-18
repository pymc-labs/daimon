"""Drain the ephemeral Files-API TTL queue.

Files-API objects are uploaded as the transport for an agent's assembled
`.env`; the durable copy lives in the `agent_files` DB row. Once mounted, the
uploaded object is disposable. `enqueue_pending_file_delete` records each
upload with a `delete_after`; this sweeper deletes the due objects and clears
their rows.

Shell-only: lists due rows, calls `beta.files.delete` per object, then drops
the row after a successful delete. A 404 (object already gone — e.g. MA's own
server-side TTL beat us to it) is treated as success and the row is still
removed. Any other `anthropic.APIError` propagates per the architecture
error-propagation guideline; the row is left in place for the next sweep.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.stores.pending_file_deletes import (
    delete_pending_file_delete,
    list_due_pending_file_deletes,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


async def sweep_pending_file_deletes(
    anthropic_client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: dt.datetime,
) -> list[str]:
    """Delete due Files-API objects and clear their queue rows.

    Returns the file_ids successfully swept (deleted-or-already-gone). A
    non-404 `anthropic.APIError` propagates; the failing row stays queued so
    the next sweep retries it.
    """
    async with session_factory() as session:
        due = await list_due_pending_file_deletes(session, now=now)

    swept: list[str] = []
    for row in due:
        # A 404 (object already gone — e.g. MA's server-side TTL beat us) is
        # treated as success; we still drop the row. Other APIError propagates.
        with contextlib.suppress(anthropic.NotFoundError):
            await anthropic_client.beta.files.delete(row.file_id)
        async with session_factory() as session, session.begin():
            await delete_pending_file_delete(session, file_id=row.file_id)
        swept.append(row.file_id)
        _log.info("pending_file_sweeper.deleted", file_id=row.file_id)

    return swept
