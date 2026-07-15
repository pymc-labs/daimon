"""Assemble + deliver per-agent secrets as a mounted `.env`.

Pure logic lives in `assemble_env_bytes` (rows → KEY=VALUE bytes); the shell
`upload_env_and_mount` wires the tenant-scoped row fetch + SDK Files-API upload
+ TTL-delete enqueue together. Called from `sessions.py` and `headless_runner.py`
at session-create time.

Tenant isolation lives here: assembly reads ONLY the agent's own
(tenant_id, agent_id) rows. Secret values never reach logs — log file_id and
key_count only.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from anthropic.types.beta.beta_managed_agents_file_resource_params import (
    BetaManagedAgentsFileResourceParams,
)
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.domain import AgentFileRow
from daimon.core.stores.pending_file_deletes import enqueue_pending_file_delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)
_MOUNT_PATH = ".env"


def assemble_env_bytes(rows: list[AgentFileRow]) -> bytes:
    """Pure: build .env byte content from secret rows.

    Returns empty bytes for empty rows (caller decides whether to skip upload).
    Each row becomes a `KEY=VALUE` line; the blob has a trailing newline.
    """
    lines = [f"{row.key}={row.content}" for row in rows]
    return ("\n".join(lines) + "\n").encode() if lines else b""


async def upload_env_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    ttl_hours: int = 1,
) -> BetaManagedAgentsFileResourceParams | None:
    """Fetch the agent's tenant-scoped secrets, upload them as a `.env`, mount it.

    Reads ONLY the (tenant_id, agent_id) rows (tenant isolation), assembles the
    `.env`, uploads via the Files API, and enqueues the uploaded object for TTL
    deletion (default 1h) — the DB row is the durable copy, the Files object is
    disposable per session. Returns the session-resource dict to pass as
    `resources=[result]`, or None when the agent has no secrets (caller skips
    resources entirely).

    The requested mount_path is ".env"; MA serves it at
    `/mnt/session/uploads/.env` (skills read from there).
    """
    async with session_factory() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    if not rows:
        return None

    content = assemble_env_bytes(rows)
    uploaded: FileMetadata = await anthropic.beta.files.upload(
        file=(".env", io.BytesIO(content), "text/plain"),
    )

    delete_after = dt.datetime.now(dt.UTC) + dt.timedelta(hours=ttl_hours)
    async with session_factory() as session, session.begin():
        await enqueue_pending_file_delete(session, file_id=uploaded.id, delete_after=delete_after)

    _log.info("credential_env.mounted", file_id=uploaded.id, key_count=len(rows))
    return {"type": "file", "file_id": uploaded.id, "mount_path": _MOUNT_PATH}
