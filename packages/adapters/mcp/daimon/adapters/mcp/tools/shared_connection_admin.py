"""Chat tools for the shared connections page: list, and change on the asker's card.

The list answers directly, with approved words only: an upstream's text waiting for review
shows on the turn_on card, never in the model's context. In a shared conversation the addresses
and errors are placeholders (`_private_values`) that `send_private_details` opens in the asker's
direct message. Each change runs only on the asker's Approve card (`CARDED_TOOLS`), which names
what it acts on (`shared_connections.card_lookup`), through the same
`daimon.core.shared_connection_admin` functions and checks as the web routes. A token never
passes through chat: add and refresh post the asker's private form for it.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._private_values import Privately, privately
from daimon.adapters.mcp.tools._web_admin import now, web_admin_of
from daimon.adapters.mcp.tools.credential_requests import request_data_source_credential
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
_NOT_ADMIN = "Only an admin or an owner manages shared connections."
#: An upstream's words waiting for review: only the turn_on card shows them.
_UNREVIEWED = frozenset({"seen_description", "seen_schema"})


def _view(connection: Connection, hide: Privately) -> dict[str, Any]:
    """Approved words only; the address and error as the room may read them."""
    hidden: dict[str, Any] = {"tools": {"__all__": _UNREVIEWED}}
    shown = connection.model_dump(mode="json", exclude=hidden)
    return {**shown, "url": hide(connection.url), "error": hide(connection.error)}


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
    ) -> list[dict[str, Any]]:
        hide = await privately(runtime, auth, origin_context_id)
        return [_view(c, hide) for c in connections]

    async def token_form(
        auth: AuthIdentity, origin_context_id: str, found: Connection
    ) -> dict[str, Any]:
        """The asker's private form for the connection's token; saving it reads its tools."""
        if found.declared:
            raise ToolError(f"{admin_ops.READ_ONLY} Nothing changed.")
        form = await request_data_source_credential(
            runtime, auth, source_id=uuid.UUID(found.id), name=found.name,
            origin_context_id=origin_context_id, kind="data_connection",
        )  # fmt: skip
        return {"credential_form": form.instruction}

    @mcp.tool(name="list_shared_connections", tags={"admin"})
    async def list_connections(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str | None = None
    ) -> dict[str, Any]:
        """List the workspace's shared connections: each server's state and its tools, with
        what each does, whether it is on, whether the upstream changed it and the ``seen``
        fingerprint to turn it on as shown (the card shows its words). Addresses and errors
        are [[private:...]] placeholders in a shared conversation (a channel or group): answer
        without them and send any the admin needs with send_private_details. Pass this active
        turn's origin_context_id. Admin-only."""
        auth = await _auth(ctx)
        actor, on = await manager(auth, on=False)
        found = await admin_ops.listing(sessions, actor.tenant_id, on=on)
        return {
            "enabled": on,
            "connections": await answer(auth, origin_context_id, found.connections),
        }

    @mcp.tool(name="change_shared_connection", tags={"admin"})
    async def change_connection(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        action: ConnectionAction,
        origin_context_id: str,
        connection: str = "",
        name: str = "",
        url: str = "",
        scope: str = "",
        with_token: bool = False,
    ) -> dict[str, Any]:
        """Add a shared connection (add: name, url, optional scope; its tools start off), or
        refresh, pause, resume, rescope (scope; empty clears it) or remove one (connection:
        its id or name). Runs on the asker's Approve card, which names the connection.
        with_token, on add or refresh, posts the asker's private form for its token,
        and saving it reads the server's tools; never ask for a token in chat or pass one here.
        Addresses and errors in the answer are [[private:...]] placeholders in a shared
        conversation: send them with send_private_details. Admin-only."""
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
                found_id = await connection_id(actor, connection)
                if with_token:
                    current = await admin_ops.view(sessions, actor.tenant_id, found_id)
                    return await token_form(auth, origin_context_id, current)
                changed = await admin_ops.refresh(
                    sessions, fernet(), policy, tenant_id=actor.tenant_id,
                    connection_id=found_id,
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
        (shown,) = await answer(auth, origin_context_id, [changed])
        if action == "add" and with_token:
            shown.update(await token_form(auth, origin_context_id, changed))
        return shown

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
        return (await answer(auth, origin_context_id, [changed]))[0]


__all__ = ["register_shared_connection_admin_tools"]
