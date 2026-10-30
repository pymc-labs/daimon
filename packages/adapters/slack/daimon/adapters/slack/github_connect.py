"""Private /github connect command for one workspace agent."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import anthropic
import httpx
import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.blockkit import notice_fields, notice_update_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_connect_cards import connect_attachment, resolve_connect_card
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.github_panel import can_manage_agent_github
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.native_admins import AdminCaller
from daimon.core.roster import load_roster
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import (
    PINNED_AGENT_MESSAGE,
    PinnedAgentConnectionError,
    mint_invitation,
    require_app_eligible_agent,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.security_audit import append_event
from slack_sdk.errors import SlackApiError
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()
ACTION_UPDATE = "github_connect_update"
ACTION_CANCEL = "github_connect_cancel"


async def handle_github_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    team_id = str(payload.get("team_id") or "")
    user_id = str(payload.get("user_id") or "")
    channel_id = str(payload.get("channel_id") or "")
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    async def reply(
        message: str, *, attachments: list[dict[str, Any]] | None = None, error: bool = True
    ) -> None:
        # The connect card is itself an attachment; a bare message becomes a notice card.
        fields: dict[str, Any] = (
            {"text": message, "attachments": attachments}
            if attachments
            else notice_fields(message, neutral=not error)
        )
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            **fields,
            unfurl_links=False,
            unfurl_media=False,
        )

    try:
        parts = str(payload.get("text") or "").strip().split(maxsplit=1)
        if not parts or parts[0].casefold() != "connect":
            await reply("Run /github connect [agent].", error=False)
            return
        admin = await resolve_is_admin(client, user_id=user_id, host_team_id=team_id)
        user_info = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]
        user = cast(dict[str, Any], user_info.get("user") or {})
        if user.get("team_id") != team_id or user.get("is_stranger"):
            await reply("GitHub setup is for members of this workspace.")
            return
        root = runtime.settings.mcp.app_root_url
        config = runtime.settings.github_app
        if root is None or not all(
            (
                config.app_id,
                config.app_slug,
                config.private_key,
                config.client_id,
                config.client_secret,
            )
        ):
            await reply("GitHub setup is unavailable.")
            return
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        if len(parts) > 1:
            name = parts[1].strip()
            agents = await list_agents_by_tenant(runtime.anthropic, tenant_id=tenant_id)
            matches = [row for row in agents if row.name.casefold() == name.casefold()]
            if len(matches) != 1:
                await reply("Choose one current agent.")
                return
            target_name = matches[0].name
            target_ma_id = str(matches[0].id)
        else:
            async with runtime.sessionmaker() as session:
                roster = await load_roster(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    platform="slack",
                    channel_id=channel_id,
                    thread_id=None,
                    default=runtime.deployment_default,
                )
            if roster.answering is None:
                await reply("Choose an agent: /github connect [agent].", error=False)
                return
            target_name = roster.answering.name
            target_ma_id = roster.answering.ma_agent_id
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=target_ma_id)
        if not admin and not await _owns_target(
            runtime, tenant_id=tenant_id, user_id=user_id, ma_agent_id=target_ma_id
        ):
            await reply(
                "Only an admin or an owner of this agent can connect repos. Ask one to run /github."
            )
            return
        async with runtime.sessionmaker() as session:
            await require_app_eligible_agent(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                agent_name=target_name,
            )
        async with runtime.sessionmaker.begin() as session:
            principal = await get_or_create_platform_principal(
                session,
                tenant_id=tenant_id,
                platform="slack",
                external_id=user_id,
            )
            if admin:
                await set_role(session, principal.account_id, Role.ADMIN)
            token = await mint_invitation(
                session,
                tenant_id=tenant_id,
                requester_account_id=principal.account_id,
                requester_label=user_id,
                requester_platform_user_id=user_id,
                agent_id=agent_id,
                agent_name=target_name,
                agent_ma_id=target_ma_id,
                # An owner is never promoted: the link is for this one agent and the
                # owner is checked again when it is saved.
                agent_manager_verified=not admin,
                origin_platform="slack",
                origin_parent_channel_id=channel_id,
                encrypted_origin_followup=(
                    encrypt_token(
                        build_multifernet(
                            tuple(key.get_secret_value() for key in runtime.settings.crypto.keys)
                        ),
                        str(payload["response_url"]),
                    )
                    if payload.get("response_url")
                    else None
                ),
                origin_followup_expires_at=(
                    datetime.now(UTC) + timedelta(minutes=30)
                    if payload.get("response_url")
                    else None
                ),
            )
            await append_event(
                session,
                tenant_id=tenant_id,
                account_id=principal.account_id,
                agent_id=agent_id,
                platform="slack",
                platform_user_id=user_id,
                tool_name="github_connect",
                operation="github_connect",
                outcome="allowed",
                reason="admin link minted" if admin else "owner link minted",
            )
        card = await resolve_connect_card(
            runtime.sessionmaker,
            runtime.settings,
            tenant_id=tenant_id,
            platform="slack",
            workspace_id=team_id,
            agent_name=target_name,
        )
        await reply(
            "Connect GitHub",
            attachments=[connect_attachment(f"{root}/oauth/github/connect/{token}", card=card)],
        )
    except PinnedAgentConnectionError as exc:
        await reply(str(exc))
    except (anthropic.APIError, SlackApiError, SQLAlchemyError, ValueError):
        log.exception("slack.github_connect.failed")
        await reply("GitHub setup is unavailable. Try again.")


async def _owns_target(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, user_id: str, ma_agent_id: str
) -> bool:
    """Whether a workspace member who is not an admin owns the agent right now.

    A defaults-managed agent has no owner who counts, and an agent that can't be
    read is not one to connect.
    """
    live = await find_agent_by_derived_uuid(
        runtime.anthropic,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id),
    )
    if live is None or live.id != ma_agent_id:
        return False
    async with runtime.sessionmaker() as session:
        return await can_manage_agent_github(
            session,
            tenant_id=tenant_id,
            platform="slack",
            caller=AdminCaller(platform_user_id=user_id),
            agent_names=(),
            ma_agent_id=ma_agent_id,
            is_daimon_managed=live.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
            default=runtime.deployment_default,
        )


async def handle_github_update_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Old update buttons cannot retire a saved key from chat."""
    team = cast(dict[str, Any], payload.get("team") or {})
    user = cast(dict[str, Any], payload.get("user") or {})
    channel = cast(dict[str, Any], payload.get("channel") or {})
    client = await resolve_web_client(runtime, team_id=str(team.get("id") or ""))
    if client is not None:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=str(channel.get("id") or ""),
            user=str(user.get("id") or ""),
            **notice_fields(PINNED_AGENT_MESSAGE),
        )


async def handle_github_cancel_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    response_url = str(payload.get("response_url") or "")
    if not response_url:
        return
    try:
        response = await runtime.http_client.post(
            response_url,
            json={
                "replace_original": True,
                **notice_update_fields("Update cancelled.", neutral=True),
            },
        )
        response.raise_for_status()
    except httpx.HTTPError:
        log.exception("slack.github_connect.cancel_failed")
