"""Slack /routines slash handler + overflow pause/resume/output actions.

Shell module: all I/O lives here. Pure builders (views.py) and reducers
(state.py) are called but never catch exceptions — failures propagate to the
listener-boundary catch in this module (S3 pattern).

Handler contract:
  handle_routines_command(runtime, payload)
    Slash command entry: open loading modal → background-fetch routines
    → views.update with the content view. No TRY/EXCEPT in the pure callers.

  handle_routine_action(runtime, payload)
    Block-action entry for action_id matching ``routine_action:{uuid}`` or
    ``routines_refresh``. TOCTOU-safe write for pause/resume (re-fetch +
    tenant/authority check inside session.begin()). Ephemeral for output.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import anthropic
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.routines_panel.read import load_routines
from daimon.adapters.slack.routines_panel.state import RoutinesPanelState, picker_label
from daimon.adapters.slack.routines_panel.views import (
    build_content_view,
    build_create_routine_modal,
    build_delete_confirm_modal,
    build_last_output_view,
    build_loading_view,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.stores.routines import get_routine, pause_routine, resume_routine
from slack_sdk.errors import SlackApiError
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()


def _dev_allow_all_admin(runtime: SlackRuntime) -> bool:
    """Read the testing-only admin-gate override from settings (default False)."""
    slack = runtime.settings.slack
    return slack is not None and slack.dev_allow_all_admin


async def handle_routines_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Slash command handler for /routines (loading-modal pattern).

    Immediately opens a "Loading…" modal with the fresh trigger_id (beats the
    ~3s expiry), then background-fetches routines and updates the modal in place.

    Args:
        runtime: Injected SlackRuntime (sessionmaker, anthropic, settings).
        payload: Slash-command payload from the Socket Mode envelope.
    """
    team_id: str = payload.get("team_id") or ""
    trigger_id: str = payload.get("trigger_id") or ""
    channel_id: str = payload.get("channel_id") or ""

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.routines_command.no_token", team_id=team_id)
        return

    try:
        # Open loading modal immediately — must beat the ~3s trigger_id TTL.
        resp = await client.views_open(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=build_loading_view(channel_id=channel_id),
        )
        view_id: str = resp["view"]["id"]  # pyright: ignore[reportUnknownVariableType, reportAssignmentType, reportOptionalSubscript]  # SlackResponse subscript is untyped

        # Slow path (off the 3s window): resolve tenant + fetch routines.
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        async with runtime.sessionmaker() as session:
            entries, over_cap_count, agent_name_map = await load_routines(
                session, runtime.anthropic, tenant_id=tenant_id
            )
        state = RoutinesPanelState(
            rows=entries,
            over_cap_count=over_cap_count,
            agent_name_map=agent_name_map,
        )
        # No hash — single updater, Pitfall 3.
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,  # pyright: ignore[reportUnknownArgumentType]  # view_id carries Unknown from SlackResponse subscript
            view=build_content_view(state, channel_id=channel_id),
        )

    except (DaimonError, anthropic.APIError, SlackApiError, InvalidToken, SQLAlchemyError) as exc:
        log.error("slack.routines_command_failed", team_id=team_id, exc_info=exc)
        capture_exception_with_scope(exc)


