"""The `here` command: who answers where it was typed, what it can read and holds.

Mirrors Discord's and Slack's `/here` with the shared card
(`daimon.core.here_card`). Typed in a channel post it describes that post's
thread, answered there for the sender only (or in the 1:1 chat); typed in the
chat it describes the chat, or the setup conversation it is switched into. The
bot heard the message, so it and the sender can both view the place.

Only the place itself counts as visible to the caller: listing which other
channels they can see takes Graph lookups per team, so an agent rule's other
channels stay unnamed rather than risk naming one they cannot see.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.adapters.teams.billing_panel import plain_name
from daimon.adapters.teams.card_actions import heading
from daimon.adapters.teams.commands import CommandContext
from daimon.core.agent_details import GitHubDeploymentFacts
from daimon.core.anthropic_guard import guard_selfhosted_channel
from daimon.core.errors import DaimonError
from daimon.core.here_card import (
    HereCard,
    for_foreign_sender,
    load_here_card,
    render_here_card,
)
from daimon.core.stores.identity import find_platform_principal
from microsoft_teams.cards import AdaptiveCard, CardElement, TextBlock

log = structlog.get_logger()

TITLE = "Here"
_FAILED = "Something went wrong reading this place's settings. Try again later."


def here_card(card: HereCard) -> AdaptiveCard:
    """The shared compact card, as literal text: names cannot format or mention."""
    shown = render_here_card(card)
    lines = [
        *([shown.subline] if shown.subline else []),
        *([f"Reading: {shown.reading}"] if shown.reading else []),
        *([f"Publishing: {shown.publishing}"] if shown.publishing else []),
        *shown.extras,
    ]
    body: list[CardElement] = [heading(plain_name(shown.title))]
    body += [TextBlock(text=plain_name(line), spacing="Small", wrap=True) for line in lines]
    return AdaptiveCard(body=body, fallback_text=TITLE)


async def _load(context: CommandContext) -> HereCard:
    runtime, asked = context.runtime, context.asked_in or context.inbound
    # A plain 1:1 chat is its own thread; only a post or a setup conversation is one.
    thread_id = asked.thread_id if asked.thread_id != asked.channel_id else None
    public_url = runtime.settings.mcp.public_url
    async with (
        guard_selfhosted_channel(
            runtime.sessionmaker,
            tenant_id=context.tenant_id,
            platform="teams",
            channel_id=asked.channel_id,
            thread_id=thread_id,
            caller_id=asked.user_id,
        ),
        runtime.sessionmaker() as session,
    ):
        principal = await find_platform_principal(
            session, tenant_id=context.tenant_id, platform="teams", external_id=asked.user_id
        )
        card = await load_here_card(
            session,
            runtime.anthropic,
            tenant_id=context.tenant_id,
            platform="teams",
            channel_id=asked.channel_id,
            thread_id=thread_id,
            default=runtime.deployment_default,
            github=GitHubDeploymentFacts(
                app_configured=runtime.settings.github_app.app_id is not None
                and runtime.settings.github_app.private_key is not None,
            ),
            public_mcp_url=str(public_url) if public_url is not None else None,
            # Only the caller sees the answer, so an admin may see admin views.
            is_admin=context.is_admin,
            caller_account_id=principal.account_id if principal else None,
            visible_channel_ids={asked.channel_id},
            bot_can_view=True,
            caller_can_view=True,
        )
    # Someone from another organisation has no 1:1 chat to send `setup` in.
    return for_foreign_sender(card) if asked.is_foreign else card


async def show_here(context: CommandContext) -> None:
    try:
        card = here_card(await _load(context))
    except (DaimonError, anthropic.APIError) as exc:
        log.warning("teams.here.failed", exc_info=exc)
        text = str(exc) if isinstance(exc, DaimonError) else _FAILED
        card = AdaptiveCard(body=[heading(TITLE), TextBlock(text=text, wrap=True)])
    await context.send_card(card)
