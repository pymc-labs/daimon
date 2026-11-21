"""A workflow ask's buttons, its answer form and typed answers on Discord.

`AskButton` is a dynamic item (`wfa:` ids, see `daimon.core.workflows.answers`), so a press
dispatches after a restart; Answer and a reject open `AskModal`. `claim_message` is what the
message handlers run before admitting a turn. The rules are core's: the press or reply is
checked against the ask posted as that message, and Discord's channel access is the outer
boundary.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Self, cast

import structlog
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.core.thread_naming import strip_mentions
from daimon.core.workflows import answers
from daimon.core.workflows.answers import Attachment, Claimed
from pydantic import JsonValue

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.runtime import DiscordRuntime

__all__ = ["AskButton", "ask_view", "claim_message"]

log = structlog.get_logger(__name__)
_FAILED = "That didn't work. Try again."


def _card(claimed: Claimed) -> discord.Embed | None:
    if claimed.text is None:
        return None
    return build_notice_embed(claimed.text, neutral=claimed.status != "refused")


def _runtime(interaction: discord.Interaction) -> DiscordRuntime:
    return cast("Any", interaction.client).runtime


async def _tell(interaction: discord.Interaction, claimed: Claimed) -> None:
    embed = _card(claimed) or build_notice_embed("Your answer is in.", neutral=True)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AskModal(discord.ui.Modal):
    """An ask's answer: one text box, a form's fields (at most five), or a reject's reason."""

    def __init__(self, form: answers.AskForm, *, route: str, message_id: str) -> None:
        super().__init__(title=form.title or "Answer")
        self._route = route
        self._message_id = message_id
        self._inputs: dict[str, discord.ui.TextInput[AskModal]] = {}
        self._picks: dict[str, discord.ui.Select[AskModal]] = {}
        for box in form.fields[:5]:
            if box.choices:
                pick: discord.ui.Select[AskModal] = discord.ui.Select(
                    options=[discord.SelectOption(label=c[:100]) for c in box.choices[:25]],
                    min_values=1 if box.required else 0,
                    required=box.required,
                )
                self._picks[box.name] = pick
                self.add_item(discord.ui.Label(text=box.title[:45] or box.name, component=pick))
                continue
            text: discord.ui.TextInput[AskModal] = discord.ui.TextInput(
                label=box.title[:45] or box.name,
                style=discord.TextStyle.paragraph,
                max_length=2000,
                required=box.required,
            )
            self._inputs[box.name] = text
            self.add_item(text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = {name: str(box.value or "") for name, box in self._inputs.items()}
        values |= {name: (pick.values or [""])[0] for name, pick in self._picks.items()}
        sent, comment = answers.form_answer(self._route, values)
        runtime = _runtime(interaction)
        claimed = await answers.press(
            runtime.sessionmaker,
            runtime.settings,
            platform="discord",
            author=str(interaction.user.id),
            message_id=self._message_id,
            route=self._route,
            values=sent,
            comment=comment,
        )
        await _tell(interaction, claimed)


class AskButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=r"wfa:(?P<wait>[0-9a-f-]{36}):(?P<route>[\w-]{1,64})",
):
    """One of an ask's buttons, rebuilt from its id alone."""

    def __init__(self, button: answers.Button | None = None, *, custom_id: str = "") -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary
                if button and button.primary
                else discord.ButtonStyle.secondary,
                label=button.label[:80] if button else "Answer",
                custom_id=button.custom_id if button else custom_id,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(custom_id=match.string)

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        # Dynamic-item dispatch swallows errors, so this boundary answers the press itself.
        try:
            parsed = answers.parse_action(self.item.custom_id or "")
            if parsed is None or interaction.message is None:
                return
            _, route = parsed
            message_id = str(interaction.message.id)
            runtime = _runtime(interaction)
            form = await answers.ask_form(
                runtime.sessionmaker, platform="discord", message_id=message_id, route=route
            )
            if form is not None and form.fields:
                await interaction.response.send_modal(
                    AskModal(form, route=route, message_id=message_id)
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            claimed = await answers.press(
                runtime.sessionmaker,
                runtime.settings,
                platform="discord",
                author=str(interaction.user.id),
                message_id=message_id,
                route=route,
            )
            await _tell(interaction, claimed)
        except Exception as err:
            log.exception("workflow_ask.press_failed", err_type=type(err).__name__)
            await _tell(interaction, Claimed("refused", _FAILED))


def ask_view(ask: dict[str, Any]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for button in answers.buttons(ask)[:5]:
        view.add_item(AskButton(button))
    return view


def _files(runtime: DiscordRuntime, message: discord.Message) -> answers.Ingest | None:
    """The message's attachments as Files API refs, read when it answers an ask."""
    attached = [a for a in message.attachments if 0 < a.size <= answers.MAX_FILE_BYTES][
        : answers.MAX_FILES
    ]
    if not attached:
        return None

    async def ingest() -> list[dict[str, JsonValue]]:
        read: list[Attachment] = []
        for attachment in attached:
            try:
                content = await attachment.read()
            except discord.HTTPException as err:
                log.warning("workflow_ask.file_read_failed", err_type=type(err).__name__)
                continue
            mime = attachment.content_type or "application/octet-stream"
            read.append(Attachment(attachment.filename, mime.split(";")[0], content))
        return await answers.ingest_files(runtime.anthropic, read)

    return ingest


async def claim_message(
    runtime: DiscordRuntime, message: discord.Message, tenant_ids: tuple[Any, ...]
) -> bool:
    """Answer an ask with `message` when it is in an ask's thread or replies to an ask.

    True when the message was the ask's (answered, refused or hinted): no turn follows."""
    if message.author.bot:
        return False
    reference = message.reference
    reply_to = None if reference is None or reference.message_id is None else reference.message_id
    in_thread = isinstance(message.channel, discord.Thread)
    claimed = await answers.claim_reply(
        runtime.sessionmaker,
        runtime.settings,
        answers.Reply(
            platform="discord",
            tenant_ids=tenant_ids,
            author=str(message.author.id),
            message_id=str(message.id),
            text=strip_mentions(message.content),
            thread_id=str(message.channel.id) if in_thread else None,
            reply_to=None if reply_to is None else str(reply_to),
            files=_files(runtime, message),
        ),
    )
    if claimed is None:
        return False
    if (embed := _card(claimed)) is not None:
        await message.reply(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    return True
