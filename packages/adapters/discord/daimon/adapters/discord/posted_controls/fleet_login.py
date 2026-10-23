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
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = [
    "LoginButton",
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


def build_login_view(card: LoginCard) -> discord.ui.LayoutView:
    """The card as it stands: a row of buttons per unanswered login while open,
    then only its final words."""
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        accent_colour=discord.Colour(COLOR_AMBER)
    )
    container.add_item(discord.ui.TextDisplay(f"**{_TITLE}**"))
    container.add_item(discord.ui.Separator(visible=False))
    if card.final:
        container.add_item(discord.ui.TextDisplay(final_state_text(card) or "No logins to share."))
    else:
        container.add_item(
            discord.ui.TextDisplay(
                "The workers start once you answer, or without these logins at "
                f"<t:{int(card.expires_at.timestamp())}:T>. "
                "Share this time asks again next run."
            )
        )
        for row in card.rows:
            if row.choice is not None:
                container.add_item(
                    discord.ui.TextDisplay(f"-# {row.label}: {_CHOICE_WORDS.get(row.choice, '')}")
                )
                continue
            container.add_item(discord.ui.TextDisplay(f"**{row.label}**"))
            buttons: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            buttons.add_item(_button(card, row.index, "share", "Share"))
            buttons.add_item(_button(card, row.index, "share_once", "Share this time"))
            buttons.add_item(_button(card, row.index, "skip", "Skip"))
            container.add_item(buttons)
        if card.without:
            container.add_item(
                discord.ui.TextDisplay("-# Workers run without: " + ", ".join(card.without))
            )
        if sum(row.choice is None for row in card.rows) > 1:
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

        async def live_admin() -> bool:
            role, _ = await requester_role(guild, clicker)
            return role is Role.ADMIN

        await interaction.response.defer()
        answer = await answer_login_click(
            bot.runtime.turn_deps,
            tenant_id=tenant_id,
            platform="discord",
            run_id=self.run_id,
            digest=self.digest,
            row=self.login_row,
            choice=self.choice,
            clicker_platform_user_id=clicker,
            live_admin=live_admin,
            limiter=bot.login_click_limiter,
        )
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


#: Post a card into a thread and return its message id; None when the thread is gone.
PostCard = Callable[[LoginCard], Awaitable[discord.Message | None]]
#: Find a posted card's message again (after a restart); None when it is gone.
FetchCard = Callable[[LoginCard], Awaitable[discord.Message | None]]


async def poll_login_cards_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    post: PostCard,
    fetch: FetchCard,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Post open cards, expire them at their deadline, and settle every final card."""
    from daimon.core.fleet_login_decision import expire_question, login_cards, settle_login_card
    from daimon.core.stores import fleet

    for card in await login_cards(sessionmaker, platform="discord"):
        try:
            if not card.final and now() >= card.expires_at:
                expired = await expire_question(
                    sessionmaker, tenant_id=card.tenant_id, run_id=card.run_id, now=now()
                )
                card = expired or card
            if not card.final:
                if card.card_message_id is None:
                    message = await post(card)
                    if message is not None and not await fleet.record_login_card(
                        sessionmaker,
                        tenant_id=card.tenant_id,
                        run_id=card.run_id,
                        message_id=str(message.id),
                    ):
                        await message.delete()  # Another poster won; keep one card.
                continue
            if card.card_message_id is not None:
                message = await fetch(card)
                if message is not None:
                    await message.edit(
                        view=build_login_view(card),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            await settle_login_card(sessionmaker, tenant_id=card.tenant_id, run_id=card.run_id)
        except discord.HTTPException as exc:
            # 429/5xx retry on the next poll; a gone or forbidden thread never will.
            if exc.status == 429 or exc.status >= 500:
                continue
            log.info("fleet.login_card_unpostable", run_id=str(card.run_id), status=exc.status)
            if card.final:
                # Nothing left to show: stop retrying. An open one expires on its own.
                await settle_login_card(sessionmaker, tenant_id=card.tenant_id, run_id=card.run_id)


async def run_login_card_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    post: PostCard,
    fetch: FetchCard,
    should_stop: Callable[[], bool],
    interval_s: float = POLL_INTERVAL_S,
) -> None:
    while not should_stop():
        try:
            await poll_login_cards_once(sessionmaker, post=post, fetch=fetch)
        except Exception:
            log.exception("fleet.login_card_poll_failed")
        await asyncio.sleep(interval_s)
