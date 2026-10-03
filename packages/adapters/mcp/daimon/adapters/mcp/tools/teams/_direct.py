"""A Teams direct message: a 1:1 chat with someone who shares a team with the caller and daimon."""

from __future__ import annotations

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient, TeamsMember
from daimon.adapters.mcp.tools.teams._directory import installed_teams, require_client
from fastmcp.exceptions import ToolError

_OTHER_ORGANISATION = (
    "the recipient is from another organisation, and Teams can't open a 1:1 chat across "
    "organisations, so nothing was sent; reach them in a channel you share instead"
)


async def _shared_member(
    runtime: McpRuntime, auth: AuthIdentity, client: TeamsBotClient, caller: str, recipient: str
) -> TeamsMember:
    """The recipient's roster entry in a team daimon is in that the caller is in too."""
    for team in await installed_teams(runtime, auth):
        try:
            member = await client.get_member(team.team_id, recipient)
            if member is None or not member.id or member.user_role == "anonymous":
                continue
            if (member.tenant_id or client.tenant_id).lower() != client.tenant_id.lower():
                raise ToolError(_OTHER_ORGANISATION)
            if await client.get_member(team.team_id, caller) is not None:
                return member
        except (httpx.HTTPError, ValueError):
            continue
    raise ToolError(
        "sender and recipient must both be members of a team daimon is in; "
        "recipient_id is the person's Entra object id"
    )


async def teams_direct_message(
    runtime: McpRuntime, auth: AuthIdentity, *, recipient_id: str, chunks: list[str]
) -> tuple[str, list[str]]:
    """(1:1 chat id, message ids). A partial failure says how many were sent."""
    client, caller = require_client(runtime, auth)
    member = await _shared_member(runtime, auth, client, caller, recipient_id)
    ids: list[str] = []
    try:
        chat = await client.open_personal_chat(member.id)
        for chunk in chunks:
            ids.append(await client.send(chat, chunk))
    except httpx.HTTPStatusError as exc:
        reason = f"HTTP {exc.response.status_code}"
        if exc.response.status_code == 403:
            reason = "Teams refused; the recipient may have blocked or removed daimon"
        raise ToolError(f"Teams DM failed after {len(ids)} message(s): {reason}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError(
            f"Teams DM failed after {len(ids)} message(s): {type(exc).__name__}"
        ) from exc
    return chat, ids
