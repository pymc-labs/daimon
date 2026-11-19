"""What the chat tools that run the web app's admin pages share: the acting admin, the
workspace's agents and private delivery of a link."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl
from daimon.adapters.mcp.tools.setup_target import get_chat_origin
from daimon.core import web_admin, web_roles_rules
from daimon.core.agent_target import ChangeRecorder
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.session_seal import is_private_conversation
from daimon.core.stores import web_people, web_roles
from daimon.core.web_admin import WebAdmin, WebAdminError
from daimon.core.web_roles_rules import role_for
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession


def now() -> datetime:
    return datetime.now(UTC)


async def web_admin_of(runtime: McpRuntime, auth: AuthIdentity) -> WebAdmin:
    """The calling chat admin as the web app sees them, with the web role they hold."""
    _require_admin(auth)
    async with runtime.session_factory() as db:
        person = await web_people.get_person_for_account(
            db, tenant_id=auth.tenant_id, account_id=auth.account_id
        )
        if person is not None and person.status == "suspended":
            raise ToolError("Your web access is suspended.")
        if person is not None:
            # Their own role: a visitor or an unadmitted member is no admin in the web app.
            role = await web_roles_rules.role_of(db, person)
        else:
            role = role_for(
                kind="member",
                workspace_grant=await web_roles.get_workspace_role(
                    db, tenant_id=auth.tenant_id, account_id=auth.account_id
                ),
                platform_admin=auth.is_platform_admin,
                chat_admin=auth.is_admin,
            )
    return WebAdmin(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        role=role,
        person_id=None if person is None else person.id,
    )


async def agent_names(runtime: McpRuntime, tenant_id: uuid.UUID) -> dict[str, str]:
    agents = await list_agents_by_tenant(
        runtime.client, tenant_id=tenant_id, records=runtime.session_factory
    )
    return {a.id: a.name for a in agents}


PrivateDetails = Literal["shown", "direct_message"]
"""`direct_message`: emails and sign-ins are left out; they show in a direct message."""


async def private_details(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> PrivateDetails:
    """`shown` only when this turn runs in the admin's own direct message or web chat."""
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    private = (
        origin is not None
        and not origin.shared_channel
        and not origin.sender_foreign
        and (
            origin.platform == "web"
            or is_private_conversation(origin.thread_id)
            or is_private_conversation(origin.parent_channel_id)
        )
    )
    return "shown" if private else "direct_message"


def refused(error: WebAdminError) -> ToolError:
    return ToolError(str(error))


async def send_privately(runtime: McpRuntime, auth: AuthIdentity, text: str) -> None:
    """Direct-message the caller. A link or key never goes into the channel."""
    if not auth.platform_user_id:
        raise ToolError("Could not deliver by direct message: the caller has no chat user.")
    await send_direct_message_impl(runtime, auth, recipient_id=auth.platform_user_id, content=text)


def audit_to(auth: AuthIdentity, action: str, detail: str | None = None) -> ChangeRecorder:
    """A recorder naming the agent or skill changed, written in the change's transaction.
    `detail` never carries a value."""

    async def record(db: AsyncSession, name: str) -> None:
        await web_admin.audit(
            db,
            tenant_id=auth.tenant_id,
            actor_account_id=auth.account_id,
            action=action,
            target=name,
            detail=detail,
            at=now(),
        )

    return record
