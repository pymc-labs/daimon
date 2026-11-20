"""The Private answer button in a room that people outside the team can see.

`audience_hold` mode `on` gives a contained room answers with internal sources left
out. An internal asker there gets this offer under the answer, or after the refusal
of an answer the room's new audience stopped (gate A). A press asks the same question
again as a new DM turn of the asker (BUILD-PLAN C4): no source, no room history,
memory read-only, nothing posted in the room. The room tells only the asker
"Sent to your DMs".

The offer lives in this process for an hour (`PrivateOffers`). The custom_id names a
token only; a press is decided for the presser's live standing, the room's live facts
and the server's DM setting. An offer the press would certainly refuse as things stand
(DMs off, or an agent that never DMs) is not shown. Mode `shadow` logs `private_offer`
and shows nothing; mode `off` never reaches this module.

Template-disjointness: `pva:` must not overlap `tho:`, `mfb:`, `ztc:`, `sup:` or the
wizard's `wz:`; discord.py fires every registered template that fullmatches.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Self, cast

import structlog
from cachetools import TTLCache
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.checks import member_role_ids
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.core.authz import AgentRef
from daimon.core.private_answer import (
    PrivateAnswerRefusal,
    offer_private_answer,
    private_answer_refusal,
    read_mode,
)

import discord
from discord.ext import commands

_log = structlog.get_logger(__name__)

CUSTOM_ID_TEMPLATE: Final[str] = r"pva:(?P<token>[0-9a-f]{16})"
BUTTON_LABEL: Final[str] = "Private answer (only me)"
#: Under an answer given with internal sources left out.
OFFER_AFTER_ANSWER: Final[str] = (
    "Internal sources left out: people outside the team can see this room."
)
#: After gate A refused an answer because the room became contained.
OFFER_AFTER_REFUSAL: Final[str] = (
    "Answer not posted: people outside the team can see this room now.\n\n"
    "Press the button to get it in your DMs."
)
SENT: Final[str] = "Sent to your DMs."
EXPIRED: Final[str] = "This button has expired.\n\nAsk your question again in the room."
NOT_ASKER: Final[str] = (
    "Only the person who asked can use this button.\n\nAsk your own question to get one."
)
DM_CLOSED: Final[str] = (
    "I couldn't send you a DM, so nothing was sent.\n\n"
    "Allow direct messages from this server, then press the button again."
)
BUSY: Final[str] = (
    "I'm still answering in your DMs.\n\nPress the button again once that answer is done."
)
FAILED: Final[str] = "Couldn't start the private answer.\n\nPlease retry, or ask a server admin."
REFUSALS: Final[dict[PrivateAnswerRefusal, str]] = {
    "unavailable": "Private answers are off on this server.\n\nAsk a server admin.",
    "not_internal": (
        "Private answers are for team members only.\n\n"
        "Ask a server admin if you should have a team role."
    ),
    "narrow": "This room is closed to private answers.\n\nAsk a server admin.",
    "dms_off": ("Daimon DMs are off on this server.\n\nAsk a server admin to run /dm-me enable."),
}
_ACCESS_REFUSED: Final[str] = (
    "You can't get a private answer from this agent.\n\nAsk a server admin."
)

#: How long an offer can be pressed.
OFFER_SECONDS: Final[float] = 3600.0
_MAX_OFFERS: Final[int] = 4096


@dataclass(frozen=True)
class PrivateOffer:
    """What a press needs: the asker, the room, the agent and the question.

    `channel_id` is the parent channel; `thread_id` the turn's thread when it has one.
    `agent_ref` is the agent as its rules name it, for the offer's DM check.
    """

    tenant_id: uuid.UUID
    asker_id: int
    channel_id: str
    agent: str
    agent_ref: AgentRef
    question: str
    role_ids: tuple[str, ...] | None = None
    thread_id: str | None = None


class PrivateOffers:
    """The offers shown in this process, by button token, for `OFFER_SECONDS`."""

    def __init__(self, *, ttl: float = OFFER_SECONDS, maxsize: int = _MAX_OFFERS) -> None:
        self._offers = TTLCache[str, PrivateOffer](maxsize, ttl)

    def add(self, offer: PrivateOffer) -> str:
        token = secrets.token_hex(8)
        self._offers[token] = offer
        return token

    def get(self, token: str) -> PrivateOffer | None:
        return self._offers.get(token)


def _offers(bot: DaimonBot) -> PrivateOffers | None:
    from daimon.adapters.discord.commands.direct_messages import DirectMessageCog

    cog = bot.get_cog("DirectMessageCog")
    return cog.private_offers if isinstance(cog, DirectMessageCog) else None


def build_custom_id(token: str) -> str:
    return f"pva:{token}"


async def show_private_offer(
    bot: DaimonBot,
    offer: PrivateOffer,
    *,
    send: Callable[[discord.Embed, discord.ui.View], Awaitable[object]],
    refused: bool,
) -> bool:
    """Post the offer card when the room, the mode and the asker call for it.

    `send` posts into the turn's thread (or its channel); `refused` picks the line
    for an answer gate A stopped. The mode is read again here: a server switched to
    `off` during the turn shows nothing, and one switched to `shadow` logs
    `private_offer` and posts nothing. Never raises: the offer is extra to the turn.
    """
    try:
        offers = _offers(bot)
        if offers is None:
            return False
        async with bot.runtime.sessionmaker() as session:
            # The mode now, not the one the turn was admitted in: it may have changed.
            mode = await read_mode(session, tenant_id=offer.tenant_id, channel_id=offer.channel_id)
            show = await offer_private_answer(
                session,
                tenant_id=offer.tenant_id,
                user_id=str(offer.asker_id),
                channel_id=offer.channel_id,
                thread_id=offer.thread_id,
                mode=mode,
                agent=offer.agent,
                agent_ref=offer.agent_ref,
                event_role_ids=offer.role_ids,
                resolver=bot.runtime.turn_deps.membership_resolver,
            )
        if not show:
            return False
        view = discord.ui.View(timeout=None)
        view.add_item(PrivateAnswerButton(token=offers.add(offer)))
        line = OFFER_AFTER_REFUSAL if refused else OFFER_AFTER_ANSWER
        await send(build_notice_embed(line, neutral=True), view)
        return True
    except Exception as err:  # the offer is best effort; the turn already ended
        _log.warning("private_answer.offer_failed", err_type=type(err).__name__)
        return False


class PrivateAnswerButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Persistent button reconstructed from its custom_id; registered in `setup_hook`."""

    def __init__(self, *, token: str) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=BUTTON_LABEL,
            custom_id=build_custom_id(token),
        )
        super().__init__(button)
        self.token = token

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(token=match["token"])

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            await press_private_answer(interaction, self.token)
        except Exception as err:
            # Dynamic-item dispatch is an adapter boundary: discord.py swallows
            # anything raised here, so log it and tell the presser.
            _log.exception("private_answer.press_failed", err_type=type(err).__name__)
            await _tell(interaction, FAILED)


