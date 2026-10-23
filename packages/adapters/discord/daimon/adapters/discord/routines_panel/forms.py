"""Create modal and confirmed deletion for the Discord routines panel."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from daimon.adapters.discord import checks
from daimon.adapters.discord.routines_panel import embeds
from daimon.adapters.discord.routines_panel.state import RoutinesPanelState
from daimon.adapters.discord.routines_panel.write import (
    create_routine_via_panel,
    delete_routine_via_panel,
)
from daimon.core.authz import Action, Place, Subject, Surface, authorize, build_agent_ref
from daimon.core.cron import InvalidScheduleError, validated_next_slot
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.routines import can_manage_routine
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.routines import get_routine

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.routines_panel.panel import RoutinesPanelView


class NewButton(discord.ui.Button["RoutinesPanelView"]):
    def __init__(self) -> None:
        super().__init__(label="New", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        if not checks.is_guild_admin(interaction):
            await interaction.response.send_message(embeds.ADMIN_ONLY, ephemeral=True)
            return
        try:
            await interaction.response.send_modal(CreateRoutineModal(self.view, interaction))
        except Exception as exc:  # routines-panel button callback boundary
            from daimon.adapters.discord.routines_panel.panel import _send_callback_error

            await _send_callback_error(interaction, exc)


class CreateRoutineModal(discord.ui.Modal, title="New routine"):
    def __init__(self, panel: RoutinesPanelView, origin: discord.Interaction) -> None:
        super().__init__(timeout=600)
        self.panel = panel
        self.origin = origin
        self.agent = discord.ui.TextInput[CreateRoutineModal](label="Agent name", max_length=100)
        self.cron = discord.ui.TextInput[CreateRoutineModal](
            label="Cron schedule", placeholder="0 9 * * 1-5", max_length=100
        )
        self.timezone = discord.ui.TextInput[CreateRoutineModal](
            label="Timezone", default="UTC", max_length=100
        )
        self.message = discord.ui.TextInput[CreateRoutineModal](
            label="Message", style=discord.TextStyle.paragraph, max_length=4000
        )
        for field in (self.agent, self.cron, self.timezone, self.message):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.routines_panel.panel import _rerender, _send_callback_error

        if interaction.user.id != self.origin.user.id or not checks.is_guild_admin(interaction):
            await interaction.response.send_message(embeds.ADMIN_ONLY, ephemeral=True)
            return
        try:
            cron = self.cron.value.strip()
            timezone = self.timezone.value.strip()
            try:
                next_fire_at = validated_next_slot(cron, timezone, datetime.now(UTC))
            except InvalidScheduleError:
                await interaction.response.send_message(embeds.INVALID_SCHEDULE, ephemeral=True)
                return
            if not self.message.value.strip() or not self.agent.value.strip():
                await interaction.response.send_message(embeds.FIELDS_REQUIRED, ephemeral=True)
                return
            channel = self.origin.channel
            if (
                not isinstance(channel, (discord.Thread, discord.abc.GuildChannel))
                or channel.guild.id != self.origin.guild_id
                or channel.id != self.origin.channel_id
            ):
                await interaction.response.send_message(embeds.SOURCE_UNVERIFIED, ephemeral=True)
                return
            source_thread_id = str(channel.id) if isinstance(channel, discord.Thread) else None
            parent_id = channel.parent_id if isinstance(channel, discord.Thread) else channel.id
            if parent_id is None:
                await interaction.response.send_message(embeds.SOURCE_UNVERIFIED, ephemeral=True)
                return
            source_channel_id = str(parent_id)
            runtime = self.panel.runtime
            if runtime.settings.recruitment.is_recruitment_channel(
                str(self.origin.guild_id), str(channel.id), source_channel_id
            ):
                await interaction.response.send_message(embeds.SOURCE_UNAVAILABLE, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            tenant_id = derive_tenant_uuid(
                platform="discord", workspace_id=str(self.origin.guild_id)
            )
            name = self.agent.value.strip()
            agent = await find_agent_by_daimon_tag(
                runtime.anthropic, tenant_id=tenant_id, name=name
            )
            if agent is None:
                await interaction.followup.send(embeds.AGENT_UNAVAILABLE, ephemeral=True)
                return
            async with runtime.sessionmaker.begin() as session:
                policy = await load_access_policy(session, tenant_id=tenant_id)
                # These routines have no automatic destination, matching the other panels.
                if not authorize(
                    policy,
                    subject=Subject(),
                    action=Action.SAVE_ROUTINE,
                    surface=Surface.ROUTINE,
                    agent=build_agent_ref(agent.name, agent.metadata, name),
                    place=Place(),
                    origin=Place(
                        channel_id=str(channel.id),
                        parent_channel_id=source_channel_id if source_thread_id else None,
                    ),
                ):
                    await interaction.followup.send(embeds.SAVE_REFUSED, ephemeral=True)
                    return
                await create_routine_via_panel(
                    session,
                    tenant_id=tenant_id,
                    created_by_user_id=str(self.origin.user.id),
                    agent_id=str(agent.id),
                    agent_name=name,
                    cron_expr=cron,
                    timezone_=timezone,
                    trigger_message=self.message.value.strip(),
                    next_fire_at=next_fire_at,
                    channel_id=source_channel_id,
                    source_thread_id=source_thread_id,
                    source_channel_id=source_channel_id,
                    source_provenance_known=True,
                )
            await _rerender(self.origin, self.panel)
            await interaction.followup.send(
                embeds.created_message(name, cron, timezone),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except DaimonError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:  # routines-panel modal callback boundary
            await _send_callback_error(interaction, exc)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        from daimon.adapters.discord.routines_panel.panel import _send_callback_error

        await _send_callback_error(interaction, error)


class DeleteButton(discord.ui.Button["RoutinesPanelView"]):
    def __init__(self, state: RoutinesPanelState) -> None:
        super().__init__(
            label="Delete", style=discord.ButtonStyle.danger, disabled=state.selected is None
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.routines_panel.panel import _send_callback_error

        if self.view is None or self.view.state.selected is None:
            return
        row = self.view.state.selected.routine
        if not can_manage_routine(
            row, user_id=str(interaction.user.id), is_admin=checks.is_guild_admin(interaction)
        ):
            await interaction.response.send_message(embeds.DELETE_REFUSED, ephemeral=True)
            return
        try:
            # Keep this interaction's original response bound to the panel;
            # the confirmation is a separate ephemeral followup.
            await interaction.response.defer()
            await interaction.followup.send(
                embeds.DELETE_CONFIRM,
                view=DeleteConfirmation(self.view, interaction, row.id),
                ephemeral=True,
            )
        except Exception as exc:  # routines-panel button callback boundary
            await _send_callback_error(interaction, exc)


class DeleteConfirmation(discord.ui.View):
    def __init__(
        self, panel: RoutinesPanelView, origin: discord.Interaction, routine_id: uuid.UUID
    ) -> None:
        super().__init__(timeout=120)
        self.panel = panel
        self.origin = origin
        self.routine_id = routine_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.origin.user.id:
            await interaction.response.send_message(embeds.CONFIRM_USER_ONLY, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm delete", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button[DeleteConfirmation]
    ) -> None:
        from daimon.adapters.discord.routines_panel.panel import _rerender, _send_callback_error

        if not await self.interaction_check(interaction):
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.origin.guild_id))
        try:
            async with self.panel.runtime.sessionmaker.begin() as session:
                row = await get_routine(
                    session,
                    self.routine_id,
                    tenant_id=tenant_id,
                    recruitment=self.panel.runtime.settings.recruitment,
                )
                if row is None:
                    await interaction.response.send_message(embeds.GONE, ephemeral=True)
                    return
                if row.tenant_id != tenant_id:
                    await interaction.response.send_message(embeds.WRONG_TENANT, ephemeral=True)
                    return
                if not can_manage_routine(
                    row,
                    user_id=str(interaction.user.id),
                    is_admin=checks.is_guild_admin(interaction),
                ):
                    await interaction.response.send_message(embeds.DELETE_REFUSED, ephemeral=True)
                    return
                if not await delete_routine_via_panel(session, row.id, tenant_id=tenant_id):
                    await interaction.response.send_message(embeds.GONE, ephemeral=True)
                    return
            await interaction.response.edit_message(content=embeds.DELETED, view=None)
            self.stop()
            await _rerender(self.origin, self.panel)
        except Exception as exc:  # routines-panel confirmation callback boundary
            await _send_callback_error(interaction, exc)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button[DeleteConfirmation]
    ) -> None:
        await interaction.response.edit_message(content=embeds.CANCELLED, view=None)
        self.stop()
