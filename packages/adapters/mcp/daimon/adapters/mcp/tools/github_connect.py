"""Private, agent-bound GitHub setup from an active conversation."""

from __future__ import annotations

import uuid
from typing import Literal

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.setup_target import (
    origin_channel_id,
    require_turn_origin,
    resolve_setup_agent,
)
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.github_connect_cards import (
    CONNECT_GITHUB_EMOJI,
    connect_attachment,
    discord_embed_payload,
    resolve_connect_card,
)
from daimon.core.github_credentials import encrypt_token
from daimon.core.github_panel import requester_manages_agent
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access import (
    activate_agent,
    get_agent_mode,
    list_agent_grants,
    list_authorized_repos,
    stage_grant,
)
from daimon.core.stores.github_app_installations import get as get_app_installation
from daimon.core.stores.github_connect import (
    CLIENT_AGENT_MESSAGE,
    ClientAgentConnectionError,
    create_discord_connect_intent,
    has_saved_github_state,
    mint_invitation,
    record_connect_request,
    require_app_eligible_agent,
    revoke_discord_connect_intent,
    revoke_invitation,
    set_invitation_encrypted_token,
)
from daimon.core.stores.github_grant_proposals import consume, propose
from daimon.core.stores.security_audit import append_event
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class ConnectResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["sent", "proposed", "granted", "ask_admin", "delivery_failed", "client_agent"]
    message: str


async def _post_discord_connect_card(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    thread_id: str,
    requester_id: str,
    intent_id: uuid.UUID,
    agent_name: str,
) -> None:
    if auth.external_id is None:
        raise ToolError("GitHub setup requires a Discord server.")
    card = await resolve_connect_card(
        runtime.session_factory,
        runtime.settings,
        tenant_id=auth.tenant_id,
        platform="discord",
        workspace_id=auth.external_id,
        agent_name=agent_name,
    )
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Connect GitHub",
            emoji=CONNECT_GITHUB_EMOJI,
            style=discord.ButtonStyle.primary,
            custom_id=f"gh_connect:{requester_id}:{intent_id.hex}",
        )
    )
    async with rest_client(_require_bot_token(runtime)) as client:
        channel = await client.fetch_channel(int(thread_id))
        if not isinstance(channel, discord.Thread) or str(channel.guild.id) != auth.external_id:
            raise ToolError("GitHub setup requires the originating Discord thread.")
        await channel.send(
            embed=discord.Embed.from_dict(discord_embed_payload(card)),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def _post_slack_connect_card(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    thread_id: str,
    requester_id: str,
    url: str,
    agent_name: str,
) -> None:
    if auth.external_id is None:
        raise ToolError("GitHub setup requires a Slack workspace.")
    card = await resolve_connect_card(
        runtime.session_factory,
        runtime.settings,
        tenant_id=auth.tenant_id,
        platform="slack",
        workspace_id=auth.external_id,
        agent_name=agent_name,
    )
    client = await slack_web_client(runtime, team_id=auth.external_id)
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id,
        thread_ts=thread_id,
        user=requester_id,
        text="Connect GitHub",
        attachments=[connect_attachment(url, card=card)],
    )


