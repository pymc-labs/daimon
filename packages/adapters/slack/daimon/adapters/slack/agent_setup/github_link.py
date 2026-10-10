"""Private Slack browser links for connecting GitHub repos."""

from __future__ import annotations

from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_connect_cards import (
    ConnectCard,
    build_connect_card,
    connect_attachment,
)
from daimon.core.github_panel import CONNECT_COPY
from daimon.core.ma_identity import derive_tenant_uuid
from slack_sdk.web.async_client import AsyncWebClient

ACTION_BACK = "github_link__back"


async def send_link(
    client: AsyncWebClient,
    *,
    channel_id: str,
    thread_id: str | None = None,
    user_id: str,
    url: str,
    line: str = CONNECT_COPY,
    card: ConnectCard | None = None,
    app_root_url: str | None = None,
) -> None:
    if card is None:
        if app_root_url is None:
            raise ValueError("GitHub connection page is unavailable")
        card = build_connect_card(
            agent_name=None,
            identity_enabled=False,
            avatar_url=None,
            public_base_url=app_root_url,
        )
    kwargs: dict[str, Any] = {
        "channel": channel_id,
        "user": user_id,
        "text": line if line != CONNECT_COPY else "Connect GitHub",
        "attachments": [connect_attachment(url, card=card)],
    }
    if thread_id is not None:
        kwargs["thread_ts"] = thread_id
    await client.chat_postEphemeral(**kwargs)  # pyright: ignore[reportUnknownMemberType]


async def handle_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    actions = cast("list[dict[str, Any]]", payload.get("actions") or [])
    if not actions or actions[0].get("action_id") != ACTION_BACK:
        return
    team = cast("dict[str, Any]", payload.get("team") or {})
    channel = cast("dict[str, Any]", payload.get("channel") or {})
    user = cast("dict[str, Any]", payload.get("user") or {})
    client = await resolve_web_client(runtime, team_id=str(team.get("id") or ""))
    if client is None:
        return
    from daimon.adapters.slack.agent_setup.actions import github_home_view

    team_id = str(team.get("id") or "")
    user_id = str(user.get("id") or "")
    if not await resolve_is_admin(client, user_id=user_id):
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    await client.views_open(  # pyright: ignore[reportUnknownMemberType]
        trigger_id=str(payload.get("trigger_id") or ""),
        view=await github_home_view(
            runtime,
            tenant_id=tenant_id,
            meta=PanelMetadata(
                team_id=team_id,
                channel_id=str(channel.get("id") or user_id),
                view="github_home",
            ),
            user_id=user_id,
            is_admin=True,
        ),
    )
