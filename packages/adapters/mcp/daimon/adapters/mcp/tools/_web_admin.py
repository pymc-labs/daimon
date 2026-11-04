"""What the chat tools that run the web app's admin pages share: the acting admin, the
workspace's agents and private delivery of a link."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.stores import web_roles
from daimon.core.web_admin import WebAdmin, WebAdminError
from daimon.core.web_roles_rules import role_for
from fastmcp.exceptions import ToolError


def now() -> datetime:
    return datetime.now(UTC)


async def web_admin_of(runtime: McpRuntime, auth: AuthIdentity) -> WebAdmin:
    """The calling chat admin as the web app sees them, with the role their grant gives."""
    _require_admin(auth)
    async with runtime.session_factory() as db:
        grant = await web_roles.get_workspace_role(
            db, tenant_id=auth.tenant_id, account_id=auth.account_id
        )
    role = role_for(
        kind="member",
        workspace_grant=grant,
        platform_admin=auth.is_platform_admin,
        chat_admin=auth.is_admin,
    )
    return WebAdmin(tenant_id=auth.tenant_id, account_id=auth.account_id, role=role)


async def agent_names(runtime: McpRuntime, tenant_id: uuid.UUID) -> dict[str, str]:
    agents = await list_agents_by_tenant(runtime.client, tenant_id=tenant_id)
    return {a.id: a.name for a in agents}


def refused(error: WebAdminError) -> ToolError:
    return ToolError(str(error))


async def send_privately(runtime: McpRuntime, auth: AuthIdentity, text: str) -> None:
    """Direct-message the caller. A link or key never goes into the channel."""
    if not auth.platform_user_id:
        raise ToolError("Could not deliver by direct message: the caller has no chat user.")
    await send_direct_message_impl(runtime, auth, recipient_id=auth.platform_user_id, content=text)
