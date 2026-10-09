"""Channel skills: extra skills this channel's agent runs with, here only.

Reached from Who answers where, by server admins and this channel's admins
(`may_set_channel_skills`). Every click and submit re-checks both live, and
each write is audited (`panel_audit`). What a channel may add is
`daimon.core.channel_skills`'s.
"""

from __future__ import annotations

import functools
from collections.abc import Coroutine, Sequence
from typing import Any, Final

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.channel_environment import load_picker_subject
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_skills import REFUSALS, add_skill_to_channel, may_set_channel_skills
from daimon.core.errors import SkillsListTruncatedError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.panel_audit import record_panel_write
from daimon.core.stores.channel_skills import list_channel_skills, remove_channel_skill
from daimon.core.stores.domain import ChannelSkillRow

import discord

log = structlog.get_logger()

CHANNEL_SKILLS_LABEL: Final = "Channel skills"
ADD_LABEL: Final = "Add skill"
BACK_LABEL: Final = "◀ Back"
MAX_LISTED: Final = 20
EXPLAINER: Final = (
    "-# Added to whatever agent answers in this channel, here only, from the next message. "
    "A workspace library skill, or one uploaded to this channel's agent. Server admins and "
    "this channel's admins only."
)
REFUSED_MESSAGE: Final = (
    "Changing this channel's skills needs Manage Server or an admin of this channel."
)
UNREADABLE: Final = "This server's skills could not all be read. Nothing changed."


def build_channel_skills_container(
    rows: Sequence[ChannelSkillRow], *, channel_name: str | None
) -> discord.ui.Container[discord.ui.LayoutView]:
    """The panel card for one channel's skills. Pure — no I/O."""
    lines = [f"`{row.name}` · {row.version}" for row in rows[:MAX_LISTED]]
    if len(rows) > MAX_LISTED:
        lines.append(f"-# and {len(rows) - MAX_LISTED} more")
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(header(f"{CHANNEL_SKILLS_LABEL} · #{channel_name or 'this channel'}"))
    container.add_item(discord.ui.TextDisplay("\n".join(lines) or "-# no extra skills"))
    container.add_item(hairline())
    container.add_item(discord.ui.TextDisplay(EXPLAINER))
    return container


async def may_change_channel_skills(
    interaction: discord.Interaction, *, runtime: DiscordRuntime, state: PanelState, live: bool
) -> bool:
    """Server admin, or an admin of the panel's channel.

    ``live`` reads Manage Server off the member, as clicks and submits must;
    rendering the button trusts the panel state.
    """
    if not state.channel_id:
        return False
    subject = await load_picker_subject(interaction, runtime=runtime, state=state, live=live)
    return bool(may_set_channel_skills(subject, str(state.channel_id)))


async def refuse_unless_may_change_skills(
    interaction: discord.Interaction, *, runtime: DiscordRuntime, state: PanelState
) -> bool:
    """True, after answering with the refusal, when the caller may not change them now.

    Call it before any ``defer()`` or response, so the refusal owns the first response.
    """
    if await may_change_channel_skills(interaction, runtime=runtime, state=state, live=True):
        return False
    await interaction.response.send_message(REFUSED_MESSAGE, ephemeral=True)
    return True


async def load_channel_skills(
    runtime: DiscordRuntime, *, state: PanelState
) -> list[ChannelSkillRow]:
    async with runtime.sessionmaker() as session:
        return await list_channel_skills(
            session,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id)),
            platform="discord",
            channel_id=str(state.channel_id),
        )


