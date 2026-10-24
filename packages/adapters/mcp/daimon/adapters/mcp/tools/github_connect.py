"""Private, agent-bound GitHub setup from an active conversation."""

from __future__ import annotations

import uuid
from typing import Literal

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._words import space_noun
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
from daimon.adapters.mcp.tools.teams._direct import send_card_to_caller
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.github_connect_cards import (
    CONNECT_GITHUB_EMOJI,
    connect_attachment,
    discord_embed_payload,
    resolve_connect_card,
    teams_connect_card,
)
from daimon.core.github_credentials import encrypt_token
from daimon.core.github_panel import requester_manages_agent
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access import (
    list_agent_grants,
    list_authorized_repos,
    stage_grant,
)
from daimon.core.stores.github_app_installations import get as get_app_installation
from daimon.core.stores.github_connect import (
    PinnedAgentConnectionError,
    create_discord_connect_intent,
    mint_invitation,
    record_connect_request,
    require_app_eligible_agent,
    revoke_discord_connect_intent,
    revoke_invitation,
    set_invitation_encrypted_token,
)
from daimon.core.stores.github_installation_coverage import installation_covers_repo
from daimon.core.stores.security_audit import append_event
from daimon.core.teams_threads import conversation_of
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
    requested_repo: str | None = None,
) -> None:
    if auth.external_id is None:
        raise ToolError("GitHub setup requires a server.")
    card = await resolve_connect_card(
        runtime.session_factory,
        runtime.settings,
        tenant_id=auth.tenant_id,
        platform="discord",
        workspace_id=auth.external_id,
        agent_name=agent_name,
        requested_repo=requested_repo,
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
            raise ToolError("GitHub setup requires the originating thread.")
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
    requested_repo: str | None = None,
) -> None:
    if auth.external_id is None:
        raise ToolError("GitHub setup only works from a chat.")
    card = await resolve_connect_card(
        runtime.session_factory,
        runtime.settings,
        tenant_id=auth.tenant_id,
        platform="slack",
        workspace_id=auth.external_id,
        agent_name=agent_name,
        requested_repo=requested_repo,
    )
    client = await slack_web_client(runtime, team_id=auth.external_id)
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id,
        thread_ts=thread_id,
        user=requester_id,
        text="Connect GitHub",
        attachments=[connect_attachment(url, card=card)],
    )


