"""Private, agent-bound GitHub setup from an active conversation."""

from __future__ import annotations

from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl
from daimon.adapters.mcp.tools.setup_target import (
    origin_channel_id,
    require_turn_origin,
    resolve_setup_agent,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import (
    CLIENT_AGENT_MESSAGE,
    ClientAgentConnectionError,
    mint_invitation,
    record_connect_request,
    require_app_eligible_agent,
    revoke_invitation,
)
from daimon.core.stores.security_audit import append_event
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class ConnectResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["sent", "ask_admin", "dm_blocked", "client_agent"]
    message: str


async def _github_connect_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    agent_name: str | None = None,
    expected_ma_agent_id: str | None = None,
) -> ConnectResult:
    if auth.platform not in ("discord", "slack") or auth.platform_user_id is None:
        raise ToolError("GitHub setup from chat is available in Discord and Slack.")
    if auth.is_external:
        raise ToolError("GitHub setup is for members of this workspace.")
    root = runtime.settings.mcp.app_root_url
    config = runtime.settings.github_app
    if root is None or not all(
        (config.app_id, config.app_slug, config.private_key, config.client_id, config.client_secret)
    ):
        raise ToolError("GitHub setup is unavailable on this server.")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    target_name = agent_name or origin.configuration_target_name or origin.responder_name
    target_ma_id = expected_ma_agent_id
    if target_ma_id is None:
        if target_name == origin.configuration_target_name:
            target_ma_id = origin.configuration_target_ma_agent_id
        elif target_name == origin.responder_name:
            target_ma_id = origin.responder_ma_agent_id
    if not target_name or not target_ma_id:
        raise ToolError("Select a current agent before connecting GitHub.")
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=target_name,
        expected_ma_agent_id=target_ma_id,
        location_channel_id=origin_channel_id(origin),
    )
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))
    async with runtime.session_factory.begin() as session:
        try:
            await require_app_eligible_agent(
                session, tenant_id=auth.tenant_id, agent_name=agent.name
            )
        except ClientAgentConnectionError:
            return ConnectResult(status="client_agent", message=CLIENT_AGENT_MESSAGE)
        account = await get_account(session, auth.account_id)
        if account is None or account.tenant_id != auth.tenant_id or account.is_external:
            raise ToolError("GitHub setup is unavailable for this account.")
        if account.role != Role.ADMIN or not auth.is_admin:
            await record_connect_request(
                session,
                tenant_id=auth.tenant_id,
                requester_account_id=auth.account_id,
                agent_id=agent_id,
                agent_name=agent.name,
            )
            await append_event(
                session,
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                agent_id=agent_id,
                platform=auth.platform,
                platform_user_id=auth.platform_user_id,
                tool_name="github_connect",
                operation="github_connect",
                outcome="allowed",
                reason="admin requested",
            )
            return ConnectResult(status="ask_admin", message="Ask an admin")
        token = await mint_invitation(
            session,
            tenant_id=auth.tenant_id,
            requester_account_id=auth.account_id,
            requester_label=auth.platform_user_id,
            agent_id=agent_id,
            agent_name=agent.name,
        )
        await append_event(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            agent_id=agent_id,
            platform=auth.platform,
            platform_user_id=auth.platform_user_id,
            tool_name="github_connect",
            operation="github_connect",
            outcome="allowed",
            reason="admin link minted",
        )
    url = f"{root}/oauth/github/connect/{token}"
    try:
        await send_direct_message_impl(
            runtime,
            auth,
            recipient_id=auth.platform_user_id,
            content=f"Connect GitHub for {agent.name}:\n{url}",
        )
    except Exception:
        async with runtime.session_factory.begin() as session:
            await revoke_invitation(session, token=token)
        return ConnectResult(
            status="dm_blocked", message="I can't DM you. Run /github connect here."
        )
    return ConnectResult(status="sent", message="Connect link sent privately.")


def register_github_connect_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def github_connect(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        agent_name: str | None = None,
        expected_ma_agent_id: str | None = None,
    ) -> ConnectResult:
        """Connect this agent to GitHub when someone asks to set up GitHub.

        Send the admin a private, single-use link bound to the selected agent.
        Members get Ask an admin and a recorded request. Never repeat a private
        link in a shared reply. For a setup target, pass its current name and
        MA id from turn_controls; otherwise this defaults to the responder.
        """
        return await _github_connect_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            agent_name=agent_name,
            expected_ma_agent_id=expected_ma_agent_id,
        )
