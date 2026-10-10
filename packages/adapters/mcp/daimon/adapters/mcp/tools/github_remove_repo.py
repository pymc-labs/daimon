"""Remove one repository from one agent after confirmation in chat."""

from __future__ import annotations

from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import (
    origin_channel_id,
    require_turn_origin,
    resolve_setup_agent,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.github_panel import requester_manages_agent
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access import (
    remove_agent_repo,
    repo_id_for_agent_removal,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class RemoveRepoResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["proposed", "removed"]
    message: str


async def remove_repo_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    repo_name: str,
    agent_name: str | None = None,
    expected_ma_agent_id: str | None = None,
    confirmed: bool = False,
) -> RemoveRepoResult:
    if auth.is_external or auth.platform not in ("discord", "slack") or not auth.platform_user_id:
        raise ToolError("GitHub setup needs a Discord or Slack workspace member.")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    target_name = agent_name or origin.configuration_target_name or origin.responder_name
    target_id = expected_ma_agent_id
    if target_id is None:
        if target_name == origin.configuration_target_name:
            target_id = origin.configuration_target_ma_agent_id
        elif target_name == origin.responder_name:
            target_id = origin.responder_ma_agent_id
    if not target_name or not target_id:
        raise ToolError("Select a current agent before removing a repo.")
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=target_name,
        expected_ma_agent_id=target_id,
        location_channel_id=origin_channel_id(origin),
    )
    requested = repo_name.strip()
    if requested.count("/") != 1 or any(c.isspace() for c in requested):
        raise ToolError("Name one repo as owner/repo.")
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))
    async with runtime.session_factory.begin() as session:
        account = await get_account(session, auth.account_id)
        if account is None or account.tenant_id != auth.tenant_id or account.is_external:
            raise ToolError("GitHub setup is unavailable for this account.")
        is_admin = account.role == Role.ADMIN and auth.is_admin
        lookups = runtime.group_lookups
        manages = is_admin or await requester_manages_agent(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=auth.platform,
            platform_user_id=auth.platform_user_id if auth.agent_id is None else None,
            agent_name=agent.name,
            other_names=(target_name, str(agent.metadata.get(MA_METADATA_KEY_NAME) or "")),
            ma_agent_id=str(agent.id),
            default=runtime.deployment_default,
            is_daimon_managed=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
            members=(
                lookups.live_members(auth.platform, auth.external_id)
                if lookups is not None and auth.external_id is not None
                else None
            ),
        )
        if not manages:
            raise ToolError("Ask a server admin or an admin for this agent's channels.")
        repo_id = await repo_id_for_agent_removal(
            session, tenant_id=auth.tenant_id, agent_id=agent_id, repo_name=requested
        )
        if repo_id is None:
            raise ToolError("That repo is not on this agent's list.")
        # A clear ask is enough (Derick, 2026-10-10): no separate "say yes" turn.
        removed = await remove_agent_repo(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            account_id=auth.account_id,
        )
        if not removed:
            raise ToolError("That repo is no longer on this agent's list.")
    return RemoveRepoResult(
        status="removed",
        message=(
            f"Removed {requested} from {agent.name}. "
            "To undo, ask me to add it back or use Connect GitHub."
        ),
    )


def register_github_remove_repo_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def remove_repo(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        repo_name: str,
        origin_context_id: str = "",
        agent_name: str | None = None,
        expected_ma_agent_id: str | None = None,
        confirmed: bool = False,
    ) -> RemoveRepoResult:
        """Remove owner/repo from this agent only.

        A clear ask is enough: call it right away and reply with the returned
        line. Ask only if the repo or agent is unclear. confirmed is ignored.
        The Connect GitHub page also adds and removes repos. If it was the working
        repo, the working repo is cleared. Other agents keep their access.
        Pass the current turn origin and selected agent identity.
        """
        return await remove_repo_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            repo_name=repo_name,
            agent_name=agent_name,
            expected_ma_agent_id=expected_ma_agent_id,
            confirmed=confirmed,
        )
