"""List coding keys: an agent's (all for an admin, the caller's own for its owner) or, with no
agent, the caller's own on any agent. Revoking is `remove_my_data`, on the person's card."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.agents import (
    _edit_actor,  # pyright: ignore[reportPrivateUsage]
    _edit_deps,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import coding_keys
from daimon.core.agent_editing import AgentEditError
from daimon.core.errors import DaimonError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_GONE = "No such key."


@dataclass(frozen=True)
class CodingKey:
    key_id: str
    agent_id: str | None
    creator: str | None
    created_at: datetime
    expires_at: datetime | None
    mine: bool


@dataclass(frozen=True)
class CodingKeys:
    keys: list[CodingKey]


@dataclass(frozen=True)
class CodingKeyRevoked:
    key_id: str


def _own(auth: AuthIdentity) -> None:
    if auth.is_external_agent or auth.hold is not None or auth.client:
        raise ToolError("This tool acts only on a person's own keys.")


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ToolError(_GONE) from None


async def _list_impl(runtime: McpRuntime, auth: AuthIdentity, agent_id: str | None) -> CodingKeys:
    if agent_id is None:
        _own(auth)
        try:
            async with runtime.session_factory() as session:
                mine = await coding_keys.list_my_coding_keys(
                    session,
                    tenant_id=auth.tenant_id,
                    account_id=auth.account_id,
                    now=datetime.now(UTC),
                )
        except DaimonError as err:
            raise ToolError(str(err)) from err
        return CodingKeys(
            [
                CodingKey(str(k.jti), k.agent_id, None, k.created_at, k.expires_at, True)
                for k in mine
            ]
        )
    try:
        found = await coding_keys.list_agent_coding_keys(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            ma_agent_id=agent_id,
            now=datetime.now(UTC),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return CodingKeys(
        [
            CodingKey(str(k.jti), agent_id, k.creator, k.created_at, k.expires_at, k.mine)
            for k in found
        ]
    )


async def revoke_coding_key(
    runtime: McpRuntime,
    auth: AuthIdentity,
    key_id: str,
    agent_id: str | None,
    origin_context_id: str | None = None,
) -> CodingKeyRevoked:
    """Revoke a key on the person's card: their own whatever `agent_id` says, else an admin's."""
    _own(auth)
    await require_card(
        runtime, auth, tool_name="remove_my_data", origin_context_id=origin_context_id
    )
    jti = _uuid(key_id)
    # A member passes the agent_id list_coding_keys returned for their own key.
    async with runtime.session_factory.begin() as session:
        done = await coding_keys.revoke_my_coding_key(
            session,
            jti=jti,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            now=datetime.now(UTC),
        )
    if done:
        return CodingKeyRevoked(key_id)
    if agent_id is None:
        raise ToolError(_GONE)
    try:
        await coding_keys.revoke_agent_coding_key(
            _edit_deps(runtime),
            actor=_edit_actor(auth),
            ma_agent_id=agent_id,
            jti=jti,
            now=datetime.now(UTC),
        )
    except AgentEditError as err:
        raise ToolError(str(err)) from err
    return CodingKeyRevoked(key_id)


def register_agent_coding_key_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_coding_keys(ctx: Context, agent_id: str | None = None) -> CodingKeys:  # pyright: ignore[reportUnusedFunction]
        """List live coding keys, never tokens. With agent_id: that agent's (all for an admin,
        else your own). Without: yours on any agent."""
        return await _list_impl(runtime, await _auth(ctx), agent_id)


__all__ = ["register_agent_coding_key_tools"]
