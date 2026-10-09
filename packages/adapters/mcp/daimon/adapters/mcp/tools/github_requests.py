"""Turn-bound GitHub access requests and private admin delivery."""

from __future__ import annotations

import uuid
from typing import Literal

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.github_request_delivery import (
    deliver_private_request_card,
    deliver_shared_admin_card,
)
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.github_app_session import effective_repo_state
from daimon.core.github_request_cards import RequestCard, requester_card
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.github_access import list_agent_grants, list_authorized_repos
from daimon.core.stores.github_access_requests import (
    AdminRecipient,
    get_request,
    list_server_admin_recipients,
    request_access,
)
from daimon.core.stores.github_app_installations import get as get_app_installation
from daimon.core.stores.github_links import account_link_is_broken, account_link_status
from daimon.core.stores.github_personal_links import mint_link
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class GitHubRequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "waiting", "no_admin", "personal_link", "unavailable"]
    message: str
    request_id: uuid.UUID | None = None


async def _admin_recipients(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    platform: Literal["discord", "slack"],
    requester_account_id: uuid.UUID,
) -> list[AdminRecipient]:
    async with runtime.session_factory() as session:
        return await list_server_admin_recipients(
            session,
            tenant_id=tenant_id,
            platform=platform,
            exclude_account_id=requester_account_id,
        )