class ChannelSkillsView(PanelViewBase):
    """This channel's extra skills, with Add and a remove select."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        rows: Sequence[ChannelSkillRow],
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.rows = list(rows)
        container = build_channel_skills_container(self.rows, channel_name=state.channel_name)
        if self.rows:
            self.remove_select: discord.ui.Select[discord.ui.LayoutView] = discord.ui.Select(
                placeholder="Remove a skill",
                options=[
                    discord.SelectOption(label=row.name[:100], value=row.skill_id)
                    for row in self.rows[:25]
                ],
            )
            self.remove_select.callback = self._on_remove  # type: ignore[method-assign]  # per-instance callback
            select_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            select_row.add_item(self.remove_select)
            container.add_item(select_row)
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=BACK_LABEL, style=discord.ButtonStyle.secondary
        )
        back.callback = self._on_back  # type: ignore[method-assign]  # per-instance callback
        row.add_item(back)
        add: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=ADD_LABEL, style=discord.ButtonStyle.primary
        )
        add.callback = self._on_add  # type: ignore[method-assign]  # per-instance callback
        row.add_item(add)
        row.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(row)
        self.add_item(container)

    def audit(
        self, interaction: discord.Interaction
    ) -> functools.partial[Coroutine[Any, Any, None]]:
        return functools.partial(
            record_panel_write,
            self.runtime.sessionmaker,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id)),
            platform="discord",
            platform_user_id=str(interaction.user.id),
            op="channel_skills",
        )

    async def rebuilt(self) -> ChannelSkillsView:
        return ChannelSkillsView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            rows=await load_channel_skills(self.runtime, state=self.state),
        )

    async def _on_back(self, interaction: discord.Interaction) -> None:
        # Lazy import: the routing screen opens this one.
        from daimon.adapters.discord.agent_setup.routing_view import build_routing_view

        await interaction.response.defer()
        routing = await build_routing_view(
            interaction,
            runtime=self.runtime,
            state=self.state,
            allowed_user_id=self.allowed_user_id,
        )
        await self.swap_to(interaction, routing)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        if await refuse_unless_may_change_skills(
            interaction, runtime=self.runtime, state=self.state
        ):
            await self.audit(interaction)(outcome="denied", reason="needs_admin")
            return
        await interaction.response.send_modal(AddChannelSkillModal(self))

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        audit = self.audit(interaction)
        if await refuse_unless_may_change_skills(
            interaction, runtime=self.runtime, state=self.state
        ):
            await audit(outcome="denied", reason="needs_admin")
            return
        await interaction.response.defer()
        async with self.runtime.sessionmaker.begin() as session:
            await remove_channel_skill(
                session,
                tenant_id=derive_tenant_uuid(
                    platform="discord", workspace_id=str(self.state.guild_id)
                ),
                platform="discord",
                channel_id=str(self.state.channel_id),
                skill_id=self.remove_select.values[0],
            )
        await audit(outcome="allowed", reason="completed")
        await self.swap_to(interaction, await self.rebuilt())


class AddChannelSkillModal(discord.ui.Modal):
    """Name a skill to add to this channel."""

    def __init__(self, view: ChannelSkillsView) -> None:
        name = view.state.channel_name or "this channel"
        super().__init__(title=f"Add a skill to #{name}"[:45])
        self._view = view
        self.skill: discord.ui.TextInput[discord.ui.View] = discord.ui.TextInput(
            label="Skill", placeholder="name, agent/name or skill id", max_length=200
        )
        self.add_item(self.skill)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view, state = self._view, self._view.state
        audit = view.audit(interaction)
        if await refuse_unless_may_change_skills(interaction, runtime=view.runtime, state=state):
            await audit(outcome="denied", reason="needs_admin")
            return
        try:
            async with view.runtime.sessionmaker.begin() as session:
                added = await add_skill_to_channel(
                    session,
                    view.runtime.anthropic,
                    tenant_id=derive_tenant_uuid(
                        platform="discord", workspace_id=str(state.guild_id)
                    ),
                    platform="discord",
                    channel_id=str(state.channel_id),
                    skill=self.skill.value,
                    default=view.runtime.deployment_default,
                    actor_account_id=state.account_id,
                )
        except (SkillsListTruncatedError, anthropic.APIError):
            await interaction.response.send_message(UNREADABLE, ephemeral=True)
            await audit(outcome="error", reason="skills_unreadable")
            return
        if isinstance(added, str):
            await interaction.response.send_message(REFUSALS[added], ephemeral=True)
            await audit(outcome="denied", reason=added)
            return
        await audit(outcome="allowed", reason="completed")
        log.info("agent_setup.channel_skills.added", skill_id=added.skill_id)
        await view.swap_to(interaction, await view.rebuilt())