async def handle_routine_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Block-action handler for overflow (⋯) and Refresh actions in the routines panel.

    Dispatches on the parsed ``value`` from the selected overflow option:
    - ``pause``/``resume``: TOCTOU-safe write inside ``session.begin()``.
    - ``output``: ``chat.postEphemeral`` with the fenced last-output text.
    - ``refresh`` (action_id ``routines_refresh``): re-read + ``views.update``.

    Authority gate (re-checked at click time, not stored in view state):
      Admin (resolve_is_admin) OR creator (row.created_by_user_id == user_id).

    Tenant gate (inside session.begin()):
      ``row.tenant_id != derive_tenant_uuid("slack", team_id)`` → refused.

    Args:
        runtime: Injected SlackRuntime.
        payload: block_actions payload from the Socket Mode interactive envelope.
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    team_id: str = team_info.get("id") or payload.get("team_id") or ""  # type: ignore[assignment]
    user_info: dict[str, Any] = payload.get("user") or {}
    user_id: str = user_info.get("id") or ""
    actions: list[dict[str, Any]] = payload.get("actions") or []
    view_info: dict[str, Any] = payload.get("view") or {}
    view_id: str = view_info.get("id") or ""
    channel_id: str = view_info.get("private_metadata") or ""

    if not actions:
        return

    action = actions[0]
    action_id: str = action.get("action_id") or ""

    # Parse routine_id and action value from the action_id string.
    if action_id.startswith("routine_action:"):
        routine_id_str = action_id.removeprefix("routine_action:")
        selected_opt: dict[str, Any] = action.get("selected_option") or {}
        action_value: str = selected_opt.get("value") or ""
    elif action_id == "routines_refresh":
        routine_id_str = ""
        action_value = "refresh"
    elif action_id == "routines_create":
        routine_id_str = ""
        action_value = "create"
    else:
        return  # unknown action — ignore

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.routines_action.no_token", team_id=team_id)
        return

    try:
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

        if action_value in ("pause", "resume"):
            try:
                routine_id = uuid.UUID(routine_id_str)
            except ValueError:
                log.warning("slack.routines_action.bad_routine_id", action_id=action_id)
                return

            async with runtime.sessionmaker() as session, session.begin():
                # TOCTOU-safe: re-fetch and validate inside session.begin().
                # Tenant scoping is enforced by the store (tenant_id kwarg).
                row = await get_routine(session, routine_id, tenant_id=tenant_id)
                if row is None:
                    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                        channel=channel_id or user_id,
                        user=user_id,
                        text="This routine no longer exists.",
                    )
                    return
                # T-82-06: authority check at click time (TOCTOU-safe).
                is_admin = await resolve_is_admin(client, user_id=user_id)
                is_creator = (
                    row.created_by_user_id is not None and row.created_by_user_id == user_id
                )
                if not (is_admin or is_creator):
                    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                        channel=channel_id or user_id,
                        user=user_id,
                        text=(
                            "Only the routine's creator or a workspace admin "
                            "can pause or resume this routine."
                        ),
                    )
                    return

                now = datetime.now(UTC)
                if action_value == "pause":
                    await pause_routine(session, routine_id, tenant_id=tenant_id)
                else:
                    await resume_routine(session, routine_id, tenant_id=tenant_id, now=now)

            # Re-render after successful write.
            async with runtime.sessionmaker() as session:
                entries, over_cap_count, agent_name_map = await load_routines(
                    session, runtime.anthropic, tenant_id=tenant_id
                )
            state = RoutinesPanelState(
                rows=entries,
                over_cap_count=over_cap_count,
                agent_name_map=agent_name_map,
            )
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_content_view(state, channel_id=channel_id),
            )

        elif action_value == "delete":
            try:
                routine_id = uuid.UUID(routine_id_str)
            except ValueError:
                log.warning("slack.routines_action.bad_routine_id", action_id=action_id)
                return

            async with runtime.sessionmaker() as session:
                row = await get_routine(session, routine_id, tenant_id=tenant_id)

            if row is None:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text="This routine no longer exists.",
                )
                return
            # Authority gate before showing the confirm modal (re-checked at
            # submit for TOCTOU safety). Admin (honoring dev_allow_all) OR creator.
            is_admin = await resolve_is_admin(
                client, user_id=user_id, dev_allow_all=_dev_allow_all_admin(runtime)
            )
            is_creator = row.created_by_user_id is not None and row.created_by_user_id == user_id
            if not (is_admin or is_creator):
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=(
                        "Only the routine's creator or a workspace admin can delete this routine."
                    ),
                )
                return

            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_delete_confirm_modal(
                    team_id=team_id,
                    channel_id=channel_id,
                    routine_id=routine_id_str,
                    root_view_id=view_id,
                    label=picker_label(row),
                ),
            )

        elif action_value == "output":
            try:
                routine_id = uuid.UUID(routine_id_str)
            except ValueError:
                log.warning("slack.routines_action.bad_routine_id", action_id=action_id)
                return

            async with runtime.sessionmaker() as session:
                row = await get_routine(session, routine_id, tenant_id=tenant_id)

            if row is None:
                return

            if row.last_error is not None:
                output_text = row.last_error
            else:
                output_text = row.last_result_tail or "(no output)"

            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id or user_id,
                user=user_id,
                text=build_last_output_view(output_text),
            )

        elif action_value == "refresh":
            async with runtime.sessionmaker() as session:
                entries, over_cap_count, agent_name_map = await load_routines(
                    session, runtime.anthropic, tenant_id=tenant_id
                )
            state = RoutinesPanelState(
                rows=entries,
                over_cap_count=over_cap_count,
                agent_name_map=agent_name_map,
            )
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_content_view(state, channel_id=channel_id),
            )

        elif action_value == "create":
            # Admin gate at click time (honors dev_allow_all). Refused → ephemeral.
            is_admin = await resolve_is_admin(
                client, user_id=user_id, dev_allow_all=_dev_allow_all_admin(runtime)
            )
            if not is_admin:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=":x: Only a workspace admin can create routines.",
                )
                return

            agents = await list_agents_by_tenant(runtime.anthropic, tenant_id=tenant_id)
            agent_names: list[str] = []
            for agent in agents:
                name = agent.metadata.get(MA_METADATA_KEY_NAME)
                if name is not None:
                    agent_names.append(name)

            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=payload.get("trigger_id") or "",
                view=build_create_routine_modal(
                    team_id=team_id,
                    channel_id=channel_id,
                    agent_names=agent_names,
                ),
            )

    except (DaimonError, anthropic.APIError, SlackApiError, InvalidToken, SQLAlchemyError) as exc:
        log.error(
            "slack.routine_action_failed",
            team_id=team_id,
            action_id=action_id,
            exc_info=exc,
        )
        capture_exception_with_scope(exc)
