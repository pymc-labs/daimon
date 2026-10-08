"""Private /github connect command for one workspace agent."""

from __future__ import annotations

from typing import Any, cast

import anthropic
import httpx
import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.roster import load_roster
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import (
    CLIENT_AGENT_MESSAGE,
    ClientAgentConnectionError,
    mint_invitation,
    pending_update_for_agent,
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

    async def reply(message: str, *, blocks: list[dict[str, Any]] | None = None) -> None:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=message,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )

    try:
        parts = str(payload.get("text") or "").strip().split(maxsplit=1)
        if not parts or parts[0].casefold() != "connect":
            await reply("Run /github connect [agent].")
            return
        if not await resolve_is_admin(client, user_id=user_id):
            await reply("Ask an admin")
            return
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
                await reply("Choose an agent: /github connect [agent].")
                return
            target_name = roster.answering.name
            target_ma_id = roster.answering.ma_agent_id
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=target_ma_id)
        async with runtime.sessionmaker() as session:
            pending = await pending_update_for_agent(
                session, tenant_id=tenant_id, agent_id=agent_id
            )
        if pending is not None:
            await reply(CLIENT_AGENT_MESSAGE)
            return
        async with runtime.sessionmaker() as session:
            await require_app_eligible_agent(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=target_name
            )
        async with runtime.sessionmaker.begin() as session:
            principal = await get_or_create_platform_principal(
                session,
                tenant_id=tenant_id,
                platform="slack",
                external_id=user_id,
            )
            await set_role(session, principal.account_id, Role.ADMIN)
            token = await mint_invitation(
                session,
                tenant_id=tenant_id,
                requester_account_id=principal.account_id,
                requester_label=user_id,
                agent_id=agent_id,
                agent_name=target_name,
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
                reason="admin link minted",
            )
        message = (
            f"Opens GitHub to pick repos for {target_name}.\nNothing is shared until you confirm."
        )
        await reply(
            message,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": escape_mrkdwn(message)}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Connect GitHub"},
                            "url": f"{root}/oauth/github/connect/{token}",
                        }
                    ],
                },
            ],
        )
    except ClientAgentConnectionError:
        await reply(CLIENT_AGENT_MESSAGE)
    except (anthropic.APIError, SlackApiError, SQLAlchemyError, ValueError):
        log.exception("slack.github_connect.failed")
        await reply("GitHub setup is unavailable. Try again.")


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
            text=CLIENT_AGENT_MESSAGE,
        )


async def handle_github_cancel_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    response_url = str(payload.get("response_url") or "")
    if not response_url:
        return
    try:
        response = await runtime.http_client.post(
            response_url,
            json={"replace_original": True, "text": "Update cancelled."},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        log.exception("slack.github_connect.cancel_failed")
