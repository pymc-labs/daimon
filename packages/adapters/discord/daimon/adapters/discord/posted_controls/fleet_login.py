"""The fleet login question as a Discord card (PR B): post, answer, expire, settle.

Core owns the question, the decision and every check (`daimon.core.fleet_login_decision`).
This module only draws `LoginCard` and turns a button press into one
`answer_login_click` call. Nothing is held in memory: the buttons are durable dynamic
items whose custom id names the run, the row, the choice and a digest prefix, and every
render re-reads the card from the database, so a restart or a click landing on another
process behaves the same. Login names only; never a value.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Self, cast

import structlog
from daimon.adapters.discord.theme import COLOR_AMBER
from daimon.core.fleet_login_decision import (
    REFUSAL_TEXT,
    LoginCard,
    LoginChoice,
    final_state_text,
)

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.bot import DaimonBot
    from daimon.core.authz import Subject
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = [
    "LoginButton",
    "LoginPollState",
    "build_login_view",
    "login_custom_id",
    "poll_login_cards_once",
    "run_login_card_poller",
]

#: The spawn waits at most 60 s for an answer, so the card must appear within seconds.
POLL_INTERVAL_S: Final = 2.0
_DIGEST_PREFIX: Final = 12
_CODES: Final[dict[str, LoginChoice]] = {"s": "share", "k": "skip", "o": "share_once"}
_LETTERS: Final[dict[LoginChoice, str]] = {code: letter for letter, code in _CODES.items()}
_CHOICE_WORDS: Final[dict[str, str]] = {
    "share": "shared",
    "share_once": "shared this time",
    "skip": "skipped",
    "no_answer": "no answer",
}
_TITLE: Final = "Share your logins with this fleet's workers?"
CLICK_FAILED_TEXT: Final = "Something went wrong with this card. Try again in a minute."
#: A final card that keeps failing to show is given up on after this many polls.
MAX_FINAL_ATTEMPTS: Final = 5
#: A duplicate copy whose delete keeps failing is left (and logged) after this many polls.
MAX_ORPHAN_DELETE_ATTEMPTS: Final = 5
#: Login rows with buttons on one card: five components each, within Discord's 40.
MAX_ROWS_SHOWN: Final = 5
_LABEL_MAX: Final = 80


def login_custom_id(
    run_id: uuid.UUID, row: int | Literal["all"], choice: LoginChoice, digest: str
) -> str:
    return f"flogin:{run_id.hex}:{row}:{_LETTERS[choice]}:{digest[:_DIGEST_PREFIX]}"


def _button(
    card: LoginCard, row: int | Literal["all"], choice: LoginChoice, label: str
) -> discord.ui.Button[discord.ui.LayoutView]:
    style = discord.ButtonStyle.secondary if choice == "skip" else discord.ButtonStyle.primary
    return discord.ui.Button(
        style=style, label=label, custom_id=login_custom_id(card.run_id, row, choice, card.digest)
    )


def _label(text: str) -> str:
    return text if len(text) <= _LABEL_MAX else text[: _LABEL_MAX - 1] + "…"


def build_login_view(card: LoginCard) -> discord.ui.LayoutView:
    """The card as it stands: buttons for the next few unanswered logins while open,
    then only its final words.

    A components-v2 message holds at most 40 components and each login row takes
    five, so at most `MAX_ROWS_SHOWN` rows get buttons; the rest wait their turn and
    show up as the ones above are answered (each click redraws the card).
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        accent_colour=discord.Colour(COLOR_AMBER)
    )
    container.add_item(discord.ui.TextDisplay(f"**{_TITLE}**"))
    container.add_item(discord.ui.Separator(visible=False))
    if card.final:
        short = replace(
            card,
            rows=tuple(replace(row, label=_label(row.label)) for row in card.rows),
            without=tuple(_label(line) for line in card.without),
        )
        container.add_item(discord.ui.TextDisplay(final_state_text(short) or "No logins to share."))
    else:
        container.add_item(
            discord.ui.TextDisplay(
                "The workers start once you answer, or without these logins at "
                f"<t:{int(card.expires_at.timestamp())}:T>. "
                "Share this time asks again next run."
            )
        )
        answered = [row for row in card.rows if row.choice is not None]
        if answered:
            container.add_item(
                discord.ui.TextDisplay(
                    "-# "
                    + "; ".join(
                        f"{_label(row.label)}: {_CHOICE_WORDS.get(row.choice or '', '')}"
                        for row in answered
                    )
                )
            )
        open_rows = [row for row in card.rows if row.choice is None]
        for row in open_rows[:MAX_ROWS_SHOWN]:
            container.add_item(discord.ui.TextDisplay(f"**{_label(row.label)}**"))
            buttons: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            buttons.add_item(_button(card, row.index, "share", "Share"))
            buttons.add_item(_button(card, row.index, "share_once", "Share this time"))
            buttons.add_item(_button(card, row.index, "skip", "Skip"))
            container.add_item(buttons)
        if (waiting := len(open_rows) - MAX_ROWS_SHOWN) > 0:
            container.add_item(
                discord.ui.TextDisplay(
                    f"-# {waiting} more login{'s' if waiting != 1 else ''} after these."
                )
            )
        if card.without:
            container.add_item(
                discord.ui.TextDisplay(
                    "-# Workers run without: " + ", ".join(_label(line) for line in card.without)
                )
            )
        if len(open_rows) > 1:
            skip_all: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            skip_all.add_item(_button(card, "all", "skip", "Skip all"))
            container.add_item(skip_all)
        if card.requester_platform_user_id is not None:
            container.add_item(
                discord.ui.TextDisplay(f"-# Only <@{card.requester_platform_user_id}> can answer.")
            )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


class LoginButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=r"flogin:(?P<run>[0-9a-f]{32}):(?P<row>all|[0-9]{1,3}):(?P<choice>[sko]):(?P<digest>[0-9a-f]{12})",
):
    """A durable login-question button, rebuilt from its custom id on any process."""

    def __init__(self, button: discord.ui.Button[discord.ui.LayoutView]) -> None:
        super().__init__(button)
        match = self.template.fullmatch(button.custom_id or "")
        if match is None:
            raise ValueError("invalid login control")
        self.run_id = uuid.UUID(hex=match["run"])
        self.login_row: int | Literal["all"] = "all" if match["row"] == "all" else int(match["row"])
        self.choice: LoginChoice = _CODES[match["choice"]]
        self.digest = match["digest"]

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=getattr(item, "label", None),
                custom_id=match[0],
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.bot import requester_role
        from daimon.core.fleet_login_decision import answer_login_click, settle_login_card
        from daimon.core.ma_identity import derive_tenant_uuid
        from daimon.core.stores.domain import Role

        bot = cast("DaimonBot", interaction.client)
        guild = interaction.guild
        if guild is None or interaction.message is None:
            await interaction.response.send_message(REFUSAL_TEXT["no_question"], ephemeral=True)
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild.id))
        clicker = str(interaction.user.id)

        async def live_subject() -> Subject:
            """The clicker as the turn path builds them: the live role, and a client
            (another organisation) never an admin (`admission`: HOST on Discord, so a
            known foreign home is what makes one)."""
            from daimon.core.authz import build_subject
            from daimon.core.stores.identity import find_platform_principal

            role, _ = await requester_role(guild, clicker)
            async with bot.runtime.sessionmaker() as session:
                principal = await find_platform_principal(
                    session, tenant_id=tenant_id, platform="discord", external_id=clicker
                )
            return build_subject(
                is_admin=role is Role.ADMIN,
                platform_user_id=clicker,
                client=principal is not None and principal.foreign_home_id is not None,
            )

        await interaction.response.defer()
        try:
            answer = await answer_login_click(
                bot.runtime.turn_deps,
                tenant_id=tenant_id,
                platform="discord",
                run_id=self.run_id,
                digest=self.digest,
                row=self.login_row,
                choice=self.choice,
                clicker_platform_user_id=clicker,
                live_subject=live_subject,
                limiter=bot.login_click_limiter,
            )
        except Exception:
            log.exception("fleet.login_click_failed", run_id=str(self.run_id))
            await interaction.followup.send(CLICK_FAILED_TEXT, ephemeral=True)
            return
        log.info(
            "fleet.login_click",
            run_id=str(self.run_id),
            refused=answer.refused,
            decided=answer.decided,
            saved=answer.saved,
        )
        if answer.refused is not None:
            await interaction.followup.send(REFUSAL_TEXT[answer.refused], ephemeral=True)
        card = answer.card
        if card is None or card.card_message_id != str(interaction.message.id):
            return
        await interaction.message.edit(
            view=build_login_view(card), allowed_mentions=discord.AllowedMentions.none()
        )
        if card.final:
            await settle_login_card(
                bot.runtime.sessionmaker, tenant_id=tenant_id, run_id=card.run_id
            )


#: Post a card into a thread and return its message; None when the thread is gone.
PostCard = Callable[[LoginCard], Awaitable[discord.Message | None]]
#: Find a posted card's message again by its id; None when it is gone.
FetchCard = Callable[[LoginCard], Awaitable[discord.Message | None]]
#: Find a copy of this run's card already in its thread (a post whose claim never
#: landed), so a retry adopts it instead of posting a second one.
FindCard = Callable[[LoginCard], Awaitable[discord.Message | None]]


def is_login_card(message: discord.Message, run_id: uuid.UUID) -> bool:
    from daimon.adapters.discord.posted_controls.fleet import custom_ids_of

    prefix = f"flogin:{run_id.hex}:"
    return any(
        custom_id.startswith(prefix)
        for custom_id in custom_ids_of(cast("list[object]", message.components))
    )