async def _tell(
    interaction: discord.Interaction[commands.Bot], text: str, *, neutral: bool = False
) -> None:
    """Answer the presser alone; never in the room."""
    card = build_notice_embed(text, neutral=neutral)
    with contextlib.suppress(discord.HTTPException):
        if interaction.response.is_done():
            await interaction.followup.send(embed=card, ephemeral=True)
        else:
            await interaction.response.send_message(embed=card, ephemeral=True)


async def press_private_answer(interaction: discord.Interaction[commands.Bot], token: str) -> None:
    """Start the Private answer for the asker, or tell the presser privately why not."""
    bot = cast(DaimonBot, interaction.client)
    offers = _offers(bot)
    offer = offers.get(token) if offers is not None else None
    if offer is None:
        await _tell(interaction, EXPIRED)
        return
    user = interaction.user
    if user.id != offer.asker_id:
        await _tell(interaction, NOT_ASKER)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    runtime = bot.runtime
    async with runtime.sessionmaker() as session:
        mode = await read_mode(session, tenant_id=offer.tenant_id, channel_id=offer.channel_id)
        refusal = await private_answer_refusal(
            session,
            tenant_id=offer.tenant_id,
            user_id=str(user.id),
            channel_id=offer.channel_id,
            thread_id=offer.thread_id,
            mode=mode,
            event_role_ids=member_role_ids(user) if isinstance(user, discord.Member) else None,
            resolver=runtime.turn_deps.membership_resolver,
        )
    if refusal is not None:
        _log.info("private_answer.refused", reason=refusal, tenant_id=str(offer.tenant_id))
        await _tell(interaction, REFUSALS[refusal])
        return
    from daimon.adapters.discord.commands.direct_messages import DirectMessageCog

    cog = bot.get_cog("DirectMessageCog")
    if not isinstance(cog, DirectMessageCog):
        await _tell(interaction, FAILED)
        return

    async def started() -> None:
        await _tell(interaction, SENT, neutral=True)

    outcome = await cog.start_private_answer(
        user,
        request_id=interaction.id,
        tenant_id=offer.tenant_id,
        agent=offer.agent,
        question=offer.question,
        started=started,
    )
    _log.info("private_answer.pressed", outcome=outcome, tenant_id=str(offer.tenant_id))
    if outcome == "dm_failed":
        await _tell(interaction, DM_CLOSED)
    elif outcome == "busy":
        await _tell(interaction, BUSY)
    elif outcome == "refused":
        await _tell(interaction, _ACCESS_REFUSED)
    elif outcome == "failed":
        await _tell(interaction, FAILED)


__all__ = [
    "BUTTON_LABEL",
    "CUSTOM_ID_TEMPLATE",
    "PrivateAnswerButton",
    "PrivateOffer",
    "PrivateOffers",
    "press_private_answer",
    "show_private_offer",
]
