"""Staged uploads named by send_message's `file_handles`, shared by every platform."""

from __future__ import annotations

import uuid

from daimon.core.stores.domain import FileUploadRow
from daimon.core.stores.file_uploads import get_upload
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def staged_uploads(
    handles: list[str],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> list[tuple[FileUploadRow, bytes]]:
    """Each handle's row and bytes, in order.

    Handles name rows staged in Postgres by ``create_file_upload_url``, which
    the agent's sandbox filled with a direct PUT. Postgres rather than instance
    storage because the PUT and this read are separate HTTP requests and mcp
    runs several instances with no session affinity.

    The error string must surface the rejected handle so an agent can fix the
    call without inspecting structured tool output.
    """
    staged: list[tuple[FileUploadRow, bytes]] = []
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
        staged.append((upload, upload.content))
    return staged
