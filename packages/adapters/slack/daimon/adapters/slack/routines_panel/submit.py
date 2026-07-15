"""Create-Routine view_submission handler (pure evaluator + background run).

Two responsibilities, mirroring agent_setup/submit.py:

1. ``evaluate_routines_create_submission`` (PURE, synchronous):
   Validates the view_submission payload within the 3-second Socket Mode ack
   deadline. Returns a ``RoutinesCreateDecision`` carrying the
   ``response_action`` payload and whether the background run should proceed.
   No I/O.

2. ``run_routines_create_submission`` (async, background):
   Runs AFTER the ack. Re-checks ``is_admin`` server-side (fail-closed),
   validates the cron/timezone, resolves the agent on MA, then writes the
   routine row with ``created_by_user_id`` set to the submitting Slack user —
   the real user id the MCP path never carries.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic
import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.routines_panel.read import load_routines
from daimon.adapters.slack.routines_panel.state import RoutinesPanelState
from daimon.adapters.slack.routines_panel.views import build_content_view
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.cron import next_slot_at_or_after
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.stores.routines import create_routine, delete_routine, get_routine
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()


@dataclasses.dataclass(frozen=True)
class RoutinesCreateDecision:
    """Result of evaluate_routines_create_submission.

    response_payload: dict to pass as SocketModeResponse(payload=...) to ack.
    proceed:          True when validation passed and the background run should fire.
    extra:            Form-specific fields for the background run, keyed by name.
    """

    response_payload: dict[str, Any]
    proceed: bool
    extra: dict[str, Any]


# ---------------------------------------------------------------------------
# Shared payload-reading helpers
# ---------------------------------------------------------------------------


def _get_values(payload: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = payload.get("view") or {}
    view_state: dict[str, Any] = view.get("state") or {}
    return view_state.get("values") or {}


def _get_text(values: dict[str, Any], block_id: str, action_id: str) -> str:
    """Extract and strip a plain_text_input value from state.values."""
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(action_id) or {}
    return str(element.get("value") or "").strip()


def _get_selected(values: dict[str, Any], block_id: str, action_id: str) -> str:
    """Extract the selected static_select option value from state.values."""
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(action_id) or {}
    selected: dict[str, Any] = element.get("selected_option") or {}
    return str(selected.get("value") or "").strip()


# ---------------------------------------------------------------------------
# Pure evaluator
# ---------------------------------------------------------------------------


def evaluate_routines_create_submission(payload: dict[str, Any]) -> RoutinesCreateDecision:
    """Pure: validate the New Routine form submission.

    Checks all four fields are non-empty pre-ack. Cron/timezone correctness is
    deferred to ``run_*`` (slow I/O on the safe side of the budget) and reported
    via ephemeral. Returns:
      - proceed=False + response_action=errors keyed per block on empty fields
      - proceed=True + response_action=clear on success

    No I/O.
    """
    values = _get_values(payload)

    agent_name = _get_selected(values, "routines_create__agent", "routines_create__agent")
    cron_expr = _get_text(values, "routines_create__cron", "routines_create__cron")
    timezone_ = _get_text(values, "routines_create__timezone", "routines_create__timezone")
    trigger_message = _get_text(values, "routines_create__message", "routines_create__message")

    if not agent_name:
        return RoutinesCreateDecision(
            response_payload={
                "response_action": "errors",
                "errors": {"routines_create__agent": "Pick an agent for this routine."},
            },
            proceed=False,
            extra={},
        )
    if not cron_expr:
        return RoutinesCreateDecision(
            response_payload={
                "response_action": "errors",
                "errors": {"routines_create__cron": "A cron expression is required."},
            },
            proceed=False,
            extra={},
        )
    if not timezone_:
        return RoutinesCreateDecision(
            response_payload={
                "response_action": "errors",
                "errors": {"routines_create__timezone": "A timezone is required."},
            },
            proceed=False,
            extra={},
        )
    if not trigger_message:
        return RoutinesCreateDecision(
            response_payload={
                "response_action": "errors",
                "errors": {"routines_create__message": "A trigger message is required."},
            },
            proceed=False,
            extra={},
        )

    return RoutinesCreateDecision(
        response_payload={"response_action": "clear"},
        proceed=True,
        extra={
            "agent_name": agent_name,
            "cron_expr": cron_expr,
            "timezone": timezone_,
            "trigger_message": trigger_message,
        },
    )


# ---------------------------------------------------------------------------
# Background run helpers
# ---------------------------------------------------------------------------


def _dev_allow_all_admin(runtime: SlackRuntime) -> bool:
    """Read the testing-only admin-gate override from settings (default False)."""
    slack = runtime.settings.slack
    return slack is not None and slack.dev_allow_all_admin


async def _refuse_non_admin(
    web_client: AsyncWebClient,
    *,
    channel_id: str,
    user_id: str,
    dev_allow_all: bool = False,
) -> bool:
    """Re-check is_admin server-side; send ephemeral and return True if non-admin.

    Every mutating run_* calls this first. Returns True = caller should return
    early (refused). Returns False = admin confirmed, proceed.
    """
    is_admin = await resolve_is_admin(web_client, user_id=user_id, dev_allow_all=dev_allow_all)
    if not is_admin:
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=":x: You no longer have permission to create routines.",
        )
        return True
    return False


def _compute_next_fire_at(cron_expr: str, timezone_: str) -> datetime | None:
    """Validate timezone + cron and return the next fire datetime (UTC) or None.

    Returns None when the timezone or cron expression is invalid so the caller
    can report a clean ephemeral. Croniter raises a mix of ValueError/KeyError
    on bad expressions; catching all here is the named-boundary exception.
    """
    try:
        ZoneInfo(timezone_)
    except ZoneInfoNotFoundError:
        return None
    try:
        return next_slot_at_or_after(cron_expr, timezone_, datetime.now(UTC))
    except Exception:  # noqa: BLE001  # croniter raises mixed types; named boundary
        return None


# ---------------------------------------------------------------------------
# Background run (post-ack I/O)
# ---------------------------------------------------------------------------


async def run_routines_create_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    extra: dict[str, Any],
) -> None:
    """Post-ack: create a routine bound to the submitting Slack user.

    Re-checks is_admin before the write (fail-closed). The routine's
    ``created_by_user_id`` is the real Slack user id from the interaction — the
    whole reason this surface exists instead of the MCP create_routine tool.
    """
    try:
        refused = await _refuse_non_admin(
            web_client,
            channel_id=channel_id,
            user_id=user_id,
            dev_allow_all=_dev_allow_all_admin(runtime),
        )
        if refused:
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

        agent_name = str(extra.get("agent_name") or "")
        cron_expr = str(extra.get("cron_expr") or "")
        timezone_ = str(extra.get("timezone") or "")
        trigger_message = str(extra.get("trigger_message") or "")

        next_fire_at = _compute_next_fire_at(cron_expr, timezone_)
        if next_fire_at is None:
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=(
                    f":x: Could not schedule routine — invalid cron `{cron_expr}` "
                    f"or timezone `{timezone_}`."
                ),
            )
            return

        match = await find_agent_by_daimon_tag(
            runtime.anthropic, tenant_id=tenant_id, name=agent_name
        )
        if match is None:
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=f":x: No agent named `{agent_name}` found for this workspace.",
            )
            return

        async with runtime.sessionmaker() as session, session.begin():
            await create_routine(
                session,
                tenant_id=tenant_id,
                created_by_user_id=user_id,
                agent_id=str(match.id),
                agent_name=agent_name,
                cron_expr=cron_expr,
                timezone_=timezone_,
                trigger_message=trigger_message,
                next_fire_at=next_fire_at,
            )

        log.info(
            "slack.routines_create.created",
            team_id=team_id,
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone=timezone_,
        )
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":white_check_mark: Created routine on `{agent_name}` (`{cron_expr}`).",
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error("slack.routines_create_failed", team_id=team_id, exc_info=exc)
        capture_exception_with_scope(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":x: Failed to create routine: {type(exc).__name__}",
        )


async def run_routines_delete_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    routine_id: str,
    root_view_id: str,
) -> None:
    """Post-ack: delete a routine after its confirm modal is submitted.

    Re-checks tenant + authority (admin honoring dev_allow_all, OR the routine's
    creator) inside ``session.begin()`` (TOCTOU-safe, fail-closed), deletes the
    row, then refreshes the underlying panel view (``root_view_id``) in place and
    posts a confirmation ephemeral back to the invoking channel.
    """
    try:
        try:
            rid = uuid.UUID(routine_id)
        except ValueError:
            log.warning("slack.routines_delete.bad_routine_id", routine_id=routine_id)
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

        async with runtime.sessionmaker() as session, session.begin():
            row = await get_routine(session, rid)
            if row is None:
                await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text="This routine no longer exists.",
                )
                return
            if row.tenant_id != tenant_id:
                log.warning(
                    "slack.routines_delete.cross_tenant_refused",
                    routine_id=routine_id,
                    team_id=team_id,
                )
                return
            is_admin = await resolve_is_admin(
                web_client, user_id=user_id, dev_allow_all=_dev_allow_all_admin(runtime)
            )
            is_creator = row.created_by_user_id is not None and row.created_by_user_id == user_id
            if not (is_admin or is_creator):
                await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id or user_id,
                    user=user_id,
                    text=(
                        "Only the routine's creator or a workspace admin can delete this routine."
                    ),
                )
                return
            await delete_routine(session, rid)

        # Refresh the underlying panel in place (best-effort — the row is gone).
        async with runtime.sessionmaker() as session:
            entries, over_cap_count, agent_name_map = await load_routines(
                session, runtime.anthropic, tenant_id=tenant_id
            )
        state = RoutinesPanelState(
            rows=entries, over_cap_count=over_cap_count, agent_name_map=agent_name_map
        )
        if root_view_id:
            await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=root_view_id,
                view=build_content_view(state, channel_id=channel_id),
            )

        log.info("slack.routines_delete.deleted", team_id=team_id, routine_id=routine_id)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id or user_id,
            user=user_id,
            text=":wastebasket: Routine deleted.",
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error("slack.routines_delete_failed", team_id=team_id, exc_info=exc)
        capture_exception_with_scope(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id or user_id,
            user=user_id,
            text=f":x: Failed to delete routine: {type(exc).__name__}",
        )
