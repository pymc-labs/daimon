"""The Block Kit button that opens a web app page."""

from __future__ import annotations

import uuid
from typing import Any

from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.web_links import OPEN_IN_WEB, TenantLinks, links_at, panel_links


def open_in_web_button(url: str) -> dict[str, Any]:
    """A url button; Slack opens it in the browser and a click needs no handler."""
    return {
        "type": "button",
        "action_id": "open_in_web",
        "text": {"type": "plain_text", "text": OPEN_IN_WEB},
        "url": url,
    }


async def web_links_at(
    runtime: SlackRuntime,
    tenant_id: uuid.UUID,
    *,
    admin: bool,
    channel_id: str | None,
    thread_id: str | None = None,
    agent_id: str | None = None,
) -> TenantLinks | None:
    """The links a card may offer: None with no web app or where `web_awareness` is Off at
    the card's place (a workspace-wide card passes the channel it was opened in)."""
    place = FeaturePlace.of(tenant_id, "slack", channel_id, thread_id, agent_id=agent_id)
    return await links_at(
        runtime.sessionmaker,
        runtime.settings,
        place,
        panel_links(runtime.settings.web, tenant_id, admin=admin),
    )


async def spend_url(
    runtime: SlackRuntime, tenant_id: uuid.UUID, channel_id: str | None
) -> str | None:
    """The workspace's spend page, or None with no web app or where it is Off. Any member
    may open it."""
    links = await web_links_at(runtime, tenant_id, admin=False, channel_id=channel_id)
    return None if links is None else links.spend()