async def _github_connect_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    agent_name: str | None = None,
    expected_ma_agent_id: str | None = None,
    requested_work: str | None = None,
    repo_name: str | None = None,
    required_ability: Literal["read", "write"] = "read",
    confirmed: bool = False,
) -> ConnectResult:
    if auth.platform not in ("discord", "slack") or auth.platform_user_id is None:
        raise ToolError("GitHub setup from chat is available in Discord and Slack.")
    if auth.is_external:
        raise ToolError("GitHub setup is for members of this workspace.")
    root = runtime.settings.mcp.app_root_url
    config = runtime.settings.github_app
    if runtime.fernet is None:
        raise ToolError("GitHub setup is unavailable on this server.")
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
                session,
                tenant_id=auth.tenant_id,
                agent_id=agent_id,
                agent_name=agent.name,
                switch_saved_key=True,
            )
        except ClientAgentConnectionError:
            return ConnectResult(status="client_agent", message=CLIENT_AGENT_MESSAGE)
        account = await get_account(session, auth.account_id)
        if account is None or account.tenant_id != auth.tenant_id or account.is_external:
            raise ToolError("GitHub setup is unavailable for this account.")
        is_admin = account.role == Role.ADMIN and auth.is_admin
        # A channel admin may connect their own repos for an agent they manage,
        # by their groups as the platform reports them now, not as cached.
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
        if repo_name is not None:
            requested_repo = repo_name.strip()
            if requested_repo.count("/") != 1 or any(c.isspace() for c in requested_repo):
                raise ToolError("Name one repo as owner/repo.")
            question = (
                f"Give {agent.name} "
                f"{'read and change' if required_ability == 'write' else 'read'} "
                f"access to {requested_repo}?"
            )
            if not confirmed:
                await propose(
                    session,
                    tenant_id=auth.tenant_id,
                    account_id=auth.account_id,
                    platform_user_id=auth.platform_user_id,
                    platform=auth.platform,
                    thread_id=origin.thread_id,
                    agent_id=agent_id,
                    repo_name=requested_repo,
                    ability=required_ability,
                    origin_id=origin.id,
                )
                return ConnectResult(status="proposed", message=question)
            if not await consume(
                session,
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                platform_user_id=auth.platform_user_id,
                platform=auth.platform,
                thread_id=origin.thread_id,
                agent_id=agent_id,
                repo_name=requested_repo,
                ability=required_ability,
                origin_id=origin.id,
                origin_created_at=origin.created_at,
            ):
                return ConnectResult(
                    status="proposed",
                    message=f"Please confirm in a new message: {question}",
                )
            mode = await get_agent_mode(session, tenant_id=auth.tenant_id, agent_id=agent_id)
            can_activate = mode == "app" or not await has_saved_github_state(
                session, tenant_id=auth.tenant_id, agent_id=agent_id
            )
            if can_activate:
                repos = await list_authorized_repos(
                    session, tenant_id=auth.tenant_id, agent_id=agent_id
                )
                repo = next(
                    (
                        row
                        for row in repos
                        if row.repo_full_name.casefold() == requested_repo.casefold()
                        and row.status == "active"
                    ),
                    None,
                )
                if repo is not None:
                    installation = await get_app_installation(
                        session, installation_id=repo.installation_id
                    )
                    grantable = (
                        installation is not None
                        and installation.suspended_at is None
                        and repo.repo_full_name in installation.repo_full_names
                        and (is_admin or repo.scope_agent_id == agent_id)
                    )
                    if grantable:
                        if required_ability == "write" and repo.max_access != "write":
                            raise ToolError(
                                "GitHub confirmed read access only. "
                                "Ask for write access in Connect GitHub."
                            )
                        existing = next(
                            (
                                row
                                for row in await list_agent_grants(
                                    session, tenant_id=auth.tenant_id, agent_id=agent_id
                                )
                                if row.repo_id == repo.repo_id
                            ),
                            None,
                        )
                        ability: Literal["read", "write"] = (
                            "write"
                            if required_ability == "write"
                            or (existing is not None and existing.ceiling_access == "write")
                            else "read"
                        )
                        await stage_grant(
                            session,
                            tenant_id=auth.tenant_id,
                            agent_id=agent_id,
                            repo_id=repo.repo_id,
                            baseline_access=ability,
                            ceiling_access=ability,
                            granted_by_account_id=auth.account_id,
                            mount_path=existing.mount_path if existing else None,
                            is_working_repo=existing.is_working_repo if existing else False,
                        )
                        if mode == "legacy":
                            await activate_agent(
                                session,
                                tenant_id=auth.tenant_id,
                                agent_id=agent_id,
                                changed_by_account_id=auth.account_id,
                            )
                        return ConnectResult(
                            status="granted",
                            message=f"{agent.name} has {ability} access to {repo.repo_full_name}.",
                        )
        intent_id: uuid.UUID | None = None
        token: str | None = None
        if auth.platform == "discord":
            intent_id = await create_discord_connect_intent(
                session,
                tenant_id=auth.tenant_id,
                requester_account_id=auth.account_id,
                requester_platform_user_id=auth.platform_user_id,
                agent_id=agent_id,
                agent_name=agent.name,
                parent_channel_id=origin.parent_channel_id,
                thread_id=origin.thread_id,
                origin_ma_agent_id=origin.responder_ma_agent_id,
                origin_responder_name=origin.responder_name,
                requested_work=requested_work,
                agent_ma_id=str(agent.id),
            )
        else:
            token = await mint_invitation(
                session,
                tenant_id=auth.tenant_id,
                requester_account_id=auth.account_id,
                requester_label=auth.platform_user_id,
                requester_platform_user_id=auth.platform_user_id,
                agent_id=agent_id,
                agent_name=agent.name,
                agent_ma_id=str(agent.id),
                agent_manager_verified=manages,
                origin_platform=auth.platform,
                origin_parent_channel_id=origin.parent_channel_id,
                origin_thread_id=origin.thread_id,
                origin_ma_agent_id=origin.responder_ma_agent_id,
                origin_responder_name=origin.responder_name,
                requested_work=requested_work,
            )
            await set_invitation_encrypted_token(
                session, token=token, encrypted_token=encrypt_token(runtime.fernet, token)
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
            reason=(
                ("admin" if is_admin else "channel admin")
                + (" connect button posted" if intent_id else " link minted")
            ),
        )
    try:
        if auth.platform == "discord":
            assert intent_id is not None
            await _post_discord_connect_card(
                runtime,
                auth,
                thread_id=origin.thread_id,
                requester_id=auth.platform_user_id,
                intent_id=intent_id,
                agent_name=agent.name,
            )
        else:
            assert token is not None
            await _post_slack_connect_card(
                runtime,
                auth,
                channel_id=origin.parent_channel_id,
                thread_id=origin.thread_id,
                requester_id=auth.platform_user_id,
                url=f"{root}/oauth/github/connect/{token}",
                agent_name=agent.name,
            )
    except Exception:
        async with runtime.session_factory.begin() as session:
            if intent_id is not None:
                await revoke_discord_connect_intent(session, intent_id=intent_id)
            elif token is not None:
                await revoke_invitation(session, token=token)
        return ConnectResult(
            status="delivery_failed",
            message="I couldn't show the GitHub connection button. Try again.",
        )
    return ConnectResult(
        status="sent",
        message=(
            "The Connect GitHub button is posted and speaks for itself. "
            "Don't announce or describe it; reply nothing more about it."
        ),
    )


