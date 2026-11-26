"""A person's own data-source accounts, from chat: what they are known as, their sign-ins,
the sources open to them and the notices about links an admin changed (`list_my_data` lists
them; `remove_my_data` disconnects one).

Starting a sign-in runs on the person's card. Its link or code goes to their direct messages,
never into the conversation: anyone who enters a device code connects their own account to it.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import require_card
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._data_admin import (
    own,
    person,
    plain_all,
    refused,
    schemas_of,
    store,
    uuid_of,
)
from daimon.adapters.mcp.tools._web_admin import send_privately
from daimon.data import identity, manage
from daimon.data.connect import (
    PROVIDERS,
    Connector,
    Failed,
    NotConfigured,
    configured_clients,
)
from daimon.data.identity import IdentityError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_UNREACHABLE = "The sign-in page isn't reachable. Try again later. Nothing changed."
_NOT_SET_UP = "Signing in to that source isn't set up on this server. Nothing changed."


async def my_data_accounts(runtime: McpRuntime, auth: AuthIdentity) -> dict[str, Any] | None:
    """The caller's accounts in the data sources, or None where data sources are off. Their
    unread notices are listed and left unread (`dismiss_data_notices` reads them)."""
    try:
        actor = await person(runtime, auth)
    except ToolError:
        return None
    schemas, tenant = schemas_of(runtime), actor.tenant_id
    async with store(runtime) as conn:
        accounts = await manage.identities(conn, schemas, tenant, account_id=actor.account_id)
        made = await manage.person_connections(conn, schemas, tenant, actor.account_id)
        open_ = await manage.connectable(conn, schemas, tenant, list(PROVIDERS))
        notices = await identity.unseen_notices(conn, schemas, tenant, actor.account_id)
    have = {c.source_id for c in made}
    return {
        "accounts": [
            {"provider": a.provider, "workspace": a.workspace, "name": a.name, "proof": a.proof}
            for a in accounts
        ],
        "sign_ins": [
            {"source_id": str(c.source_id), "source": c.source, "status": c.status}
            for c in made
        ],
        "can_connect": [
            {"source_id": str(i), "source": n} for i, n, _ in open_ if i not in have
        ],
        "notices": plain_all(notices),
    }  # fmt: skip


async def disconnect_data_source(
    runtime: McpRuntime, auth: AuthIdentity, source_id: str, origin_context_id: str
) -> None:
    """End the caller's own sign-in to a source, on their card."""
    own(auth)
    actor = await person(runtime, auth)
    await require_card(
        runtime, auth, tool_name="remove_my_data", origin_context_id=origin_context_id
    )
    async with httpx.AsyncClient() as http, store(runtime) as conn:
        try:
            await _connector(runtime, http).disconnect(
                conn, account_id=actor.account_id, source_id=uuid_of(source_id, "key")
            )
        except IdentityError as e:
            raise refused(e) from None


def _connector(runtime: McpRuntime, http: httpx.AsyncClient) -> Connector:
    web = runtime.settings.web
    if web is None:
        raise ToolError("The web app is not set up here. Nothing changed.")
    callback = f"{str(web.public_url).rstrip('/')}/api/profile/data/connect/callback"
    keys = tuple(k.get_secret_value() for k in runtime.settings.crypto.keys)
    return Connector(
        schemas_of(runtime), keys, http, callback,
        clients=configured_clients(runtime.settings.data),
    )  # fmt: skip


def register_data_connect_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def connect_my_data(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, source_id: str, origin_context_id: str
    ) -> dict[str, Any]:
        """Start your own sign-in to a data source (source_id from list_my_data's
        can_connect). Runs on your Approve card. The link or code is sent to you by direct
        message, never shown here; after you approve it there, check_my_data_connection
        finishes it."""
        auth = await _auth(ctx)
        own(auth)
        actor = await person(runtime, auth)
        await require_card(
            runtime, auth, tool_name="connect_my_data", origin_context_id=origin_context_id
        )
        async with httpx.AsyncClient(timeout=10.0) as http, store(runtime) as conn:
            try:
                begun = await _connector(runtime, http).start(
                    conn, tenant_id=actor.tenant_id, account_id=actor.account_id,
                    source_id=uuid_of(source_id, "source_id"),
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None
            except NotConfigured:
                raise ToolError(_NOT_SET_UP) from None
            except (Failed, httpx.HTTPError):
                raise ToolError(_UNREACHABLE) from None
        words = f"Code {begun.user_code}, then open" if begun.flow == "device" else "Open"
        await send_privately(runtime, auth, f"Sign in to {begun.source}: {words} {begun.url}")
        return {
            "flow": begun.flow, "source": begun.source, "pending_id": str(begun.pending_id),
            "delivered": "direct_message", "expires_at": begun.expires_at,
        }  # fmt: skip

    @mcp.tool
    async def check_my_data_connection(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, pending_id: str
    ) -> dict[str, Any]:
        """Whether the sign-in started by connect_my_data (pending_id) is finished. Where
        the link opened a page, it finishes there and shows in list_my_data."""
        auth = await _auth(ctx)
        own(auth)
        actor = await person(runtime, auth)
        async with httpx.AsyncClient(timeout=10.0) as http, store(runtime) as conn:
            try:
                done = await _connector(runtime, http).poll(
                    conn, account_id=actor.account_id, pending_id=uuid_of(pending_id, "pending_id")
                )
            except IdentityError as e:
                raise ToolError(str(e)) from None
            except (Failed, NotConfigured, httpx.HTTPError):
                raise ToolError(_UNREACHABLE) from None
        return {"connected": done is not None}

    @mcp.tool
    async def dismiss_data_notices(ctx: Context) -> dict[str, Literal[True]]:  # pyright: ignore[reportUnusedFunction]
        """Mark your notices about data-source links an admin changed as read. They show in
        list_my_data until then."""
        auth = await _auth(ctx)
        own(auth)
        actor = await person(runtime, auth)
        async with store(runtime) as conn:
            await identity.take_notices(
                conn, schemas_of(runtime), actor.tenant_id, actor.account_id
            )
        return {"done": True}


__all__ = ["disconnect_data_source", "my_data_accounts", "register_data_connect_tools"]
