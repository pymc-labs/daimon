"""RoutinesPanelView (LayoutView) + picker + pause/resume button.

The panel is read-mostly: the only write is a Pause/Resume toggle gated on
creator-or-manage_guild at click time (TOCTOU-safe refetch inside the
session.begin() block).

V2 Components format: the panel is a Container with ActionRows for controls
appended inside the card, no embed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import anthropic
import sentry_sdk
import structlog
from daimon.adapters.discord import layout
from daimon.adapters.discord.routines_panel.embeds import build_panel_container
from daimon.adapters.discord.routines_panel.read import load_guild_routines
from daimon.adapters.discord.routines_panel.state import RoutinesPanelState, state_label
from daimon.adapters.discord.routines_panel.write import (
    pause_routine_via_panel,
    resume_routine_via_panel,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.routines import get_routine
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.exc import SQLAlchemyError

import discord

__all__ = ["RoutinesPanelView"]

log = structlog.get_logger()


async def _send_callback_error(interaction: discord.Interaction, exc: Exception) -> None:
    """Best-effort ephemeral error surface for a failed panel button/select callback.

    T-95-08: always logs + captures to Sentry. T-95-07: the user-visible text
    is generic; the exception detail stays server-side. The send itself is
    best-effort (its own try/except that only logs) -- a failure here must
    never re-raise out of the caller's boundary.
    """
    log.exception("routines_panel_callback_failed", error=str(exc))
    sentry_sdk.capture_exception(exc)
    error_text = "Something went wrong handling that click. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(error_text, ephemeral=True)
        else:
            await interaction.response.send_message(error_text, ephemeral=True)
    except discord.HTTPException:
        log.exception("routines_panel_callback_error_send_failed")


class _RoutinePicker(discord.ui.Select["RoutinesPanelView"]):
    def __init__(self, state: RoutinesPanelState) -> None:
        if state.rows:
            options = [
                discord.SelectOption(
                    label=entry.label,
                    value=str(entry.routine.id),
                    description=(f"{entry.glyph} {state_label(entry.glyph)} · {entry.agent_name}")[
                        :100
                    ],
                    default=(
                        state.selected is not None and entry.routine.id == state.selected.routine.id
                    ),
                )
                for entry in state.rows[:25]
            ]
            disabled = False
        else:
            options = [discord.SelectOption(label="(no routines)", value="__none__")]
            disabled = True
        super().__init__(
            placeholder="Switch routine…",
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        chosen = self.values[0]
        if chosen == "__none__" or self.view is None:
            return
        try:
            routine_id = uuid.UUID(chosen)
        except ValueError:
            return
        try:
            self.view.state.select(routine_id)
            new_view = RoutinesPanelView(
                self.view.state,
                runtime=self.view.runtime,
                allowed_user_id=self.view.allowed_user_id,
            )
            await interaction.response.edit_message(
                view=new_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await _send_callback_error(interaction, exc)
        except Exception as exc:  # noqa: BLE001 — routines-panel button-callback boundary
            await _send_callback_error(interaction, exc)


class _PauseButton(discord.ui.Button["RoutinesPanelView"]):
    def __init__(self, state: RoutinesPanelState) -> None:
        if state.selected is not None and not state.selected.routine.enabled:
            label = "▶ Resume"
            style = discord.ButtonStyle.success
        else:
            label = "⏸ Pause"
            style = discord.ButtonStyle.danger
        super().__init__(
            label=label,
            style=style,
            disabled=(state.selected is None),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        state = self.view.state
        if state.selected is None:
            return
        routine_id = state.selected.routine.id
        runtime = self.view.runtime

        try:
            async with runtime.sessionmaker() as session, session.begin():
                row = await get_routine(session, routine_id)
                if row is None:
                    await interaction.response.send_message(
                        "This routine no longer exists.",
                        ephemeral=True,
                    )
                    return
                if row.tenant_id != derive_tenant_uuid(
                    platform="discord", workspace_id=str(interaction.guild_id)
                ):
                    await interaction.response.send_message(
                        "This routine does not belong to this guild.",
                        ephemeral=True,
                    )
                    return
                member = interaction.user
                is_admin = (
                    isinstance(member, discord.Member) and member.guild_permissions.manage_guild
                )
                is_creator = row.created_by_user_id is not None and row.created_by_user_id == str(
                    interaction.user.id
                )
                if not (is_admin or is_creator):
                    await interaction.response.send_message(
                        "Only the routine's creator or a guild admin can pause this routine.",
                        ephemeral=True,
                    )
                    return
                now = datetime.now(UTC)
                if row.enabled:
                    await pause_routine_via_panel(session, routine_id)
                else:
                    await resume_routine_via_panel(session, routine_id, now=now)

            await _rerender(interaction, self.view)
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await _send_callback_error(interaction, exc)
        except Exception as exc:  # noqa: BLE001 — routines-panel button-callback boundary
            await _send_callback_error(interaction, exc)


class _ViewOutputButton(discord.ui.Button["RoutinesPanelView"]):
    def __init__(self, state: RoutinesPanelState) -> None:
        disabled = state.selected is None or state.selected.routine.last_fired_at is None
        super().__init__(
            label="📜 View last output",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or self.view.state.selected is None:
            return
        try:
            from daimon.adapters.discord.routines_panel.subviews import (
                ViewLastOutputSubView,  # noqa: PLC0415
            )

            row = self.view.state.selected.routine
            await interaction.response.send_message(
                view=ViewLastOutputSubView(row, allowed_user_id=self.view.allowed_user_id),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await _send_callback_error(interaction, exc)
        except Exception as exc:  # noqa: BLE001 — routines-panel button-callback boundary
            await _send_callback_error(interaction, exc)


class _RefreshButton(discord.ui.Button["RoutinesPanelView"]):
    def __init__(self) -> None:
        super().__init__(
            label="🔄 Refresh",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        try:
            await _rerender(interaction, self.view)
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await _send_callback_error(interaction, exc)
        except Exception as exc:  # noqa: BLE001 — routines-panel button-callback boundary
            await _send_callback_error(interaction, exc)


class _DoneButton(discord.ui.Button["RoutinesPanelView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Done",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        try:
            now = datetime.now(UTC)
            await interaction.response.edit_message(
                view=layout.static_view(build_panel_container(self.view.state, now=now)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await _send_callback_error(interaction, exc)
        except Exception as exc:  # noqa: BLE001 — routines-panel button-callback boundary
            await _send_callback_error(interaction, exc)


class RoutinesPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        state: RoutinesPanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__(timeout=600)
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id

        container = build_panel_container(state, now=datetime.now(UTC))
        picker_row: discord.ui.ActionRow[RoutinesPanelView] = discord.ui.ActionRow(
            _RoutinePicker(state)
        )
        buttons_row: discord.ui.ActionRow[RoutinesPanelView] = discord.ui.ActionRow(
            _PauseButton(state),
            _ViewOutputButton(state),
            _RefreshButton(),
            _DoneButton(),
        )
        container.add_item(layout.hairline())
        container.add_item(picker_row)
        container.add_item(buttons_row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.",
                ephemeral=True,
            )
            return False
        return True


async def _rerender(interaction: discord.Interaction, view: RoutinesPanelView) -> None:
    """Re-query and re-render the panel from scratch."""
    runtime = view.runtime
    guild_id = str(interaction.guild_id)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    async with runtime.sessionmaker() as session:
        row = await get_tenant(session, tenant_id)
        if row is None:
            raise DaimonError("This server is not registered.")
        entries, over_cap_count, agent_name_map = await load_guild_routines(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
        )
    new_state = RoutinesPanelState.initial(
        rows=entries,
        over_cap_count=over_cap_count,
        agent_name_map=agent_name_map,
    )
    # Preserve selection across refreshes when the row still exists.
    if view.state.selected is not None:
        new_state.select(view.state.selected.routine.id)

    new_view = RoutinesPanelView(
        new_state,
        runtime=runtime,
        allowed_user_id=view.allowed_user_id,
    )
    if interaction.response.is_done():
        await interaction.edit_original_response(
            view=new_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await interaction.response.edit_message(
            view=new_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
