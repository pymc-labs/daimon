"""Chat tools for the shared connections page: list, and change on the asker's card.

The list answers directly, with approved words only: an upstream's text waiting for review
shows on the turn_on card, never in the model's context. Outside a private conversation the
addresses and errors go to the asker by direct message. Each change runs only on the asker's
Approve card (`CARDED_TOOLS`), which names what it acts on
(`shared_connections.card_lookup`), through the same `daimon.core.shared_connection_admin`
functions and checks as the web routes. A token never passes through chat.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import (
    PrivateDetails,
    now,
    private_details,
    send_privately,
    web_admin_of,
)
from daimon.core import shared_connection_admin as admin_ops
from daimon.core import shared_connections as shared
from daimon.core.shared_connection_admin import NO_CONNECTION, OFF, Connection
from daimon.core.shared_connections import SharedConnectionError
from daimon.core.web_admin import WebAdmin
from daimon.core.web_roles_rules import is_admin
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

ConnectionAction = Literal["add", "refresh", "pause", "resume", "rescope", "remove"]
_STATUS: dict[str, Literal["active", "paused"]] = {"pause": "paused", "resume": "active"}
ToolAction = Literal["turn_on", "turn_off", "mark_writes", "mark_reads", "rescope"]
_NOT_SENT = "The address and errors couldn't be sent to you privately."
_NOT_ADMIN = "Only an admin or an owner manages shared connections."
#: An upstream's words waiting for review: only the turn_on card shows them.
_UNREVIEWED = frozenset({"seen_description", "seen_schema"})


def _view(connection: Connection, details: PrivateDetails) -> dict[str, Any]:
    """Approved words only; outside a private conversation, no address or error."""
    hidden: dict[str, Any] = {"tools": {"__all__": _UNREVIEWED}}
    if details != "shown":
        hidden.update(url=True, error=True)
    return connection.model_dump(mode="json", exclude=hidden)


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
        async with sessions() as db:
            found = await shared.find_connection(db, actor.tenant_id, ref)
        if found is None:
            raise ToolError(NO_CONNECTION)
        return found.id

    def fernet() -> MultiFernet:
        if runtime.fernet is None:
            raise ToolError("Shared connections aren't set up here.")
        return runtime.fernet

    async def answer(
        auth: AuthIdentity, origin_context_id: str | None, connections: list[Connection]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The connections as the room may read them; addresses and errors to the asker. A
        change already made stays reported as made when that message fails."""
        details = await private_details(runtime, auth, origin_context_id)
        head: dict[str, Any] = {"private_details": details}
        if details != "shown" and connections:
            lines = (
                f"{c.name}: {c.url}" + (f" ({c.error})" if c.error else "") for c in connections
            )
            try:
                await send_privately(runtime, auth, "\n".join(lines))
            except ToolError:
                head["details_not_sent"] = _NOT_SENT
        return head, [_view(c, details) for c in connections]

    @mcp.tool(name="list_shared_connections", tags={"admin"})
    async def list_connections(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str | None = None
    ) -> dict[str, Any]:
        """List the workspace's shared connections: each server's state and its tools, with
        what each does, whether it is on, whether the upstream changed it and the ``seen``
        fingerprint to turn it on as shown (the card shows its words). Addresses and errors
        show only in a direct message with the admin. Pass this active turn's
        origin_context_id. Admin-only."""
        auth = await _auth(ctx)
        actor, on = await manager(auth, on=False)
        found = await admin_ops.listing(sessions, actor.tenant_id, on=on)
        head, shown = await answer(auth, origin_context_id, found.connections)
        return {**head, "enabled": on, "connections": shown}

    @mcp.tool(name="change_shared_connection", tags={"admin"})
    async def change_connection(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        action: ConnectionAction,
        origin_context_id: str,
        connection: str = "",
        name: str = "",
        url: str = "",
        scope: str = "",
    ) -> dict[str, Any]:
        """Add a shared connection (add: name, url, optional scope; its tools start off), or
        refresh, pause, resume, rescope (scope; empty clears it) or remove one (connection:
        its id or name). Runs on the asker's Approve card, which names the connection and its
        address. A token is added or replaced under Integrations, Shared connections, never
        here. Admin-only."""
        auth = await _auth(ctx)
        # As on the web, a connection can be removed with the feature off.
        actor, _ = await manager(auth, on=action != "remove")
        await require_card(
            runtime, auth, tool_name="change_shared_connection", origin_context_id=origin_context_id
        )
        policy = runtime.settings.tool_safety
        try:
            if action == "add":
                changed = await admin_ops.add(
                    sessions, fernet(), policy, tenant_id=actor.tenant_id,
                    actor=actor.account_id, name=name, url=url, token=None,
                    scope=scope or None, now=now(),
                )  # fmt: skip
            elif action == "refresh":
                changed = await admin_ops.refresh(
                    sessions, fernet(), policy, tenant_id=actor.tenant_id,
                    connection_id=await connection_id(actor, connection),
                )  # fmt: skip
            elif action == "remove":
                await admin_ops.remove(
                    sessions, tenant_id=actor.tenant_id, actor=actor.account_id,
                    connection_id=await connection_id(actor, connection), now=now(),
                )  # fmt: skip
                return {"removed": True}
            else:
                status = _STATUS.get(action)
                changed = await admin_ops.edit(
                    sessions, fernet(), tenant_id=actor.tenant_id, actor=actor.account_id,
                    connection_id=await connection_id(actor, connection), status=status,
                    scope=scope or None, scope_set=action == "rescope", token=None, now=now(),
                )  # fmt: skip
        except SharedConnectionError as exc:
            raise ToolError(f"{exc} Nothing changed.") from exc
        head, shown = await answer(auth, origin_context_id, [changed])
        return {**head, **shown[0]}

    @mcp.tool(name="change_shared_connection_tool", tags={"admin"})
    async def change_tool(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        action: ToolAction,
        connection: str,
        tool: str,
        origin_context_id: str,
        seen: str = "",
        scope: str = "",
    ) -> dict[str, Any]:
        """Turn one tool of a shared connection on (turn_on: the ``seen`` fingerprint from
        list_shared_connections, so it serves exactly what was shown) or off for the whole
        workspace, mark it as changing things or only reading, or rescope it (scope; empty
        clears it). To keep a tool from one agent, set that agent's tool rule instead.
        Runs on the asker's Approve card, which names the tool, its server and, to turn it
        on, the words its callers get. Admin-only."""
        auth = await _auth(ctx)
        actor, _ = await manager(auth)
        await require_card(
            runtime,
            auth,
            tool_name="change_shared_connection_tool",
            origin_context_id=origin_context_id,
        )
        try:
            changed = await admin_ops.change_tool(
                sessions, tenant_id=actor.tenant_id, actor=actor.account_id,
                connection_id=await connection_id(actor, connection), name=tool,
                enabled={"turn_on": True, "turn_off": False}.get(action),
                seen=seen or None,
                writes={"mark_writes": True, "mark_reads": False}.get(action),
                scope=scope or None, scope_set=action == "rescope", now=now(),
            )  # fmt: skip
        except SharedConnectionError as exc:
            raise ToolError(f"{exc} Nothing changed.") from exc
        head, shown = await answer(auth, origin_context_id, [changed])
        return {**head, **shown[0]}


__all__ = ["register_shared_connection_admin_tools"]
