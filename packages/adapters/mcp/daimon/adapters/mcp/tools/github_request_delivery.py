"""GitHub request cards delivered where the person asked."""

from __future__ import annotations

import uuid

import discord
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import rest_client
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core.github_request_cards import RequestCard
from daimon.core.stores.github_access_requests import (
    get_delivery,
    lock_delivery_slot,
    lookup_request,
    record_delivery,
)
from slack_sdk.errors import SlackApiError

_DECISIONS = {
    "Add repo": "approve",
    "Allow": "approve",
    "Connect and add": "connect",
    "Decline": "decline",
    "Hide for me": "hide",
    "Cancel request": "cancel",
}
_LINK_LABELS = frozenset(
    {"Link GitHub", "Use another GitHub account", "Try again", "Get a new link"}
)


def _card_parts(text: str) -> tuple[str, str]:
    title, _, detail = text.partition("\n")
    return title, detail


def _discord_embed(card: RequestCard) -> discord.Embed:
    title, detail = _card_parts(card.text)
    embed = discord.Embed(
        title=title[:256],
        color=0xFEE75C if card.primary else 0x5865F2,
    )
    if detail:
        if detail.startswith("Can: "):
            embed.add_field(name="Can", value=detail.removeprefix("Can: "), inline=False)
        else:
            embed.add_field(name="Details", value=detail[:1024], inline=False)
    embed.set_footer(text="GitHub on Daimon")
    return embed


def _discord_view(
    card: RequestCard, *, request_id: uuid.UUID, link_url: str | None
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label in ((card.primary,) if card.primary else ()) + card.secondary:
        if label in _LINK_LABELS and link_url:
            view.add_item(
                discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.primary,
                    custom_id=f"github_request:{request_id}:link",
                )
            )
        elif label in _DECISIONS:
            view.add_item(
                discord.ui.Button(
                    label=label,
                    custom_id=f"github_request:{request_id}:{_DECISIONS[label]}",
                    style=(
                        discord.ButtonStyle.primary
                        if label == card.primary
                        else discord.ButtonStyle.secondary
                    ),
                )
            )
    return view


def _slack_blocks(
    card: RequestCard, *, request_id: uuid.UUID, link_url: str | None
) -> list[dict[str, object]]:
    buttons: list[dict[str, object]] = []
    for label in ((card.primary,) if card.primary else ()) + card.secondary:
        if label in _LINK_LABELS and link_url:
            buttons.append(
                {
                    "type": "button",
                    "action_id": "github_request__link",
                    "url": link_url,
                    "text": {"type": "plain_text", "text": label},
                }
            )
        elif label in _DECISIONS:
            buttons.append(
                {
                    "type": "button",
                    "action_id": "github_request__decision",
                    "value": f"{request_id}:{_DECISIONS[label]}",
                    "text": {"type": "plain_text", "text": label},
                }
            )
    title, detail = _card_parts(card.text)
    blocks: list[dict[str, object]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}}
    ]
    if detail:
        blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": detail}]})
    if buttons:
        blocks.append({"type": "divider"})
        blocks.append({"type": "actions", "elements": buttons})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "GitHub on Daimon"}]})
    return blocks


async def deliver_private_request_card(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    workspace_id: str,
    request_id: uuid.UUID,
    recipient_account_id: uuid.UUID,
    platform_user_id: str,
    card: RequestCard,
    link_url: str | None = None,
) -> bool:
    """Post in the originating thread or ephemerally to this recipient."""
    async with runtime.session_factory.begin() as session:
        await lock_delivery_slot(
            session, request_id=request_id, recipient_account_id=recipient_account_id
        )
        delivery = await get_delivery(
            session,
            tenant_id=tenant_id,
            request_id=request_id,
            recipient_account_id=recipient_account_id,
        )
        request = await lookup_request(session, request_id=request_id)
        if request is None or request.tenant_id != tenant_id:
            return False
        if delivery is not None and delivery.dismissed_at is not None:
            return False
        try:
            if platform == "discord":
                if runtime.settings.discord is None:
                    return False
                token = runtime.settings.discord.bot_token.get_secret_value()
                async with rest_client(token) as client:
                    channel = await client.fetch_channel(int(request.thread_id))
                    if not isinstance(channel, discord.Thread):
                        return False
                    view = _discord_view(card, request_id=request_id, link_url=link_url)
                    if delivery is not None and delivery.message_id is not None:
                        message = channel.get_partial_message(int(delivery.message_id))
                        await message.edit(
                            content=None,
                            embed=_discord_embed(card),
                            view=view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return True
                    message = await channel.send(
                        embed=_discord_embed(card),
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    message_id = str(message.id)
            elif platform == "slack":
                client = await slack_web_client(runtime, team_id=workspace_id)
                blocks = _slack_blocks(card, request_id=request_id, link_url=link_url)
                sent = await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=request.parent_channel_id,
                    thread_ts=request.thread_id,
                    user=platform_user_id,
                    text=card.text,
                    blocks=blocks,
                )
                message_id = str(sent.get("message_ts") or sent.get("ts") or uuid.uuid4())
            else:
                return False
        except (discord.HTTPException, SlackApiError, ValueError):
            return False
        if not message_id:
            return False
        return await record_delivery(
            session,
            tenant_id=tenant_id,
            request_id=request_id,
            recipient_account_id=recipient_account_id,
            platform_user_id=platform_user_id,
            message_id=message_id,
        )
