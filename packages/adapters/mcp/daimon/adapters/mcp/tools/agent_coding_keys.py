"""List and revoke an agent's coding keys: all for an admin, the caller's own for its owner."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.agents import (
    _edit_actor,  # pyright: ignore[reportPrivateUsage]
    _edit_deps,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import coding_keys
from daimon.core.agent_editing import AgentEditError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class AgentCodingKey:
    key_id: str
    creator: str
    created_at: datetime
    expires_at: datetime | None
    mine: bool


@dataclass(frozen=True)
class AgentCodingKeys:
    agent_id: str
    keys: list[AgentCodingKey]


@dataclass(frozen=True)
class CodingKeyRevoked:
    agent_id: str
    key_id: str


async def _list_impl(runtime: McpRuntime, auth: AuthIdentity, agent_id: str) -> AgentCodingKeys:
    try:
        found = await coding_keys.list_agent_coding_keys(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            ma_agent_id=agent_id,
            now=datetime.now(UTC),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return AgentCodingKeys(
        agent_id=agent_id,
        keys=[
            AgentCodingKey(str(k.jti), k.creator, k.created_at, k.expires_at, k.mine) for k in found
        ],
    )


async def _revoke_impl(
    runtime: McpRuntime, auth: AuthIdentity, agent_id: str, key_id: uuid.UUID
) -> CodingKeyRevoked:
    try:
        await coding_keys.revoke_agent_coding_key(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            ma_agent_id=agent_id,
            jti=key_id,
            now=datetime.now(UTC),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return CodingKeyRevoked(agent_id=agent_id, key_id=str(key_id))


def register_agent_coding_key_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_agent_coding_keys(ctx: Context, agent_id: str) -> AgentCodingKeys:  # pyright: ignore[reportUnusedFunction]
        """List an agent's live coding keys, never their tokens: all for an admin, the caller's
        own for the agent's owner."""
        return await _list_impl(runtime, await _auth(ctx), agent_id)

    @mcp.tool
    async def revoke_agent_coding_key(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, agent_id: str, key_id: uuid.UUID
    ) -> CodingKeyRevoked:
        """Revoke an agent coding key: any for an admin, the caller's own for its owner."""
        return await _revoke_impl(runtime, await _auth(ctx), agent_id, key_id)


__all__ = ["register_agent_coding_key_tools"]
