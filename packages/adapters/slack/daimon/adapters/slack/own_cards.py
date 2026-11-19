"""Presses on the cards Daimon answers itself: a host call's Approve/Deny, a switch's Confirm.

Posted from the MCP process (`tools._own_card`); the press re-verifies the clicker, the
message and the tenant in core (`host_calls.press_host_call`, `switch_cards.press_switch_card`),
the same guards as on Discord.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.core import host_calls, switch_cards
from daimon.core.features.snapshot import deployment_on
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.posted_controls import PostedCard
from daimon.core.posted_controls.slack_blocks import build_card_blocks, card_notification_text
from slack_sdk.errors import SlackApiError

if TYPE_CHECKING:
    from daimon.adapters.slack.runtime import SlackRuntime
    from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)

NOT_YOURS = "Only the person who asked can answer this."
UNAVAILABLE = "This card is unavailable. Ask again to redo it."
PREFIXES = ("hcc:", "bsw:")


def is_own_card_action(action_id: str) -> bool:
    return action_id.startswith(PREFIXES)


async def handle_own_card_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Answer one press; only a closing press edits the card, anything else is ephemeral."""
    team: dict[str, Any] = payload.get("team") or {}
    user: dict[str, Any] = payload.get("user") or {}
    channel_info: dict[str, Any] = payload.get("channel") or {}
    team_id = str(team.get("id") or "")
    clicker = str(user.get("id") or "")
    channel = str(channel_info.get("id") or "")
    container: dict[str, Any] = payload.get("container") or {}
    message_ts = str(container.get("message_ts") or "")
    thread_ts = str(container.get("thread_ts") or "") or None
    actions: list[dict[str, Any]] = payload.get("actions") or []
    action_id = str(actions[0].get("action_id") or "") if actions else ""
    if not (team_id and clicker and channel and message_ts):
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    async def current_admin(user_id: str) -> bool:
        return await resolve_is_admin(client, user_id=user_id, host_team_id=team_id)

    async def say(text: str) -> None:
        try:
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel, user=clicker, thread_ts=thread_ts, **notice_fields(text)
            )
        except SlackApiError as err:
            log.warning("own_card.ephemeral_failed", error=str(err))

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    try:
        answered = await _press(
            runtime, action_id, tenant_id, clicker, message_ts, current_admin, say
        )
    except Exception:
        # Boundary: a failed press must answer the presser, not leave the click hanging.
        log.exception("own_card.press_failed", action_id=action_id)
        await say(UNAVAILABLE)
        return
    if answered is not None:
        await _edit(client, channel, message_ts, answered)


async def _press(
    runtime: SlackRuntime,
    action_id: str,
    tenant_id: uuid.UUID,
    clicker: str,
    message_ts: str,
    current_admin: Callable[[str], Awaitable[bool]],
    say: Callable[[str], Awaitable[None]],
) -> PostedCard | None:
    now = datetime.now(UTC)
    if (call := host_calls.parse_custom_id(action_id)) is not None:
        press = await host_calls.press_host_call(
            sessionmaker=runtime.sessionmaker,
            # Off is a rollback: presses on cards already posted are refused too.
            enabled=deployment_on(runtime.settings, "host_call_gate", "slack"),
            tenant_id=tenant_id,
            card_id=call[0],
            action=call[1],
            clicker=clicker,
            message_id=message_ts,
            current_admin=current_admin,
            now=now,
        )
        if press is None or not press.closed:
            await say(NOT_YOURS if press is None else press.text)
            return None
        return host_calls.answered_call_card(press.text, approved=press.approved)
    if (switch := switch_cards.parse_custom_id(action_id)) is not None:
        pressed = await switch_cards.press_switch_card(
            deps=runtime.turn_deps,
            sessionmaker=runtime.sessionmaker,
            anthropic=runtime.anthropic,
            turn_settings=runtime.settings.turn,
            deployment_default=runtime.deployment_default,
            tenant_id=tenant_id,
            card_id=switch[0],
            action=switch[1],
            clicker=clicker,
            current_admin=current_admin,
            place_word="workspace",
            now=now,
            message_id=message_ts,
        )
        if pressed is None or not pressed.closed:
            await say(NOT_YOURS if pressed is None else pressed.text)
            return None
        return switch_cards.answered_switch_card(pressed)
    return None


async def _edit(client: AsyncWebClient, channel: str, ts: str, card: PostedCard) -> None:
    try:
        await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
            channel=channel,
            ts=ts,
            text=card_notification_text(card),
            blocks=build_card_blocks(card),
        )
    except SlackApiError as err:
        log.warning("own_card.edit_failed", error=str(err))
