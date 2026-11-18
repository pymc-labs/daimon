"""Links into the web app for tool results, limited to what the caller may open."""

from __future__ import annotations

from typing import Annotated

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.capability_gate import caller_places
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.web_admin import WebAdmin
from daimon.core.web_links import Assignment, TenantLinks, aware_at, panel_links
from daimon.core.web_roles_rules import is_admin, role_of_account
from pydantic import Field

log = structlog.get_logger(__name__)

# A result's link to the web app; left out of the output when there is none.
WebUrl = Annotated[str | None, Field(exclude_if=lambda value: value is None)]


async def web_aware(runtime: McpRuntime, auth: AuthIdentity) -> bool:
    """Whether `web_awareness` is On at every place the call may run (`caller_places`);
    False when that can't be read."""
    assignment = Assignment(runtime.deployment_default, runtime.client, runtime.resolver_cache)
    try:
        for place in await caller_places(runtime, auth, None):
            if not await aware_at(runtime.session_factory, runtime.settings, place, assignment):
                return False
    except Exception:  # named boundary: often after a committed write, so no link, no raise
        log.warning("web_url.aware_read_failed", exc_info=True)
        return False
    return True


async def _where_aware(
    runtime: McpRuntime, auth: AuthIdentity, links: TenantLinks | None
) -> TenantLinks | None:
    """`links` when `web_awareness` is On at every place the call may run (`web_aware`)."""
    return links if links is not None and await web_aware(runtime, auth) else None


async def tenant_links(runtime: McpRuntime, auth: AuthIdentity) -> TenantLinks | None:
    """The caller's workspace links, or None: no web app, `web_awareness` Off where the call
    runs, or a caller who can open nothing there. Pages follow the caller's web role, not
    their chat admin standing."""
    if runtime.settings.web is None:
        return None
    async with runtime.session_factory() as db:
        role = await role_of_account(
            db, tenant_id=auth.tenant_id, account_id=auth.account_id, chat_admin=auth.is_admin
        )
    if role is None:
        return None
    links = panel_links(runtime.settings.web, auth.tenant_id, admin=is_admin(role))
    return await _where_aware(runtime, auth, links)


async def admin_links(
    runtime: McpRuntime, auth: AuthIdentity, actor: WebAdmin
) -> TenantLinks | None:
    """The workspace links for a chat admin `web_admin_of` already resolved, with no second
    role lookup; None with no web app or `web_awareness` Off where the call runs."""
    links = panel_links(runtime.settings.web, actor.tenant_id, admin=is_admin(actor.role))
    return await _where_aware(runtime, auth, links)
