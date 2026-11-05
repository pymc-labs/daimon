"""The caller's own data: what Daimon holds for them, and letting it go.

Every tool acts only on the person making the call. An agent's own key is refused, and a
change runs only on the person's card in chat.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.agent_coding_keys import revoke_coding_key
from daimon.core import profile
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
class MyData:
    connections: list[MyConnection]
    memory: list[MyMemory]


@dataclass(frozen=True)
class MyDataChange:
    action: Literal["forgot_memory", "disconnected", "revoked_key"]
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
    except DaimonError as err:
        raise ToolError(str(err)) from err
    return MyData(
        connections=[
            MyConnection(c.kind, c.label, c.key, None if c.agent_id is None else str(c.agent_id))
            for c in connections
        ],
        memory=[MyMemory(str(m.agent_id), list(m.paths)) for m in memory],
    )


async def _forget_impl(
    runtime: McpRuntime, auth: AuthIdentity, agent_id: str, origin_context_id: str | None = None
) -> MyDataChange:
    _own(auth)
    await require_card(
        runtime, auth, tool_name="remove_my_data", origin_context_id=origin_context_id
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
        runtime, auth, tool_name="remove_my_data", origin_context_id=origin_context_id
    )
    subject = mcp_subject(auth)
    try:
        if kind == "mcp":
            if agent_id is None or not key:
                raise ToolError("A sign-in needs its agent_id and key, as listed.")
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


def register_my_data_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_my_data(ctx: Context) -> MyData:  # pyright: ignore[reportUnusedFunction]
        """List what is held for you: connected accounts and sign-ins, and personal memory per
        agent. Ids feed remove_my_data."""
        return await _list_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def remove_my_data(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        kind: Literal["memory", "microsoft", "slack", "mcp", "coding_key"],
        origin_context_id: str,
        agent_id: str = "",
        key: str = "",
    ) -> MyDataChange:
        """Remove your own, by ids from list_my_data or list_coding_keys: memory (agent_id), a
        connected account (its kind, as listed), an mcp sign-in (agent_id, key) or a
        coding_key (key; an admin adds agent_id for another person's)."""
        auth = await _auth(ctx)
        if kind == "memory":
            if not agent_id:
                raise ToolError("agent_id is needed.")
            return await _forget_impl(runtime, auth, agent_id, origin_context_id)
        if kind == "coding_key":
            if not key:
                raise ToolError("key is needed.")
            await revoke_coding_key(runtime, auth, key, agent_id or None, origin_context_id)
            return MyDataChange("revoked_key", 1)
        return await _disconnect_impl(
            runtime,
            auth,
            kind=kind,
            agent_id=agent_id or None,
            key=key or None,
            origin_context_id=origin_context_id,
        )


__all__ = ["register_my_data_tools"]
