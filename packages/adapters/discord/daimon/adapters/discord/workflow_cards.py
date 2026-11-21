"""Workflow cards on Discord: presses, the run form, `/workflows` and the run card editor.

Every decision is `daimon.core.workflows.chat_cards`, as the person who pressed; this module
only reads the press, shows the answer and posts or edits the cards.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Self, cast

import structlog
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.posted_controls import PostedCard
from daimon.core.web_links import Assignment, TenantLinks, WebAt, links_at, panel_links
from daimon.core.workflows import chat_cards
from daimon.core.workflows.chat_cards import ChatPlace, Presser, RunForm
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.bot import DaimonBot

log = structlog.get_logger()

#: A Discord modal holds at most five text inputs.
FORM_LIMIT = 5
_NONE = discord.AllowedMentions.none()

ChannelFetcher = Callable[[int], Awaitable[object]]


def _presser(interaction: discord.Interaction) -> Presser | None:
    if interaction.guild_id is None:
        return None
    tenant = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
    channel = interaction.channel
    in_thread = isinstance(channel, discord.Thread)
    return Presser(
        tenant,
        "discord",
        str(interaction.user.id),
        channel_id=str(channel.parent_id) if in_thread else str(interaction.channel_id),
        thread_id=str(channel.id) if in_thread else None,
    )


def web_at(runtime: DiscordRuntime, fetch_channel: ChannelFetcher) -> WebAt:
    """The web links a workflow card may offer at a channel or thread, where `web_awareness`
    is On there. A channel id that is a thread resolves to its parent channel."""

    async def at(
        tenant_id: uuid.UUID, channel_id: str | None, thread_id: str | None
    ) -> TenantLinks | None:
        if thread_id is None and channel_id is not None and channel_id.isdigit():
            try:
                found = await fetch_channel(int(channel_id))
            except discord.HTTPException:
                found = None  # Unknown: treated as a plain channel.
            if isinstance(found, discord.Thread):
                channel_id, thread_id = str(found.parent_id), channel_id
        return await links_at(
            runtime.sessionmaker,
            runtime.settings,
            FeaturePlace.of(tenant_id, "discord", channel_id, thread_id),
            panel_links(runtime.settings.web, tenant_id, admin=False),
            Assignment(runtime.deployment_default, runtime.anthropic, runtime.resolver_cache),
        )

    return at


def card_text(components: Sequence[Any]) -> str:
    """The text a posted card shows, read back from its components."""
    parts: list[str] = []
    for component in components:
        if isinstance(component, discord.components.TextDisplay):
            parts.append(component.content)
        parts.append(card_text(getattr(component, "children", ())))
    return "\n\n".join(p for p in parts if p)


async def card_editor(fetch_channel: ChannelFetcher, place: ChatPlace, card: PostedCard) -> None:
    """Edit the card at `place`; the follower calls it at most every few seconds."""
    channel = await fetch_channel(int(place.channel_id))
    if not isinstance(channel, discord.abc.Messageable):
        return
    message = cast(Any, channel).get_partial_message(int(place.message_id))
    await message.edit(view=build_card_view(card), allowed_mentions=_NONE)


async def _say(interaction: discord.Interaction, text: str, link: str | None = None) -> None:
    body = f"{text}\n{link}" if link else text
    if interaction.response.is_done():
        await interaction.followup.send(embed=build_notice_embed(body), ephemeral=True)
    else:
        await interaction.response.send_message(embed=build_notice_embed(body), ephemeral=True)


async def _open(
    interaction: discord.Interaction,
    run: Callable[..., Awaitable[str | None]],
) -> None:
    """Post a run card in the pressed card's channel and start the run on it."""
    channel = interaction.channel
    bot = cast("DaimonBot", interaction.client)
    if not isinstance(channel, discord.abc.Messageable):
        await _say(interaction, chat_cards.UNAVAILABLE)
        return

    async def post(card: PostedCard) -> ChatPlace:
        sent = await channel.send(view=build_card_view(card), allowed_mentions=_NONE)
        return ChatPlace("discord", str(sent.channel.id), str(sent.id))

    async def edit(place: ChatPlace, card: PostedCard) -> None:
        await card_editor(bot._channel_by_id, place, card)  # pyright: ignore[reportPrivateUsage]

    refusal = await run(post=post, edit=edit)
    if refusal:
        await _say(interaction, refusal)


