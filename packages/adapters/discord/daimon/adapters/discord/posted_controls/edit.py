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

import aiohttp
import structlog
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_submit import credential_card_edit, note_credential_save_failure
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.posted_controls import CardState, RefusalReason, card_for_request
from daimon.core.stores.domain import CredentialRequestRow

import discord

__all__ = ["edit_expired_posted_card", "edit_posted_card"]

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
    retry_reason: str | None = None,
    saving_notice: str | None = None,
) -> None:
    """Re-render the request's own message into `state`.

    Never raises on a platform or transport failure. By the time this runs
    the state it is announcing is already durable — the row is consumed, the
    write has landed or been refused — so an edit that cannot be delivered
    (message deleted, thread archived, permissions lost, a timeout) is a
    downgrade in feedback, not in correctness.

    Returns silently for a row with no posted message to edit; the ids are
    recorded right after the post, so a row without them never had a card.
    """
    async with credential_card_edit(row, state):
        if state == "partial":
            note_credential_save_failure(row, outcome)
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
                retry_reason=retry_reason,
                saving_notice=saving_notice,
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
                channel = client.get_channel(
                    int(row.origin_thread_id)
                ) or await client.fetch_channel(int(row.origin_thread_id))
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
        except (discord.HTTPException, OSError, aiohttp.ClientError) as err:
            # OSError covers a timeout or dropped connection discord.py does
            # not retry, so a caller editing many cards still reaches the rest.
            _log.warning(
                "posted_card.edit_failed", err_type=type(err).__name__, state=state, kind=row.kind
            )


async def edit_expired_posted_card(client: discord.Client, *, row: CredentialRequestRow) -> bool:
    """Acknowledge only an in-place expiry edit of this persisted Discord card.

    Unlike the click helper, failures/absent identities never look like success.
    A webhook replacement is forbidden: it does not retire the original control.
    """
    if (
        row.platform != "discord"
        or row.origin_thread_id is None
        or row.posted_message_id is None
        or row.outcome != "expired_card_pending"
        or row.used_at is None
        or row.used_at < row.expires_at
    ):
        return False
    try:
        channel_id, message_id = int(row.origin_thread_id), int(row.posted_message_id)
    except ValueError:
        return False
    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False
        if (
            channel.id != channel_id
            or derive_tenant_uuid(platform="discord", workspace_id=str(channel.guild.id))
            != row.tenant_id
        ):
            return False
        parent_id = channel.parent_id if isinstance(channel, discord.Thread) else channel.id
        if row.parent_channel_id is not None and str(parent_id) != row.parent_channel_id:
            return False
        message = await channel.get_partial_message(message_id).fetch()
        if message.id != message_id or message.channel.id != channel_id:
            return False
        view = build_card_view(card_for_request(row, state="expired"))
        if message.webhook_id is not None:
            transport = DiscordPostTransport(
                client, channel, name=message.author.name, avatar_url=None, builtin=False
            )
            edited = await transport.edit(
                message,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                _allow_replacement=False,
            )
        else:
            if client.user is None or message.author.id != client.user.id:
                return False
            edited = await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        return (
            isinstance(edited, discord.Message)
            and edited.id == message_id
            and edited.channel.id == channel_id
        )
    except (discord.HTTPException, discord.ClientException, OSError, TimeoutError) as error:
        _log.warning("credential_expiry.card_edit_failed", err_type=type(error).__name__)
        return False