async def _post_teams_connect_card(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    url: str,
    agent_name: str,
    requested_repo: str | None = None,
) -> None:
    # Teams has no message only its sender sees, and the link is a bearer link:
    # it goes to the requester's 1:1 chat, never the channel.
    card = await resolve_connect_card(
        runtime.session_factory,
        runtime.settings,
        tenant_id=auth.tenant_id,
        platform="teams",
        workspace_id=auth.external_id or "",
        agent_name=agent_name,
        requested_repo=requested_repo,
    )
    await send_card_to_caller(
        runtime, auth, conversation_id=channel_id, card=teams_connect_card(url, card=card)
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
    if auth.platform not in ("discord", "slack", "teams") or auth.platform_user_id is None:
        raise ToolError("GitHub setup from chat isn't available here.")
    root = runtime.settings.mcp.app_root_url
    config = runtime.settings.github_app
    if runtime.fernet is None:
        raise ToolError(f"GitHub setup is unavailable on this {space_noun(auth.platform)}.")
    if root is None or not all(
        (config.app_id, config.app_slug, config.private_key, config.client_id, config.client_secret)
    ):
        raise ToolError(f"GitHub setup is unavailable on this {space_noun(auth.platform)}.")
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
    requested_repo: str | None = None
    async with runtime.session_factory.begin() as session:
        try:
            await require_app_eligible_agent(
                session,
                tenant_id=auth.tenant_id,
                agent_id=agent_id,
                agent_name=agent.name,
            )
        except PinnedAgentConnectionError as exc:
            return ConnectResult(status="client_agent", message=str(exc))
        account = await get_account(session, auth.account_id)
        if account is None or account.tenant_id != auth.tenant_id:
            raise ToolError("GitHub setup is unavailable for this account.")
        is_admin = account.role == Role.ADMIN and auth.is_admin
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
            repo_label = repo_name.strip() if repo_name else "GitHub"
            return ConnectResult(
                status="ask_admin",
                message=(
                    f"Only an admin can connect repos to {agent.name}. Tell the person in one "
                    f"line to ask an admin to say here: connect {repo_label} to {agent.name}."
                ),
            )
        if repo_name is not None:
            requested_repo = repo_name.strip()
            if requested_repo.count("/") != 1 or any(c.isspace() for c in requested_repo):
                raise ToolError("Name one repo as owner/repo.")
            # A clear ask is enough (Derick, 2026-10-10): no separate "say yes" turn.
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
                    and installation_covers_repo(installation, repo.repo_full_name)
                    and (is_admin or repo.scope_agent_id == agent_id)
                )
                if grantable:
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
                    try:
                        await stage_grant(
                            session,
                            tenant_id=auth.tenant_id,
                            agent_id=agent_id,
                            repo_id=repo.repo_id,
                            baseline_access="write",
                            ceiling_access="write",
                            granted_by_account_id=auth.account_id,
                            mount_path=existing.mount_path if existing else None,
                            is_working_repo=existing.is_working_repo if existing else False,
                            agent_name=agent.name,
                        )
                    except ValueError as error:
                        raise ToolError(str(error)) from error
                    return ConnectResult(
                        status="granted",
                        message=(
                            f"{agent.name} can now read and change {repo.repo_full_name} "
                            "from the next message. To undo, ask me to remove it."
                        ),
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
                requested_repo=requested_repo,
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
                requested_repo=requested_repo,
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
            reason=("admin" + (" connect button posted" if intent_id else " link minted")),
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
                requested_repo=repo_name.strip() if repo_name else None,
            )
        elif auth.platform == "teams":
            assert token is not None
            await _post_teams_connect_card(
                runtime,
                auth,
                channel_id=origin.parent_channel_id,
                url=f"{root}/oauth/github/connect/{token}",
                agent_name=agent.name,
                requested_repo=repo_name.strip() if repo_name else None,
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
                requested_repo=repo_name.strip() if repo_name else None,
            )
    except Exception:
        async with runtime.session_factory.begin() as session:
            if intent_id is not None:
                await revoke_discord_connect_intent(session, intent_id=intent_id)
            elif token is not None:
                await revoke_invitation(session, token=token)
        return ConnectResult(
            status="delivery_failed",
            message=(
                "I couldn't send the Connect GitHub link: connecting GitHub works only "
                "for people in the organisation that added Daimon."
                if auth.platform == "teams" and origin.sender_foreign
                else "I couldn't send the Connect GitHub link to your 1:1 chat with me. "
                "Open a chat with me, then try again. A chat can't be opened "
                "with people from another organisation."
                if auth.platform == "teams"
                else "I couldn't show the GitHub connection button. Try again."
            ),
        )
    if auth.platform == "teams":
        lead = f"I can't see {repo_name.strip()} yet. " if repo_name else ""
        where, line = (
            ("is posted in this chat", "Use the Connect GitHub card.")
            if conversation_of(origin.thread_id) == origin.parent_channel_id
            else (
                "went to the requester's 1:1 chat with you, not here,",
                "I sent the Connect GitHub link to our 1:1 chat.",
            )
        )
        return ConnectResult(
            status="sent",
            message=(
                f"The Connect GitHub card {where} and already lists the page steps. "
                f"Reply with at most this line: {lead}{line} "
                "Never say done, connected, pending or a tool name."
            ),
        )
    say = f"I can't see {repo_name.strip()} yet." if repo_name else "Use the button below."
    return ConnectResult(
        status="sent",
        message=(
            "The Connect GitHub button is posted below your reply. The card already says "
            "nothing is connected yet and lists the page steps, so don't repeat them. "
            f"Reply with at most this line, or nothing if connecting was all they asked for: "
            f"{say} Never say done, connected, pending or a tool name."
        ),
    )


def register_github_connect_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def github_connect(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str = "",
        agent_name: str | None = None,
        expected_ma_agent_id: str | None = None,
        requested_work: str | None = None,
        repo_name: str | None = None,
        confirmed: bool = False,
    ) -> ConnectResult:
        """Give this agent token access to more repos, such as a private GitHub repo.

        Use this when someone asks to let an agent read a private repository
        at github.com/owner/repo. Show a single-use
        connection button bound to the selected agent to an admin; repos
        connected this way are for that agent only. Other members get Ask an
        admin and a recorded request. The card may go to the requester's
        private chat instead; the result says so. Never repeat a private link in a
        shared reply. For a setup
        target, pass its current name and MA id from turn_controls; otherwise
        this defaults to the responder.
        When GitHub access interrupted a task, pass a short restatement as
        requested_work so the task resumes after connection. Leave it empty
        when someone only asks to connect GitHub.
        For a clear 'give this agent access to owner/repo', call with repo_name
        right away; don't ask for a yes first. Ask only if the repo or agent is
        unclear. confirmed is ignored. A connected repo is granted
        directly when this person may grant it. Otherwise the single Connect
        GitHub link lets them connect that repo in a browser. Connecting repos
        gives token access; it does not put them all in the filesystem.
        After it posts, nothing is connected until the person saves on the page.
        The card carries the steps: never repeat them, and never say done or connected.
        """
        return await _github_connect_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            agent_name=agent_name,
            expected_ma_agent_id=expected_ma_agent_id,
            requested_work=requested_work,
            repo_name=repo_name,
            required_ability="write",
            confirmed=confirmed,
        )
