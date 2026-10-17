"""Create form and explicit delete confirmation for the routines panel."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import sentry_sdk
import structlog
from daimon.adapters.discord import theme
from daimon.adapters.discord.routines_panel.state import RoutineEntry
from daimon.adapters.discord.routines_panel.write import (
    create_routine_via_panel,
    delete_routine_via_panel,
)
from daimon.core.cron import InvalidScheduleError, validated_next_slot
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.routines import can_manage_routine
from daimon.core.stores.routines import get_routine

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.routines_panel.panel import RoutinesPanelView

log = structlog.get_logger()


def _is_admin(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )


async def _error(interaction: discord.Interaction, exc: Exception) -> None:
    from daimon.adapters.discord.routines_panel.panel import send_callback_error

    await send_callback_error(interaction, exc)


async def _refresh_after_write(
    interaction: discord.Interaction, panel: RoutinesPanelView, *, message: str
) -> None:
    from daimon.adapters.discord.routines_panel.panel import rerender

    try:
        await rerender(interaction, panel)
    except Exception as exc:  # committed mutation: never invite a duplicate write
        log.exception("routines_panel_refresh_after_write_failed")
        sentry_sdk.capture_exception(exc)
        message += " Open /routines again to see the updated list."
    try:
        await interaction.followup.send(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
    except discord.HTTPException as exc:
        log.exception("routines_panel_success_notice_failed")
        sentry_sdk.capture_exception(exc)


class NewRoutineButton(discord.ui.Button["RoutinesPanelView"]):
    def __init__(self) -> None:
        super().__init__(label="New routine", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or not await self.view.interaction_check(interaction):
            return
        if (
            not _is_admin(interaction)
            or interaction.guild_id is None
            or interaction.channel_id is None
        ):
            await interaction.response.send_message(
                "Only a server admin can create routines.", ephemeral=True
            )
            return
        if not self.view.state.agent_name_map:
            await interaction.response.send_message(
                "No agents are available. Set up an agent, then refresh this panel.", ephemeral=True
            )
            return
        try:
            await interaction.response.send_modal(
                CreateRoutineModal(
                    self.view, guild_id=interaction.guild_id, channel_id=interaction.channel_id
                )
            )
        except Exception as exc:  # routines-panel button-callback boundary
            await _error(interaction, exc)


class CreateRoutineModal(discord.ui.Modal):
    def __init__(self, panel: RoutinesPanelView, *, guild_id: int, channel_id: int) -> None:
        super().__init__(title="New routine", timeout=600)
        self.panel = panel
        self.guild_id = guild_id
        self.channel_id = channel_id
        self._lock = asyncio.Lock()
        self._created = False
        agents = sorted(
            panel.state.agent_name_map.items(), key=lambda item: (item[1].lower(), item[0])
        )
        self.agent_ids = {agent_id for agent_id, _ in agents[:25]}
        agent_label: discord.ui.Label[CreateRoutineModal] = discord.ui.Label(
            text="Agent",
            description=(
                "First 25 agents shown; ask an agent in chat for others."
                if len(agents) > 25
                else None
            ),
            component=discord.ui.Select(
                placeholder="Choose an agent",
                options=[
                    discord.SelectOption(label=name[:100], value=agent_id)
                    for agent_id, name in agents[:25]
                ],
            ),
        )
        self.agent = cast("discord.ui.Select[CreateRoutineModal]", agent_label.component)
        cron_label: discord.ui.Label[CreateRoutineModal] = discord.ui.Label(
            text="Schedule (cron)",
            description="Five fields: minute, hour, day, month, weekday.",
            component=discord.ui.TextInput(placeholder="0 9 * * 1-5", max_length=200),
        )
        self.cron = cast("discord.ui.TextInput[CreateRoutineModal]", cron_label.component)
        timezone_label: discord.ui.Label[CreateRoutineModal] = discord.ui.Label(
            text="Time zone",
            description="An IANA time zone, such as America/New_York.",
            component=discord.ui.TextInput(default="UTC", max_length=100),
        )
        self.timezone = cast("discord.ui.TextInput[CreateRoutineModal]", timezone_label.component)
        message_label: discord.ui.Label[CreateRoutineModal] = discord.ui.Label(
            text="What to ask the agent",
            component=discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=4000),
        )
        self.message = cast("discord.ui.TextInput[CreateRoutineModal]", message_label.component)
        for item in (agent_label, cron_label, timezone_label, message_label):
            self.add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self.panel.interaction_check(interaction):
            return False
        if interaction.guild_id != self.guild_id or interaction.channel_id != self.channel_id:
            await interaction.response.send_message(
                "Open /routines in this server again.", ephemeral=True
            )
            return False
        return True

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Only a server admin can create routines.", ephemeral=True
            )
            return
        cron, timezone, message = (
            self.cron.value.strip(),
            self.timezone.value.strip(),
            self.message.value.strip(),
        )
        if not self.agent.values or self.agent.values[0] not in self.agent_ids:
            await interaction.response.send_message(
                "Choose an agent from this form.", ephemeral=True
            )
            return
        if not cron or not timezone or not message:
            await interaction.response.send_message(
                "Fill in the schedule, time zone and message.", ephemeral=True
            )
            return
        try:
            next_fire = validated_next_slot(cron, timezone, datetime.now(UTC))
        except InvalidScheduleError:
            await interaction.response.send_message(
                "That schedule or time zone isn't valid. Check both and try again.", ephemeral=True
            )
            return
        await interaction.response.defer()
        async with self._lock:
            if self._created:
                await interaction.followup.send(
                    "This form already created a routine. Refresh the panel.", ephemeral=True
                )
                return
            try:
                tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.guild_id))
                agents = await list_agents_by_tenant(
                    self.panel.runtime.anthropic, tenant_id=tenant_id
                )
                agent = next((agent for agent in agents if agent.id == self.agent.values[0]), None)
                if agent is None:
                    await interaction.followup.send(
                        "That agent is no longer available. Refresh the panel.", ephemeral=True
                    )
                    return
                async with self.panel.runtime.sessionmaker() as session, session.begin():
                    await create_routine_via_panel(
                        session,
                        tenant_id=tenant_id,
                        created_by_user_id=str(interaction.user.id),
                        agent_id=agent.id,
                        agent_name=agent.metadata.get("daimon_name") or agent.id,
                        cron_expr=cron,
                        timezone_=timezone,
                        trigger_message=message,
                        next_fire_at=next_fire,
                        channel_id=str(self.channel_id),
                    )
                self._created = True
            except Exception as exc:  # routines-panel modal submission boundary
                await _error(interaction, exc)
                return
            await _refresh_after_write(interaction, self.panel, message="Routine created.")


class DeleteRoutineButton(discord.ui.Button["RoutinesPanelView"]):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(label="Delete…", style=discord.ButtonStyle.danger, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or self.view.state.selected is None:
            return
        if not await self.view.interaction_check(interaction):
            return
        try:
            confirmation = DeleteRoutineConfirmView(
                self.view, self.view.state.selected, guild_id=interaction.guild_id
            )
            if not await confirmation.interaction_check(interaction):
                return
            await interaction.response.defer()
            if not await confirmation.authorized(interaction):
                return
            await interaction.edit_original_response(
                view=confirmation, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception as exc:  # routines-panel button-callback boundary
            await _error(interaction, exc)


class DeleteRoutineConfirmView(discord.ui.LayoutView):
    def __init__(
        self, panel: RoutinesPanelView, entry: RoutineEntry, *, guild_id: int | None
    ) -> None:
        super().__init__(timeout=600)
        self.panel = panel
        self.routine_id = entry.routine.id
        self.guild_id = guild_id
        self._lock = asyncio.Lock()
        self._finished = False
        confirm: discord.ui.Button[DeleteRoutineConfirmView] = discord.ui.Button(
            label="Delete routine", style=discord.ButtonStyle.danger
        )
        confirm.callback = self.confirm  # type: ignore[method-assign]
        cancel: discord.ui.Button[DeleteRoutineConfirmView] = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary
        )
        cancel.callback = self.cancel  # type: ignore[method-assign]
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## Delete this routine?"),
                discord.ui.TextDisplay(discord.utils.escape_markdown(entry.label)),
                discord.ui.TextDisplay(
                    f"Agent: {discord.utils.escape_markdown(entry.agent_name)}\n"
                    f"Schedule: `{entry.routine.cron_expr}` ({entry.routine.timezone})\n\n"
                    "This cannot be undone."
                ),
                discord.ui.ActionRow(confirm, cancel),
                accent_colour=theme.COLOR_RED,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self.panel.interaction_check(interaction):
            return False
        if self.guild_id is None or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "Open /routines in this server again.", ephemeral=True
            )
            return False
        return True

    async def authorized(self, interaction: discord.Interaction) -> bool:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.guild_id))
        async with self.panel.runtime.sessionmaker() as session:
            row = await get_routine(session, self.routine_id, tenant_id=tenant_id)
        if row is None or row.tenant_id != tenant_id:
            await interaction.followup.send("This routine no longer exists.", ephemeral=True)
            return False
        if not can_manage_routine(
            row, user_id=str(interaction.user.id), is_admin=_is_admin(interaction)
        ):
            await interaction.followup.send(
                "Only its creator or a server admin can delete it.", ephemeral=True
            )
            return False
        return True

    async def confirm(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        async with self._lock:
            if self._finished:
                await interaction.followup.send(
                    "This confirmation is already closed. Open /routines again.", ephemeral=True
                )
                return
            tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.guild_id))
            try:
                async with self.panel.runtime.sessionmaker() as session, session.begin():
                    row = await get_routine(session, self.routine_id, tenant_id=tenant_id)
                    if row is None or row.tenant_id != tenant_id:
                        await interaction.followup.send(
                            "This routine no longer exists.", ephemeral=True
                        )
                        return
                    if not can_manage_routine(
                        row, user_id=str(interaction.user.id), is_admin=_is_admin(interaction)
                    ):
                        await interaction.followup.send(
                            "Only its creator or a server admin can delete it.", ephemeral=True
                        )
                        return
                    deleted = await delete_routine_via_panel(
                        session, self.routine_id, tenant_id=tenant_id
                    )
                self._finished = True
                self.stop()
            except Exception as exc:  # routines-panel confirmation boundary
                await _error(interaction, exc)
                return
            await _refresh_after_write(
                interaction,
                self.panel,
                message="Routine deleted." if deleted else "This routine no longer exists.",
            )

    async def cancel(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        async with self._lock:
            if self._finished:
                await interaction.followup.send(
                    "This confirmation is already closed. Open /routines again.", ephemeral=True
                )
                return
            self._finished = True
            self.stop()
            from daimon.adapters.discord.routines_panel.panel import rerender

            try:
                await rerender(interaction, self.panel)
            except Exception as exc:  # routines-panel button-callback boundary
                await _error(interaction, exc)
