"""The `here` command: who answers where it was typed, what it can read and holds.

Mirrors Discord's and Slack's `/here` with the shared card
(`daimon.core.here_card`). Typed in a channel post it describes that post's
thread and is answered in the 1:1 chat; typed in the chat it describes the
chat, or the setup conversation it is switched into. The bot heard the message,
so it and the sender can both view the place.

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
from daimon.core.errors import DaimonError
from daimon.core.here_card import HereCard, load_here_card, render_here_card
from daimon.core.stores.identity import find_platform_principal
from microsoft_teams.cards import AdaptiveCard, CardElement, TextBlock

log = structlog.get_logger()

TITLE = "Here"
_FAILED = "Something went wrong reading this place's settings. Try again later."


def here_card(card: HereCard) -> AdaptiveCard:
    """The shared compact card, as literal text: names cannot format or mention."""
    shown = render_here_card(card)
    lines = [
        # The shared copy names Slack and Discord's command; Teams' is `setup`.
        *([shown.subline.replace("/agent-setup", "setup")] if shown.subline else []),
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
    github = runtime.settings.github
    public_url = runtime.settings.mcp.public_url
    async with runtime.sessionmaker() as session:
        principal = await find_platform_principal(
            session, tenant_id=context.tenant_id, platform="teams", external_id=asked.user_id
        )
        return await load_here_card(
            session,
            runtime.anthropic,
            tenant_id=context.tenant_id,
            platform="teams",
            channel_id=asked.channel_id,
            thread_id=thread_id,
            default=runtime.deployment_default,
            github=GitHubDeploymentFacts(
                has_fallback_pat=github.fallback_pat is not None,
                app_configured=github.app_id is not None and github.app_private_key is not None,
            ),
            public_mcp_url=str(public_url) if public_url is not None else None,
            # Answered in the caller's own 1:1 chat, so an admin may see admin views.
            is_admin=context.is_admin,
            caller_account_id=principal.account_id if principal else None,
            visible_channel_ids={asked.channel_id},
            bot_can_view=True,
            caller_can_view=True,
        )


async def show_here(context: CommandContext) -> None:
    try:
        card = here_card(await _load(context))
    except (DaimonError, anthropic.APIError) as exc:
        log.warning("teams.here.failed", exc_info=exc)
        text = str(exc) if isinstance(exc, DaimonError) else _FAILED
        card = AdaptiveCard(body=[heading(TITLE), TextBlock(text=text, wrap=True)])
    await context.send_card(card)
