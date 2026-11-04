"""The caller's own data: what Daimon holds for them, and letting it go.

Every tool acts only on the person making the call. An agent's own key is refused, and a
change runs only on the person's card in chat.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core import coding_keys, profile
from daimon.core.errors import DaimonError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_GONE = "Nothing like that is connected to this account."


@dataclass(frozen=True)
class MyConnection:
    kind: str
    label: str
    key: str
    agent_id: str | None


@dataclass(frozen=True)
class MyMemory:
    agent_id: str
    files: list[str]


@dataclass(frozen=True)
class MyCodingKey:
    key_id: str
    agent_id: str | None
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class MyData:
    connections: list[MyConnection]
    memory: list[MyMemory]
    coding_keys: list[MyCodingKey]


@dataclass(frozen=True)
class MyDataChange:
    action: Literal["forgot_memory", "disconnected", "revoked_coding_key"]
    removed: int


def _own(auth: AuthIdentity) -> None:
    if auth.is_external_agent or auth.hold is not None or auth.client:
        raise ToolError("This tool acts only on a person's own data.")


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ToolError(_GONE) from None


async def _list_impl(runtime: McpRuntime, auth: AuthIdentity) -> MyData:
    _own(auth)
    subject = mcp_subject(auth)
    try:
        async with runtime.session_factory() as session:
            connections = await profile.list_connections(
                session, subject=subject, tenant_id=auth.tenant_id, account_id=auth.account_id
            )
            memory = await profile.list_personal_memory(
                session,
                runtime.client,
                subject=subject,
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
            )
            keys = await coding_keys.list_my_coding_keys(
                session,
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                now=datetime.now(UTC),
            )
    except DaimonError as err:
        raise ToolError(str(err)) from err
    return MyData(
        connections=[
            MyConnection(c.kind, c.label, c.key, None if c.agent_id is None else str(c.agent_id))
            for c in connections
        ],
        memory=[MyMemory(str(m.agent_id), list(m.paths)) for m in memory],
        coding_keys=[MyCodingKey(str(k.jti), k.agent_id, k.created_at, k.expires_at) for k in keys],
    )


async def _forget_impl(
    runtime: McpRuntime, auth: AuthIdentity, agent_id: str, origin_context_id: str | None = None
) -> MyDataChange:
    _own(auth)
    await require_card(
        runtime, auth, tool_name="forget_my_memory", origin_context_id=origin_context_id
    )
    try:
        async with runtime.session_factory.begin() as session:
            removed = await profile.forget_personal_memory(
                session,
                runtime.client,
                subject=mcp_subject(auth),
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                agent_id=_uuid(agent_id),
            )
    except DaimonError as err:
        raise ToolError(str(err)) from err
    return MyDataChange("forgot_memory", removed)


async def _disconnect_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    kind: Literal["microsoft", "slack", "mcp"],
    agent_id: str | None,
    key: str | None,
    origin_context_id: str | None = None,
) -> MyDataChange:
    _own(auth)
    await require_card(
        runtime, auth, tool_name="disconnect_my_account", origin_context_id=origin_context_id
    )
    subject = mcp_subject(auth)
    try:
        if kind == "mcp":
            if agent_id is None or not key:
                raise ToolError("An MCP sign-in needs its agent_id and key, as listed.")
            gone = await profile.disconnect_mcp_sign_in(
                runtime.client,
                sessionmaker=runtime.session_factory,
                subject=subject,
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                agent_id=_uuid(agent_id),
                server_url=key,
            )
            count = int(gone)
        else:
            async with runtime.session_factory.begin() as session:
                count = await profile.disconnect_token(
                    session,
                    subject=subject,
                    tenant_id=auth.tenant_id,
                    account_id=auth.account_id,
                    kind=kind,
                )
    except DaimonError as err:
        raise ToolError(str(err)) from err
    if not count:
        raise ToolError(_GONE)
    return MyDataChange("disconnected", count)


async def _revoke_key_impl(
    runtime: McpRuntime, auth: AuthIdentity, key_id: str, origin_context_id: str | None = None
) -> MyDataChange:
    _own(auth)
    await require_card(
        runtime, auth, tool_name="revoke_my_coding_key", origin_context_id=origin_context_id
    )
    async with runtime.session_factory.begin() as session:
        done = await coding_keys.revoke_my_coding_key(
            session,
            jti=_uuid(key_id),
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            now=datetime.now(UTC),
        )
    if not done:
        raise ToolError(_GONE)
    return MyDataChange("revoked_coding_key", 1)


def register_my_data_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_my_data(ctx: Context) -> MyData:  # pyright: ignore[reportUnusedFunction]
        """List what is held for the caller: connected accounts and sign-ins, personal memory
        per agent, and their coding keys. Ids here feed the other my-data tools."""
        return await _list_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def forget_my_memory(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, agent_id: str, origin_context_id: str | None = None
    ) -> MyDataChange:
        """Delete the caller's personal memory with one agent, by the agent_id from list_my_data.

        Only the caller's own memory; ``removed`` counts the files that went. Runs on the
        person's Confirm card in chat; pass this active turn's origin_context_id.
        """
        return await _forget_impl(runtime, await _auth(ctx), agent_id, origin_context_id)

    @mcp.tool
    async def disconnect_my_account(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        kind: Literal["microsoft", "slack", "mcp"],
        agent_id: str | None = None,
        key: str | None = None,
        origin_context_id: str | None = None,
    ) -> MyDataChange:
        """Disconnect one of the caller's accounts: saved Microsoft 365 or Slack access, or an
        MCP sign-in (``agent_id`` and ``key`` as listed by list_my_data). Runs on the person's
        Confirm card in chat; pass this active turn's origin_context_id."""
        return await _disconnect_impl(
            runtime,
            await _auth(ctx),
            kind=kind,
            agent_id=agent_id,
            key=key,
            origin_context_id=origin_context_id,
        )

    @mcp.tool
    async def revoke_my_coding_key(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, key_id: str, origin_context_id: str | None = None
    ) -> MyDataChange:
        """Revoke one of the caller's own coding keys, by the key_id from list_my_data. Runs on
        the person's Confirm card in chat; pass this active turn's origin_context_id."""
        return await _revoke_key_impl(runtime, await _auth(ctx), key_id, origin_context_id)


__all__ = ["register_my_data_tools"]
