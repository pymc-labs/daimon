"""Private Slack browser links for connecting GitHub repos."""

from __future__ import annotations

from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.panel_views import build_github_home_view
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_connect_cards import connect_button_blocks
from daimon.core.github_panel import CONNECT_COPY
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_connected_repos import summary
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
) -> None:
    blocks = connect_button_blocks(url, line)
    kwargs: dict[str, Any] = {
        "channel": channel_id,
        "user": user_id,
        "text": "Connect GitHub",
        "blocks": blocks,
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
    from daimon.adapters.slack.agent_setup.actions import github_pending_url

    team_id = str(team.get("id") or "")
    user_id = str(user.get("id") or "")
    if not await resolve_is_admin(client, user_id=user_id):
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session:
        home = await summary(session, tenant_id=tenant_id)
    pending_url = await github_pending_url(runtime, tenant_id=tenant_id, user_id=user_id)
    await client.views_open(  # pyright: ignore[reportUnknownMemberType]
        trigger_id=str(payload.get("trigger_id") or ""),
        view=build_github_home_view(
            PanelMetadata(
                team_id=team_id,
                channel_id=str(channel.get("id") or user_id),
                view="github_home",
            ),
            connected_count=home.count,
            owners=home.owners,
            agent_count=home.agent_count,
            pending_url=pending_url,
        ),
    )
