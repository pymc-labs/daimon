"""RoutinesPanelView with routine creation, deletion and run controls.

V2 Components format: the panel is a Container with ActionRows for controls
appended inside the card, no embed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import anthropic
import sentry_sdk
import structlog
from daimon.adapters.discord import checks, layout
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.routines_panel.embeds import build_panel_container
from daimon.adapters.discord.routines_panel.forms import DeleteButton, NewButton
from daimon.adapters.discord.routines_panel.read import load_guild_routines
from daimon.adapters.discord.routines_panel.state import RoutinesPanelState, state_label
from daimon.adapters.discord.routines_panel.write import (
    pause_routine_via_panel,
    resume_routine_via_panel,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize, build_agent_ref
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.permissions import any_agent_rules
from daimon.core.posted_controls.routines import apply_routine_action
from daimon.core.routines import can_read_routine_output
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import get_routine, update_routine
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.exc import SQLAlchemyError

import discord

__all__ = ["RoutinesPanelView"]

log = structlog.get_logger()


async def _send_callback_error(interaction: discord.Interaction, exc: Exception) -> None:
    """Best-effort ephemeral error surface for a failed panel button/select callback.

    Always logs and captures to Sentry. The user-visible text
    is generic; the exception detail stays server-side. The send itself is
    best-effort (its own try/except that only logs) -- a failure here must
    never re-raise out of the caller's boundary.
    """
    log.exception("routines_panel_callback_failed", error=str(exc))
    sentry_sdk.capture_exception(exc)
    error_embed = build_notice_embed("Something went wrong.\n\nTry again.")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed, ephemeral=True)
    except discord.HTTPException:
        log.exception("routines_panel_callback_error_send_failed")


class _RoutinePicker(discord.ui.Select["RoutinesPanelView"]):
    def __init__(self, state: RoutinesPanelState) -> None:
        if state.rows:
            options = [
                discord.SelectOption(
                    label=entry.label,
                    value=str(entry.routine.id),
                    description=(f"{state_label(entry.glyph)}: {entry.agent_name}")[:100],
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
        except Exception as exc:  # routines-panel button-callback boundary
            await _send_callback_error(interaction, exc)


class _DestinationPicker(discord.ui.Select["RoutinesPanelView"]):
    def __init__(self, state: RoutinesPanelState) -> None:
        super().__init__(
            placeholder="Change delivery… (owner only)",
            options=[
                discord.SelectOption(label="Direct message to me", value="dm"),
                discord.SelectOption(label="No automatic delivery", value="none"),
            ],
            disabled=state.selected is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or self.view.state.selected is None:
            return
        selected = self.view.state.selected.routine
        try:
            async with self.view.runtime.sessionmaker.begin() as session:
                row = await get_routine(session, selected.id, tenant_id=selected.tenant_id)
                if row is None or row.created_by_user_id != str(interaction.user.id):
                    await interaction.response.send_message(
                        "Only the routine owner can change private delivery.", ephemeral=True
                    )
                    return
                policy = await load_access_policy(session, tenant_id=row.tenant_id)
                agent_ref = AgentRef.of(row.agent_name)
                if self.values[0] == "dm" and any_agent_rules(policy):
                    agent = await find_agent_by_daimon_tag(
                        self.view.runtime.anthropic, tenant_id=row.tenant_id, name=row.agent_name
                    )
                    if agent is None:
                        await interaction.response.send_message(
                            "The routine agent is unavailable.", ephemeral=True
                        )
                        return
                    agent_ref = build_agent_ref(agent.name, agent.metadata, row.agent_name)
                if self.values[0] == "dm" and not authorize(
                    policy,
                    subject=Subject(),
                    action=Action.SAVE_ROUTINE,
                    surface=Surface.ROUTINE,
                    agent=agent_ref,
                    place=Place(),
                ):
                    await interaction.response.send_message(
                        "This agent cannot run a private routine.", ephemeral=True
                    )
                    return
                await update_routine(
                    session,
                    row.id,
                    tenant_id=row.tenant_id,
                    expected_updated_at=row.updated_at,
                    destination_kind="dm" if self.values[0] == "dm" else None,
                    clear_destination=self.values[0] == "none",
                    edited_by=str(interaction.user.id),
                )
            await _rerender(interaction, self.view)
        except DaimonError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception as exc:  # routines-panel callback boundary
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

        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        try:
            async with runtime.sessionmaker() as session, session.begin():

                async def allowed(row: RoutineRow) -> bool:
                    is_admin = await checks.is_guild_admin(
                        interaction, action=DelegatedAction.ROUTINES
                    )
                    is_creator = (
                        row.created_by_user_id is not None
                        and row.created_by_user_id == str(interaction.user.id)
                    )
                    return is_admin or is_creator

                result = await apply_routine_action(
                    session,
                    routine_id,
                    tenant_id=tenant_id,
                    op="toggle",
                    allowed=allowed,
                    load=get_routine,
                    pause=pause_routine_via_panel,
                    resume=resume_routine_via_panel,
                    now=lambda: datetime.now(UTC),
                    check_tenant=True,
                )
                refusals = {
                    "already_running": "This routine is already on.",
                    "gone": "This routine no longer exists.",
                    "wrong_tenant": "This routine is from another server.",
                    "not_owner": ("Only its creator or a server admin can pause it."),
                }
                if result != "changed":
                    await interaction.response.send_message(
                        embed=build_notice_embed(refusals[result]), ephemeral=True
                    )
                    return

            await _rerender(interaction, self.view)
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await _send_callback_error(interaction, exc)
        except Exception as exc:  # routines-panel button-callback boundary
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
                ViewLastOutputSubView,
            )

            row = self.view.state.selected.routine
            async with self.view.runtime.sessionmaker() as session:
                current = await get_routine(session, row.id, tenant_id=row.tenant_id)
            if current is None or (
                not can_read_routine_output(current, user_id=str(interaction.user.id))
            ):
                await interaction.response.send_message(
                    "Only the owner can read a private routine result.", ephemeral=True
                )
                return
            row = current
            await interaction.response.send_message(
                view=ViewLastOutputSubView(row, allowed_user_id=self.view.allowed_user_id),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await _send_callback_error(interaction, exc)
        except Exception as exc:  # routines-panel button-callback boundary
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
        except Exception as exc:  # routines-panel button-callback boundary
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
        except Exception as exc:  # routines-panel button-callback boundary
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
        container.add_item(discord.ui.ActionRow(_DestinationPicker(state)))
        container.add_item(discord.ui.ActionRow(NewButton(), DeleteButton(state)))
        container.add_item(buttons_row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                embed=build_notice_embed("Only the person who opened this can use it."),
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
            raise UserFacingError(
                "Daimon isn't set up on this server yet.\n\nAsk an admin to set it up."
            )
        entries, over_cap_count, agent_name_map = await load_guild_routines(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            viewer_user_id=str(view.allowed_user_id),
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
