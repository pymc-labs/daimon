"""Resolve ``send_message`` file handles into bytes, for either platform.

A handle names a row staged in Postgres by ``create_file_upload_url`` and
filled by a direct PUT from the agent's sandbox. Postgres rather than
instance storage because the PUT and this read are separate HTTP requests
and mcp runs several instances with no session affinity.

The error strings surface the rejected handle so an agent can fix the call
without inspecting structured tool output.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.stores.file_uploads import get_upload
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_ATTACHMENTS_PER_MESSAGE: int = 10


@dataclass(frozen=True, slots=True)
class ResolvedUpload:
    filename: str
    content: bytes


async def resolve_file_handles(
    handles: list[str],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> list[ResolvedUpload]:
    if len(handles) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ToolError(f"max {MAX_ATTACHMENTS_PER_MESSAGE} attachments per message")
    resolved: list[ResolvedUpload] = []
    for name in handles:
        async with session_factory() as session:
            upload = await get_upload(session, tenant_id=tenant_id, handle_id=name)
        if upload is None:
            raise ToolError(
                f"file handle {name!r} not found — mint one with create_file_upload_url, "
                f"or check the handle id"
            )
        if upload.content is None:
            raise ToolError(
                f"file handle {name!r} has no bytes yet — PUT the file to its "
                f"upload_url before posting it"
            )
        resolved.append(ResolvedUpload(filename=upload.display_filename, content=upload.content))
    return resolved
