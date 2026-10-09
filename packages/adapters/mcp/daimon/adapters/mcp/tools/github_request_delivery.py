"""GitHub request cards delivered where the person asked."""

from __future__ import annotations

import uuid

import discord
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import rest_client
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core.github_request_cards import RequestCard, slack_mrkdwn_escape
from daimon.core.stores.github_access_requests import (
    get_delivery,
    lock_delivery_slot,
    lock_shared_card_slot,
    lookup_request,
    record_delivery,
    record_shared_card,
    shared_card_mention_allowed,
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


async def deliver_shared_admin_card(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    platform: str,
    workspace_id: str,
    admin_user_ids: list[str],
) -> bool:
    """Post one repo-free card, with at most three admin mention posts per thread per hour."""
    async with runtime.session_factory.begin() as session:
        request = await lookup_request(session, request_id=request_id)
        if request is None or request.tenant_id != tenant_id:
            return False
        await lock_shared_card_slot(session, request=request)
        request = await lookup_request(session, request_id=request_id)
        if request is None or request.status not in ("open", "waiting_github"):
            return False
        mention = (
            request.admin_card_message_id is None
            and bool(admin_user_ids)
            and await shared_card_mention_allowed(session, request=request)
        )
        title = f"{request.agent_name} needs GitHub access to continue."
        try:
            if platform == "discord":
                if runtime.settings.discord is None:
                    return False
                ids = [int(user_id) for user_id in admin_user_ids] if mention else []
                content = " ".join(f"<@{user_id}>" for user_id in ids) if mention else None
                view = discord.ui.View(timeout=None)
                view.add_item(
                    discord.ui.Button(
                        label="Review", custom_id=f"github_request:{request_id}:review"
                    )
                )
                async with rest_client(
                    runtime.settings.discord.bot_token.get_secret_value()
                ) as client:
                    channel = await client.fetch_channel(int(request.thread_id))
                    if not isinstance(channel, discord.Thread):
                        return False
                    if request.admin_card_message_id:
                        message = channel.get_partial_message(int(request.admin_card_message_id))
                        await message.edit(
                            content=content,
                            embed=discord.Embed(title=title),
                            view=view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        message_id = request.admin_card_message_id
                    else:
                        sent = await channel.send(
                            content=content,
                            embed=discord.Embed(title=title),
                            view=view,
                            allowed_mentions=discord.AllowedMentions(
                                users=[discord.Object(id=i) for i in ids],
                                everyone=False,
                                roles=False,
                                replied_user=False,
                            ),
                        )
                        message_id = str(sent.id)
            elif platform == "slack":
                client = await slack_web_client(runtime, team_id=workspace_id)
                mentions = (
                    " ".join(f"<@{user_id}>" for user_id in admin_user_ids) if mention else ""
                )
                text = f"{slack_mrkdwn_escape(title)} {mentions}".strip()
                blocks = [
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "github_request__review",
                                "value": str(request_id),
                                "text": {"type": "plain_text", "text": "Review"},
                            }
                        ],
                    },
                ]
                if request.admin_card_message_id:
                    await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
                        channel=request.parent_channel_id,
                        ts=request.admin_card_message_id,
                        text=text,
                        blocks=blocks,
                    )
                    message_id = request.admin_card_message_id
                else:
                    sent = await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                        channel=request.parent_channel_id,
                        thread_ts=request.thread_id,
                        text=text,
                        blocks=blocks,
                    )
                    raw_message_id: object = sent.get("ts")
                    if not isinstance(raw_message_id, str):
                        return False
                    message_id = raw_message_id
            else:
                return False
        except (discord.HTTPException, SlackApiError, ValueError, KeyError):
            return False
        await record_shared_card(session, request=request, message_id=message_id, mentioned=mention)
        return True


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
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{slack_mrkdwn_escape(title)}*"}}
    ]
    if detail:
        blocks.append(
            {"type": "section", "fields": [{"type": "mrkdwn", "text": slack_mrkdwn_escape(detail)}]}
        )
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
                    # A thread message is public to its readers. Only status
                    # text with no requested repo name belongs on this card.
                    if any(name.casefold() in card.text.casefold() for name in request.repo_names):
                        embed = discord.Embed(
                            title="GitHub request",
                            description="Only the requester can use this card.",
                        )
                    else:
                        title, detail = _card_parts(card.text)
                        embed = discord.Embed(title=title, description=detail or None)
                    if delivery is not None and delivery.message_id is not None:
                        message = channel.get_partial_message(int(delivery.message_id))
                        await message.edit(
                            content=None,
                            embed=embed,
                            view=view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return True
                    message = await channel.send(
                        embed=embed,
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