def register_github_connect_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def github_connect(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        agent_name: str | None = None,
        expected_ma_agent_id: str | None = None,
        requested_work: str | None = None,
        repo_name: str | None = None,
        required_ability: Literal["read", "write"] = "read",
        confirmed: bool = False,
    ) -> ConnectResult:
        """Give this agent token access to more repos, such as a private GitHub repo.

        Show a single-use connection button bound to the selected agent to a
        server admin, or to a channel admin who manages that agent; repos
        connected this way are for that agent only. Other members get Ask an
        admin and a recorded request. Never repeat a private
        link in a shared reply. For a setup target, pass its current name and
        MA id from turn_controls; otherwise this defaults to the responder.
        When GitHub access interrupted a task, pass a short restatement as
        requested_work so the task resumes after connection. Leave it empty
        when someone only asks to connect GitHub.
        For 'give this agent access to owner/repo', first call with repo_name and
        confirmed=false. Ask the returned question in the thread. Only after
        that person answers yes in a later turn, call again with confirmed=true.
        Use write only if asked. A connected repo is granted
        directly when this person may grant it. Otherwise the single Connect
        GitHub link lets them connect that repo in a browser. Connecting repos
        gives token access; it does not put them all in the filesystem.
        The posted button speaks for itself: do not announce or describe it.
        """
        return await _github_connect_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            agent_name=agent_name,
            expected_ma_agent_id=expected_ma_agent_id,
            requested_work=requested_work,
            repo_name=repo_name,
            required_ability=required_ability,
            confirmed=confirmed,
        )
