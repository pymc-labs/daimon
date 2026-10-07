"""Re-render a posted control card in place, in the bot process.

The card the MCP process posted is the only durable record of a private-value
request: the ephemeral reply a submitter gets is gone on refresh and was never
visible to anyone else. So every state the request reaches — received, then
applied, partial, refused, superseded or expired — is written back onto that
same message.

Two platform facts shape this module. A components-v2 message can never be
edited back to a classic `View` (Discord answers 50035), so every edit here
passes a `LayoutView`, no exceptions. And the initial post is the one and only
ping in the lifecycle: the footer mention on the `requested` card. Every edit
therefore goes out with `AllowedMentions.none()` — re-rendering must not
re-ping the requester each time the state moves.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress

import structlog
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.posted_controls import CardState, RefusalReason, card_for_request
from daimon.core.stores.domain import CredentialRequestRow

import discord

__all__ = ["edit_posted_card"]

_log = structlog.get_logger()


async def edit_posted_card(
    client: discord.Client,
    *,
    row: CredentialRequestRow,
    state: CardState,
    outcome: ConfigurationChange | None = None,
    refusal: RefusalReason | None = None,
    refusal_lines: Sequence[str] = (),
    replaces: str | None = None,
) -> None:
    """Re-render the request's own message into `state`.

    Never raises. By the time this runs the state it is announcing is already
    durable — the row is consumed, the write has landed or been refused — so
    an edit that cannot be delivered (message deleted, thread archived,
    permissions lost) is a downgrade in feedback, not in correctness.

    Returns silently for a row with no posted message to edit; the ids are
    recorded right after the post, so a row without them never had a card.
    """
    if row.origin_thread_id is None or row.posted_message_id is None:
        return
    view = build_card_view(
        card_for_request(
            row,
            state=state,
            outcome=outcome,
            refusal=refusal,
            refusal_lines=refusal_lines,
            replaces=replaces,
        )
    )
    message = client.get_partial_messageable(int(row.origin_thread_id)).get_partial_message(
        int(row.posted_message_id)
    )
    try:
        fetched: discord.Message | None = None
        if isinstance(message, discord.PartialMessage):  # pyright: ignore[reportUnnecessaryIsInstance]
            with suppress(discord.HTTPException):
                fetched = await message.fetch()
        if isinstance(fetched, discord.Message) and fetched.webhook_id is not None:
            channel = client.get_channel(int(row.origin_thread_id)) or await client.fetch_channel(
                int(row.origin_thread_id)
            )
            if not isinstance(channel, discord.abc.Messageable):
                return
            transport = DiscordPostTransport(
                client,
                channel,
                name=fetched.author.name,
                avatar_url=None,
                builtin=False,
            )
            await transport.edit(
                fetched, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as err:
        _log.warning(
            "posted_card.edit_failed", err_type=type(err).__name__, state=state, kind=row.kind
        )
