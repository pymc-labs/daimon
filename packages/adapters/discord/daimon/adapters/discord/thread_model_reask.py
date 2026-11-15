"""Ask again in a thread, once, when an agent edit leaves its model consent stale.

The card is posted off the turn (`daimon.core.thread_model_reask`), maybe by an earlier
process, so its Confirm is a persistent button. The custom_id holds the shown revision and
digest only; the pick is the thread's stored choice and the click is decided for the
clicker's live rights, so a replayed custom_id can only confirm what the clicker could pick.

Template-disjointness: `tmr:` must not overlap any other registered template.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Final, Self, cast

import structlog
from daimon.adapters.discord.checks import is_guild_admin, resolve_tenant_for_interaction
from daimon.adapters.discord.commands.model import may_switch_thread, thread_pick_of
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.stores.thread_models import get_thread_choice
from daimon.core.thread_model_flow import NOTHING_CHANGED, ThreadConfirm, confirm_thread_pick
from daimon.core.thread_model_reask import ThreadReask, ThreadReaskNotifier
from daimon.core.turn.deps import TurnDeps

import discord
from discord.ext import commands

_log = structlog.get_logger()

CUSTOM_ID_TEMPLATE: Final[str] = r"tmr:(?P<revision>[0-9]{1,18}):(?P<digest>[0-9a-f]{64})"
_TITLE = "Model for this conversation"
_PROMPT = "Confirm to switch this conversation."
_NOT_ALLOWED = "Only people who can start a conversation here can change its model."
_FAILED = "Something went wrong switching this conversation -- please try again."


def build_custom_id(confirm: ThreadConfirm) -> str:
    return f"tmr:{confirm.revision}:{confirm.digest}"


class ReaskConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Persistent Confirm reconstructed from its custom_id; registered in `setup_hook`."""

    def __init__(self, *, revision: int, digest: str) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Confirm",
            custom_id=f"tmr:{revision}:{digest}",
        )
        super().__init__(button)
        self.revision = revision
        self.digest = digest

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(revision=int(match["revision"]), digest=match["digest"])

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            await self._confirm(interaction)
        except Exception as err:
            # Dynamic-item dispatch is an adapter boundary: discord.py swallows what is raised.
            _log.exception("thread_model_reask.callback_failed", err_type=type(err).__name__)
            await _reply(interaction, _FAILED)

    async def _confirm(self, interaction: discord.Interaction[commands.Bot]) -> None:
        thread = interaction.channel
        tenant_id = await resolve_tenant_for_interaction(interaction.client, interaction)
        if not isinstance(thread, discord.Thread) or tenant_id is None:
            await _reply(interaction, "This button only works in a conversation thread.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await may_switch_thread(interaction, tenant_id, thread):
            await _reply(interaction, f"{_NOT_ALLOWED} {NOTHING_CHANGED}")
            return
        runtime = cast(DiscordRuntime, interaction.client.runtime)  # type: ignore[attr-defined]
        async with runtime.sessionmaker() as session:
            choice = await get_thread_choice(
                session, tenant_id=tenant_id, platform="discord", thread_id=str(thread.id)
            )
        admin = await is_guild_admin(interaction, action=DelegatedAction.AGENT_SETTINGS)
        pick = await thread_pick_of(interaction, thread, admin=admin)
        if choice is None or pick is None:
            await _reply(interaction, NOTHING_CHANGED)
            return
        confirm = ThreadConfirm(f"{choice.provider}:{choice.id}", self.digest, self.revision)
        text = await confirm_thread_pick(pick, confirm, conversation_id=str(thread.id), admin=admin)
        await _reply(interaction, text)


async def _reply(interaction: discord.Interaction[commands.Bot], text: str) -> None:
    none = discord.AllowedMentions.none()
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=none)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=none)


def reask_message(ask: ThreadReask) -> tuple[discord.Embed, discord.ui.View | None]:
    """The card, with its Confirm when the pick can still be confirmed."""
    if ask.confirm is None:
        return build_notice_embed("\n".join(ask.lines), title=_TITLE, neutral=True), None
    view = discord.ui.View(timeout=None)
    view.add_item(ReaskConfirmButton(revision=ask.confirm.revision, digest=ask.confirm.digest))
    card = build_notice_embed("\n".join((*ask.lines, _PROMPT)), title=_TITLE, neutral=True)
    return card, view


def discord_thread_reask_notifier(client: discord.Client) -> ThreadReaskNotifier:
    """Post the card in the thread; a failed post raises so the next message asks again."""

    async def send(ask: ThreadReask) -> None:
        target_id = int(ask.thread_id or ask.channel_id)
        channel = client.get_channel(target_id) or await client.fetch_channel(target_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError("thread is not messageable")
        card, view = reask_message(ask)
        none = discord.AllowedMentions.none()
        if view is None:
            await channel.send(embed=card, allowed_mentions=none)
        else:
            await channel.send(embed=card, view=view, allowed_mentions=none)

    return send


def with_thread_reask_notifier(runtime: DiscordRuntime, client: discord.Client) -> DiscordRuntime:
    """`runtime` whose turns post the card; a stand-in test runtime comes back unchanged."""
    real = cast(object, runtime)
    deps = cast(object, runtime.turn_deps)
    if not isinstance(real, DiscordRuntime) or not isinstance(deps, TurnDeps):
        return runtime
    return replace(
        real, turn_deps=replace(deps, thread_reask_notifier=discord_thread_reask_notifier(client))
    )