class RunFormModal(discord.ui.Modal):
    """A workflow's inputs, typed in; submitting posts a run card and starts the run."""

    def __init__(self, form: RunForm) -> None:
        super().__init__(title=form.title, custom_id=form.submit_id, timeout=600)
        self.form = form
        self.inputs: dict[str, discord.ui.TextInput[Self]] = {}
        for field in form.fields:
            hint = " or ".join(field.choices) if field.choices else None
            box: discord.ui.TextInput[Self] = discord.ui.TextInput(
                label=field.label,
                required=field.required,
                default=field.default or None,
                placeholder=(hint or "")[:100] or None,
                max_length=min(field.max_length or 4000, 4000),
                style=discord.TextStyle.short if field.kind != "text" else discord.TextStyle.long,
            )
            self.inputs[field.key] = box
            self.add_item(box)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = cast("DaimonBot", interaction.client)
        presser = _presser(interaction)
        if presser is None:
            await _say(interaction, chat_cards.UNAVAILABLE)
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        runtime = bot.runtime
        submitted = await chat_cards.submit_form(
            runtime.sessionmaker,
            runtime.settings,
            presser=presser,
            form_id=self.form.submit_id,
            raw={key: box.value for key, box in self.inputs.items()},
            limit=FORM_LIMIT,
            client=runtime.anthropic,
        )
        if isinstance(submitted, str):
            await _say(interaction, submitted)
            return
        env, form, inputs = submitted

        async def run(**io: Any) -> None:  # noqa: ANN401 - post and edit
            await chat_cards.open_run(
                env,
                form.workflow_id,
                inputs,
                test=form.test,
                name=form.title,
                links=web_at(runtime, bot._channel_by_id),  # pyright: ignore[reportPrivateUsage]
                **io,
            )

        await _open(interaction, run)


class WorkflowCardControl(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=chat_cards.CUSTOM_ID_PATTERN,
):
    """A button on a workflow card, rebuilt from its custom_id after restarts."""

    def __init__(self, button: discord.ui.Button[discord.ui.LayoutView]) -> None:
        super().__init__(button)

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(discord.ui.Button(label=getattr(item, "label", None), custom_id=match[0]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("DaimonBot", interaction.client)
        presser = _presser(interaction)
        message = interaction.message
        if presser is None or message is None:
            await _say(interaction, chat_cards.UNAVAILABLE)
            return
        runtime = bot.runtime
        try:
            # No defer: a form must be the press's first answer.
            pressed = await chat_cards.press(
                runtime.sessionmaker,
                runtime.settings,
                presser=presser,
                custom_id=self.item.custom_id or "",
                place=ChatPlace("discord", str(message.channel.id), str(message.id)),
                links=web_at(runtime, bot._channel_by_id),  # pyright: ignore[reportPrivateUsage]
                card_text=card_text(message.components),
                form_limit=FORM_LIMIT,
                client=runtime.anthropic,
            )
        except Exception:
            # Boundary: a failed press must answer the presser, not leave the click hanging.
            log.exception("workflow_card.press_failed", custom_id=self.item.custom_id)
            await _say(interaction, chat_cards.UNAVAILABLE)
            return
        if pressed.form is not None:
            await interaction.response.send_modal(RunFormModal(pressed.form))
        elif pressed.card is not None:
            await interaction.response.edit_message(
                view=build_card_view(pressed.card), allowed_mentions=_NONE
            )
        elif pressed.start is not None:
            await interaction.response.defer()
            workflow_id, test = pressed.start

            async def run(**io: Any) -> str | None:  # noqa: ANN401 - post and edit
                return await chat_cards.open_pressed(
                    runtime.sessionmaker,
                    runtime.settings,
                    presser,
                    workflow_id,
                    test=test,
                    links=web_at(runtime, bot._channel_by_id),  # pyright: ignore[reportPrivateUsage]
                    client=runtime.anthropic,
                    **io,
                )

            await _open(interaction, run)
        else:
            await _say(interaction, pressed.say or chat_cards.UNAVAILABLE, pressed.link)


class WorkflowPicker(discord.ui.Select[discord.ui.LayoutView]):
    """The `/workflows` list: picking one shows its card in place."""

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        options = [discord.SelectOption(label=n, value=i, description=s) for i, n, s in rows]
        super().__init__(
            custom_id=chat_cards.PICK_ID, placeholder="Pick a workflow", options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("DaimonBot", interaction.client)
        presser = _presser(interaction)
        if presser is None:
            await _say(interaction, chat_cards.UNAVAILABLE)
            return
        runtime = bot.runtime
        card = await chat_cards.pick(
            runtime.sessionmaker,
            runtime.settings,
            presser,
            self.values[0],
            web_at(runtime, bot._channel_by_id),  # pyright: ignore[reportPrivateUsage]
        )
        await interaction.response.edit_message(view=build_card_view(card), allowed_mentions=_NONE)


async def workflows_panel(
    sm: async_sessionmaker[AsyncSession], settings: Settings, interaction: discord.Interaction
) -> tuple[discord.ui.LayoutView | None, str | None]:
    """The panel for the person who asked, or the refusal to show them."""
    presser = _presser(interaction)
    if presser is None:
        return None, chat_cards.UNAVAILABLE
    listed = await chat_cards.list_for(sm, settings, presser)
    if isinstance(listed, str):
        return None, listed
    view = build_card_view(chat_cards.notice(chat_cards.panel_headline(listed)))
    if rows := chat_cards.panel_rows(listed):
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        row.add_item(WorkflowPicker(rows))
        view.add_item(row)
    view.timeout = 600
    return view, None


__all__ = [
    "FORM_LIMIT",
    "RunFormModal",
    "WorkflowCardControl",
    "card_editor",
    "card_text",
    "web_at",
    "workflows_panel",
]
