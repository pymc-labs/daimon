"""Links into the web app for tool results, limited to what the caller may open."""

from __future__ import annotations

from typing import Annotated

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.web_admin import WebAdmin
from daimon.core.web_links import TenantLinks, WebLinks
from daimon.core.web_roles_rules import is_admin, role_of_account
from pydantic import Field

# A result's link to the web app; left out of the output when there is none.
WebUrl = Annotated[str | None, Field(exclude_if=lambda value: value is None)]


async def tenant_links(runtime: McpRuntime, auth: AuthIdentity) -> TenantLinks | None:
    """The caller's workspace links, or None: no web app, or a caller who can open nothing
    there. Pages follow the caller's web role, not their chat admin standing."""
    links = WebLinks.from_settings(runtime.settings.web)
    if links is None:
        return None
    async with runtime.session_factory() as db:
        role = await role_of_account(
            db, tenant_id=auth.tenant_id, account_id=auth.account_id, chat_admin=auth.is_admin
        )
    return None if role is None else links.for_tenant(auth.tenant_id, admin=is_admin(role))


def admin_links(runtime: McpRuntime, actor: WebAdmin) -> TenantLinks | None:
    """The workspace links for a chat admin `web_admin_of` already resolved: no web app,
    none; otherwise the admin pages, with no second role lookup."""
    links = WebLinks.from_settings(runtime.settings.web)
    return None if links is None else links.for_tenant(actor.tenant_id, admin=is_admin(actor.role))