async def request_github_access_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    repo_name: str,
    required_ability: Literal["read", "write"],
    remaining_work: str,
) -> GitHubRequestResult:
    """Request access for the verified asker's unfinished work in this thread."""
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if origin.is_external or auth.is_external:
        return GitHubRequestResult(
            status="unavailable",
            message="I can't use GitHub for people outside this workspace.",
        )
    if origin.platform == "teams":
        return GitHubRequestResult(
            status="unavailable",
            message="GitHub setup isn't in Teams yet. Set it up from Discord or Slack.",
        )
    if origin.platform not in ("discord", "slack") or auth.platform_user_id is None:
        raise ToolError("GitHub requests need a Discord or Slack conversation.")
    if len(remaining_work.strip()) < 8:
        raise ToolError("Describe the unfinished work so it can continue after access is ready.")
    agent_id = derive_agent_uuid(
        tenant_id=origin.tenant_id, ma_agent_id=origin.responder_ma_agent_id
    )
    async with runtime.session_factory() as session:
        known_repos = await list_authorized_repos(session, tenant_id=origin.tenant_id)
        known = next(
            (
                repo
                for repo in known_repos
                if repo.repo_full_name.casefold() == repo_name.casefold()
            ),
            None,
        )
        connected = next(
            (
                repo
                for repo in known_repos
                if repo.status == "active"
                and repo.repo_full_name.casefold() == repo_name.casefold()
            ),
            None,
        )
        installation = (
            await get_app_installation(session, installation_id=known.installation_id)
            if known is not None and known.status == "active"
            else None
        )
        if connected is not None and (
            installation is None or connected.repo_full_name not in installation.repo_full_names
        ):
            connected = None
        grant = None
        if connected is not None:
            grant = next(
                (
                    row
                    for row in await list_agent_grants(
                        session, tenant_id=origin.tenant_id, agent_id=agent_id
                    )
                    if row.repo_id == connected.repo_id and not row.staged
                ),
                None,
            )
        linked_login = await account_link_status(session, account_id=origin.account_id)
        broken_link = await account_link_is_broken(session, account_id=origin.account_id)
    rank = {"none": 0, "read": 1, "write": 2}
    grant_covers = grant is not None and rank[grant.ceiling_access] >= rank[required_ability]
    personal_case = grant_covers and not linked_login
    if (
        auth.is_admin
        and connected is not None
        and grant_covers
        and linked_login
        and runtime.fernet is not None
    ):
        try:
            names, permissions = await effective_repo_state(
                runtime.session_factory,
                tenant_id=origin.tenant_id,
                agent_id=agent_id,
                account_id=origin.account_id,
                is_external=False,
                config=runtime.settings.github_app,
                fernet=runtime.fernet,
            )
            if f"https://github.com/{connected.repo_full_name}" in names and (
                required_ability == "read"
                or permissions.get(connected.repo_id, {}).get("contents") == "write"
            ):
                return GitHubRequestResult(
                    status="ready", message="GitHub access is ready. Continue the request."
                )
        except (ValueError, httpx.HTTPError):
            return GitHubRequestResult(
                status="unavailable", message="GitHub didn't answer. Try again in a minute."
            )
        # The answer for an unusable connected repo must not reveal whether it exists.
    asker_is_admin = auth.is_admin
    try:
        async with runtime.session_factory.begin() as session:
            request = await request_access(
                session,
                tenant_id=origin.tenant_id,
                requester_account_id=origin.account_id,
                requester_platform_user_id=auth.platform_user_id,
                platform=origin.platform,
                parent_channel_id=origin.parent_channel_id,
                thread_id=origin.thread_id,
                agent_id=agent_id,
                ma_agent_id=origin.responder_ma_agent_id,
                agent_name=origin.responder_name,
                repo_name=repo_name,
                requested_work=remaining_work,
                required_ability=required_ability,
                is_admin=asker_is_admin,
            )
    except ValueError as error:
        if str(error) == "Your admins already have requests from you waiting.":
            return GitHubRequestResult(status="waiting", message=str(error))
        raise ToolError(str(error)) from error
    workspace_id = auth.external_id or ""
    if personal_case:
        root = runtime.settings.mcp.app_root_url
        if root is None:
            raise ToolError("GitHub linking is unavailable here.")
        async with runtime.session_factory.begin() as session:
            url = await mint_link(
                session,
                tenant_id=origin.tenant_id,
                account_id=origin.account_id,
                platform=origin.platform,
                platform_user_id=auth.platform_user_id,
                root_url=str(root),
            )
        card = (
            RequestCard(
                "This GitHub account doesn't have access to what this request needs.",
                "Use another GitHub account",
                ("Cancel request",),
            )
            if linked_login
            else RequestCard(
                "Your GitHub link stopped working. Link again?",
                "Link GitHub",
                ("Cancel request",),
            )
            if broken_link
            else RequestCard(
                "Link GitHub so Daimon can check that you have access to what this request needs.",
                "Link GitHub",
                ("Cancel request",),
            )
        )
        await deliver_private_request_card(
            runtime,
            tenant_id=origin.tenant_id,
            platform=origin.platform,
            workspace_id=workspace_id,
            request_id=request.id,
            recipient_account_id=origin.account_id,
            platform_user_id=auth.platform_user_id,
            card=card,
            link_url=url,
        )
        return GitHubRequestResult(
            status="waiting", message="I've asked an admin.", request_id=request.id
        )
    connected_names = (connected.repo_full_name,) if connected is not None else ()
    ability = required_ability
    if asker_is_admin:
        card = requester_card(
            request,
            connected_names=connected_names,
            asker_is_admin=True,
            needs_more_ability=grant is not None,
            ability=ability,
        )
        await deliver_private_request_card(
            runtime,
            tenant_id=origin.tenant_id,
            platform=origin.platform,
            workspace_id=workspace_id,
            request_id=request.id,
            recipient_account_id=origin.account_id,
            platform_user_id=auth.platform_user_id,
            card=card,
        )
        return GitHubRequestResult(status="waiting", message="", request_id=request.id)
    recipients = await _admin_recipients(
        runtime,
        tenant_id=origin.tenant_id,
        platform=origin.platform,
        requester_account_id=origin.account_id,
    )
    await deliver_shared_admin_card(
        runtime,
        tenant_id=origin.tenant_id,
        platform=origin.platform,
        workspace_id=workspace_id,
        request_id=request.id,
        admin_user_ids=[recipient.platform_user_id for recipient in recipients],
    )
    async with runtime.session_factory() as session:
        updated = await get_request(session, tenant_id=origin.tenant_id, request_id=request.id)
    if updated is None:
        raise ToolError("GitHub request disappeared.")
    status_card = requester_card(
        updated,
        connected_names=(),
        asker_is_admin=False,
        ability=ability,
    )
    await deliver_private_request_card(
        runtime,
        tenant_id=origin.tenant_id,
        platform=origin.platform,
        workspace_id=workspace_id,
        request_id=request.id,
        recipient_account_id=origin.account_id,
        platform_user_id=auth.platform_user_id,
        card=status_card,
    )
    return GitHubRequestResult(
        status="waiting", message="I've asked an admin.", request_id=request.id
    )


def register_github_request_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def request_github_access(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        repo_name: str,
        required_ability: Literal["read", "write"],
        remaining_work: str,
    ) -> GitHubRequestResult:
        """Ask for GitHub access needed to finish this person's current request.

        Use the active turn's origin ID and exactly one owner/repo. Give only the
        unfinished work, so the queued turn will not repeat completed steps.
        Never pass a repo name found in a DM or by the agent to another person.
        Return the message verbatim only when it is nonempty. A private card
        handles the admin's own request without a public status line.
        """
        return await request_github_access_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            repo_name=repo_name,
            required_ability=required_ability,
            remaining_work=remaining_work,
        )
