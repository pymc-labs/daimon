"""`enable_channel_files`: the agent asks for the Teams Enable files card.

The card's sign-in grants daimon the SharePoint site of a channel's files. Its
link is signed by, and returns to, the Teams adapter, which this server cannot
reach, so the adapter posts the card after the turn once it sees this tool's
successful call (`daimon.core.teams_sharepoint.ENABLE_FILES_TOOL`). The tool
only checks the caller and tells the agent what follows. Admin-gated inside
rather than tagged `admin`, so a member who asks hears who can do it.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.teams_sharepoint import ENABLE_FILES_TOOL
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


def register_enable_files_tools(mcp: FastMCP) -> None:
    @mcp.tool(name=ENABLE_FILES_TOOL, tags={"teams"})  # pyright: ignore[reportArgumentType]
    async def enable_channel_files(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        """Post the Enable files card in this Teams channel, right after your reply.

        Call it only in a Teams channel whose ``<channel>`` element has a
        ``files_hint`` naming this tool, when an admin asks for a file there or
        to turn files on, before offering anything else (an artifact, report
        or notebook). Elsewhere no card can follow. A Microsoft 365 admin who
        is a member of the channel signs in with the card once; daimon can then
        save and read files in that channel, private and shared channels
        included. Admins only.
        """
        return enable_channel_files_impl(await _auth(ctx))


def enable_channel_files_impl(auth: AuthIdentity) -> str:
    if auth.platform != "teams":
        raise ToolError("Only Teams channels have files to turn on; nothing was done.")
    _require_admin(auth)
    return (
        "The Enable files card will be posted in this conversation right after your reply. "
        "Tell them an admin who is a member of this channel signs in with it, then to ask "
        "again for what they wanted. Do not name this tool."
    )
