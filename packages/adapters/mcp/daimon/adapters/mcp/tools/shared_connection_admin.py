"""Chat tools for the shared connections page: list, and change on the asker's card.

The list answers directly. Each change runs only on the asker's Approve card
(`CARDED_TOOLS`), in any room or direct message, through the same
`daimon.core.shared_connection_admin` functions and checks as the web routes. A token never
passes through chat: it is added or replaced in the web app.
"""

from __future__ import annotations

import uuid
from typing import Literal

from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import now, web_admin_of
from daimon.core import shared_connection_admin as admin_ops
from daimon.core.shared_connection_admin import NO_CONNECTION, OFF, Connection, Connections
from daimon.core.shared_connections import SharedConnectionError
from daimon.core.stores import mcp_connections as store
from daimon.core.web_admin import WebAdmin
from daimon.core.web_roles_rules import is_admin
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

ConnectionAction = Literal["add", "refresh", "pause", "resume", "rescope", "remove"]
ToolAction = Literal["turn_on", "turn_off", "mark_writes", "mark_reads", "rescope"]
_NOT_ADMIN = "Only a workspace admin or owner manages shared connections."


def register_shared_connection_admin_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    sessions = runtime.session_factory

    async def manager(auth: AuthIdentity, *, on: bool = True) -> tuple[WebAdmin, bool]:
        """The caller as a workspace admin or owner; a change also needs the feature on."""
        actor = await web_admin_of(runtime, auth)
        if not is_admin(actor.role):
            raise ToolError(_NOT_ADMIN)
        found = await admin_ops.is_on(
            sessions, runtime.settings, auth.tenant_id, auth.platform or "web"
        )
        if on and not found:
            raise ToolError(OFF)
        return actor, found

    async def connection_id(actor: WebAdmin, ref: str) -> uuid.UUID:
        """A connection by its id or its name, as listed."""
        try:
            return uuid.UUID(ref.strip())
        except ValueError:
            async with sessions() as db:
                found = await store.get_connection_by_name(
                    db, tenant_id=actor.tenant_id, name=ref.strip()
                )
        if found is None:
            raise ToolError(NO_CONNECTION)
        return found.id

    def fernet() -> MultiFernet:
        if runtime.fernet is None:
            raise ToolError("Shared connections aren't set up here.")
        return runtime.fernet

    @mcp.tool(name="list_shared_connections", tags={"admin"})
    async def list_connections(ctx: Context) -> Connections:  # pyright: ignore[reportUnusedFunction]
        """List the workspace's shared connections: each server's state and its tools, with
        what each does, whether it is on and the ``seen`` fingerprint to turn it on as
        shown. Admin-only."""
        actor, on = await manager(await _auth(ctx), on=False)
        return await admin_ops.listing(sessions, actor.tenant_id, on=on)

    @mcp.tool(name="change_shared_connection", tags={"admin"})
    async def change_connection(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        action: ConnectionAction,
        origin_context_id: str,
        connection: str = "",
        name: str = "",
        url: str = "",
        scope: str = "",
    ) -> Connection | dict[str, bool]:
        """Add a shared connection (add: name, url, optional scope; its tools start off), or
        refresh, pause, resume, rescope (scope; empty clears it) or remove one (connection:
        its id or name). Runs on the asker's Approve card. A token is added or replaced in
        the web app, never here. Admin-only."""
        auth = await _auth(ctx)
        actor, _ = await manager(auth)
        await require_card(
            runtime, auth, tool_name="change_shared_connection", origin_context_id=origin_context_id
        )
        policy = runtime.settings.tool_safety
        try:
            if action == "add":
                return await admin_ops.add(
                    sessions, fernet(), policy, tenant_id=actor.tenant_id,
                    actor=actor.account_id, name=name, url=url, token=None,
                    scope=scope or None, now=now(),
                )  # fmt: skip
            found = await connection_id(actor, connection)
            if action == "refresh":
                return await admin_ops.refresh(
                    sessions, fernet(), policy, tenant_id=actor.tenant_id, connection_id=found
                )
            if action == "remove":
                await admin_ops.remove(
                    sessions, tenant_id=actor.tenant_id, actor=actor.account_id,
                    connection_id=found, now=now(),
                )  # fmt: skip
                return {"removed": True}
            return await admin_ops.edit(
                sessions, fernet(), tenant_id=actor.tenant_id, actor=actor.account_id,
                connection_id=found,
                status="paused" if action == "pause" else "active" if action == "resume" else None,
                scope=scope or None, scope_set=action == "rescope", token=None, now=now(),
            )  # fmt: skip
        except SharedConnectionError as exc:
            raise ToolError(f"{exc} Nothing changed.") from exc

    @mcp.tool(name="change_shared_connection_tool", tags={"admin"})
    async def change_tool(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        action: ToolAction,
        connection: str,
        tool: str,
        origin_context_id: str,
        seen: str = "",
        scope: str = "",
    ) -> Connection:
        """Turn one tool of a shared connection on (turn_on: the ``seen`` fingerprint from
        list_shared_connections, so it serves exactly what was shown) or off for the whole
        workspace, mark it as changing things or only reading, or rescope it (scope; empty
        clears it). To keep a tool from one agent, set that agent's tool rule instead.
        Runs on the asker's Approve card. Admin-only."""
        auth = await _auth(ctx)
        actor, _ = await manager(auth)
        await require_card(
            runtime,
            auth,
            tool_name="change_shared_connection_tool",
            origin_context_id=origin_context_id,
        )
        try:
            return await admin_ops.change_tool(
                sessions, tenant_id=actor.tenant_id, actor=actor.account_id,
                connection_id=await connection_id(actor, connection), name=tool,
                enabled={"turn_on": True, "turn_off": False}.get(action),
                seen=seen or None,
                writes={"mark_writes": True, "mark_reads": False}.get(action),
                scope=scope or None, scope_set=action == "rescope", now=now(),
            )  # fmt: skip
        except SharedConnectionError as exc:
            raise ToolError(f"{exc} Nothing changed.") from exc


__all__ = ["register_shared_connection_admin_tools"]
