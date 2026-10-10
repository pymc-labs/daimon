"""Conversational selection of an agent's one filesystem repository."""

from __future__ import annotations

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
from daimon.core.stores.github_access import list_agent_repos, set_working_repo
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class WorkingRepoResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str
    working_repo: str | None
    pending: bool = False


async def set_working_repo_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    repo_name: str,
    agent_name: str | None = None,
    expected_ma_agent_id: str | None = None,
) -> WorkingRepoResult:
    if auth.is_external or auth.platform not in ("discord", "slack"):
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
        raise ToolError("Select a current agent before changing its working repo.")
    agent = await resolve_setup_agent(
        runtime,
        auth,
        name=target_name,
        expected_ma_agent_id=target_id,
        location_channel_id=origin_channel_id(origin),
    )
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))
    requested = repo_name.strip()
    if not requested:
        raise ToolError("Name one owner/repo or say none.")
    selected = None if requested.casefold() == "none" else requested
    if selected is not None and (selected.count("/") != 1 or any(c.isspace() for c in selected)):
        raise ToolError("Name one repo as owner/repo, or say none.")
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
        try:
            saved = await set_working_repo(
                session,
                tenant_id=auth.tenant_id,
                agent_id=agent_id,
                repo_name=selected,
                account_id=auth.account_id,
            )
        except ValueError as error:
            raise ToolError(str(error)) from error
        pending = saved is not None and any(
            repo.full_name.casefold() == saved.casefold() and repo.staged
            for repo in await list_agent_repos(session, tenant_id=auth.tenant_id, agent_id=agent_id)
        )
    return WorkingRepoResult(
        message=(
            f"{agent.name} will use {saved} as its working repo when GitHub setup finishes."
            if pending
            else f"{agent.name}'s working repo is now {saved}. "
            "To switch back, ask me for another repo or none."
            if saved is not None
            else f"{agent.name} has no working repo now. To set one, ask me for a repo."
        ),
        working_repo=saved,
        pending=pending,
    )


def register_github_working_repo_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def set_working_repo(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        repo_name: str,
        origin_context_id: str = "",
        agent_name: str | None = None,
        expected_ma_agent_id: str | None = None,
    ) -> WorkingRepoResult:
        """Choose the one repo in this agent's filesystem, or pass 'none' to clear it.

        A clear ask is enough: set it right away and reply with the returned
        line. Ask once in plain words during agent setup. Use an owner/repo already on
        this agent's list. Other repos have token access; clone one with gh repo clone when
        asked. Only a server admin or an admin for this non-managed agent's
        channels may change this setting. Pass the current turn origin and the
        selected agent's name and MA id. If the repo is not on its list, offer
        the single Connect GitHub link.
        """
        return await set_working_repo_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            repo_name=repo_name,
            agent_name=agent_name,
            expected_ma_agent_id=expected_ma_agent_id,
        )