@dataclass
class LoginPollState:
    """What one poller remembers between polls: copies still to remove, and how many
    times each final card failed to show."""

    orphans: list[tuple[discord.Message, int]] = field(
        default_factory=list[tuple[discord.Message, int]]
    )
    failures: dict[uuid.UUID, int] = field(default_factory=dict[uuid.UUID, int])


def _retryable(error: BaseException) -> bool:
    return isinstance(error, discord.HTTPException) and (error.status == 429 or error.status >= 500)


async def _remove_orphans(state: LoginPollState) -> None:
    kept: list[tuple[discord.Message, int]] = []
    for message, attempts in state.orphans:
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except Exception as error:
            if _retryable(error) and attempts + 1 < MAX_ORPHAN_DELETE_ATTEMPTS:
                kept.append((message, attempts + 1))
            else:
                log.warning("fleet.login_card_copy_left", message_id=message.id)
    state.orphans = kept


async def _show_final(message: discord.Message, card: LoginCard) -> None:
    await message.edit(view=build_login_view(card), allowed_mentions=discord.AllowedMentions.none())


async def _post_open(
    sessionmaker: async_sessionmaker[AsyncSession],
    card: LoginCard,
    *,
    post: PostCard,
    find: FindCard,
    state: LoginPollState,
) -> None:
    """Post (or adopt) the open card, then claim it; a lost claim never leaves a live copy."""
    from daimon.core.fleet_login_decision import login_cards, settle_login_card
    from daimon.core.stores import fleet

    message = await find(card) or await post(card)
    if message is None:
        return
    try:
        claimed = await fleet.record_login_card(
            sessionmaker, tenant_id=card.tenant_id, run_id=card.run_id, message_id=str(message.id)
        )
    except Exception:
        # The claim never landed: no live copy may be left; the next poll posts again.
        state.orphans.append((message, 0))
        await _remove_orphans(state)
        raise
    if claimed:
        return
    now_card = next(
        (
            other
            for other in await login_cards(sessionmaker, platform="discord")
            if other.run_id == card.run_id and other.tenant_id == card.tenant_id
        ),
        None,
    )
    if now_card is not None and now_card.final and now_card.card_message_id is None:
        # Decided or expired between the post and the claim: this copy is the card.
        await _show_final(message, now_card)
        await settle_login_card(sessionmaker, tenant_id=card.tenant_id, run_id=card.run_id)
        return
    if now_card is None or now_card.card_message_id != str(message.id):
        state.orphans.append((message, 0))  # Another poster's card is the live one.
        await _remove_orphans(state)


async def poll_login_cards_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    post: PostCard,
    fetch: FetchCard,
    find: FindCard,
    state: LoginPollState | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Post open cards, expire them at their deadline, and settle every final card.

    Each card is handled on its own: one card's error never stops the others.
    """
    from daimon.core.fleet_login_decision import expire_question, login_cards, settle_login_card

    state = state if state is not None else LoginPollState()
    await _remove_orphans(state)
    for card in await login_cards(sessionmaker, platform="discord"):
        try:
            if not card.final and now() >= card.expires_at:
                expired = await expire_question(
                    sessionmaker, tenant_id=card.tenant_id, run_id=card.run_id, now=now()
                )
                card = expired or card
            if not card.final:
                if card.card_message_id is None:
                    await _post_open(sessionmaker, card, post=post, find=find, state=state)
                continue
            if card.card_message_id is not None:
                message = await fetch(card)
                if message is not None:
                    await _show_final(message, card)
            await settle_login_card(sessionmaker, tenant_id=card.tenant_id, run_id=card.run_id)
            state.failures.pop(card.run_id, None)
        except Exception as error:
            if not card.final:
                # An open card is retried until it expires; expiry then settles it.
                log.warning("fleet.login_card_post_failed", run_id=str(card.run_id), exc_info=True)
                continue
            attempts = state.failures.get(card.run_id, 0) + 1
            state.failures[card.run_id] = attempts
            if _retryable(error) and attempts < MAX_FINAL_ATTEMPTS:
                continue
            # A gone thread, a non-retryable error, or a persistent outage: stop here.
            log.warning(
                "fleet.login_card_final_unshown",
                run_id=str(card.run_id),
                attempts=attempts,
                exc_info=True,
            )
            state.failures.pop(card.run_id, None)
            try:
                await settle_login_card(sessionmaker, tenant_id=card.tenant_id, run_id=card.run_id)
            except Exception:
                log.exception("fleet.login_card_settle_failed", run_id=str(card.run_id))


async def run_login_card_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    post: PostCard,
    fetch: FetchCard,
    find: FindCard,
    should_stop: Callable[[], bool],
    interval_s: float = POLL_INTERVAL_S,
) -> None:
    state = LoginPollState()
    while not should_stop():
        try:
            await poll_login_cards_once(
                sessionmaker, post=post, fetch=fetch, find=find, state=state
            )
        except Exception:
            log.exception("fleet.login_card_poll_failed")
        await asyncio.sleep(interval_s)
