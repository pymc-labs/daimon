"""Rules the chat `routines` panels share: status glyph, label, order and who may act.

Pure, apart from `channel_agent`, which reads the agent a channel answers with,
and `panel_save_refusal`, which reads the access policy.
"""

from __future__ import annotations

import uuid
from typing import Literal, cast

from daimon.core.authz import Action, AgentRef, Place, Surface, authorize, build_subject
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.turn.errors import AdmissionDenialReason
from sqlalchemy.ext.asyncio import AsyncSession

PANEL_CAP = 25

Glyph = Literal["⏸", "⏳", "❌", "✅"]


def derive_glyph(row: RoutineRow) -> Glyph:
    """Single-glyph precedence: Paused > Never-run > Error > Success.

    ``record_result`` clears ``last_error`` on success, so
    ``last_error is not None`` always reflects the most recent run.
    """
    if not row.enabled:
        return "⏸"
    if row.last_fired_at is None:
        return "⏳"
    if row.last_error is not None:
        return "❌"
    return "✅"


def routine_label(row: RoutineRow) -> str:
    """The trigger message's first 60 characters, or a hex-id fallback when blank."""
    stripped = row.trigger_message.strip()
    if not stripped:
        return f"routine {row.id.hex[:8]}"
    return stripped[:60]


def panel_rows(rows: list[RoutineRow]) -> tuple[list[RoutineRow], int]:
    """Rows sorted by label and capped at `PANEL_CAP`, plus how many the cap hid."""
    ordered = sorted(rows, key=lambda row: routine_label(row).lower())
    return ordered[:PANEL_CAP], max(0, len(ordered) - PANEL_CAP)


def can_manage_routine(row: RoutineRow, *, user_id: str | None, is_admin: bool) -> bool:
    """An admin, or the routine's recorded creator.

    Fails closed on both nulls: a caller with no user id owns nothing, and an
    ownerless routine is admin-only.
    """
    return is_admin or (user_id is not None and row.created_by_user_id == user_id)


def may_schedule_agent(
    agent_name: str, *, is_admin: bool, talking_to: bool = False, answers_here: str | None = None
) -> bool:
    """Who may make a routine for `agent_name`, the same in chat and on every panel.

    A routine runs as its agent, with that agent's repository, keys,
    connectors and memory, so a member may schedule only the agent they are
    talking to (`talking_to`) or the one the routine's channel answers with
    (`answers_here`, from `channel_agent`). Any agent is an admin's call.
    """
    return is_admin or talking_to or (answers_here is not None and answers_here == agent_name)


async def channel_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    default: DeploymentDefault,
) -> str | None:
    """The agent `channel_id` answers with (the workspace default without one)."""
    config = await resolve(
        session, context=ScopeContext(tenant_id=tenant_id, channel_id=channel_id), default=default
    )
    return config.agent_name


async def panel_save_refusal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    is_admin: bool,
    agent: AgentRef,
    channel_id: str | None,
) -> AdmissionDenialReason | None:
    """Why a panel may not save a routine with no destination, or None.

    The checks chat's `create_routine` runs inside an admitted turn: the
    creator must be someone the access policy lets start turns (a fire acts
    for them, `ACT_FOR_CREATOR`), and the agent must be one that may run with
    nowhere to post, saved from `channel_id` (`SAVE_ROUTINE`: a pinned agent
    needs one of its channels, and a channel kept to its own agents keeps its
    routines). Without them the panel would say a routine was created that
    every fire then refuses. Raises `AccessPolicyUnreadable` when the policy
    can't be read.
    """
    policy = await load_access_policy(session, tenant_id=tenant_id)
    subject = build_subject(is_admin=is_admin, platform_user_id=user_id)
    for action, origin in (
        (Action.ACT_FOR_CREATOR, None),
        (Action.SAVE_ROUTINE, Place(channel_id=channel_id) if channel_id else None),
    ):
        decision = authorize(
            policy,
            subject=subject,
            action=action,
            surface=Surface.ROUTINE,
            agent=agent if action is Action.SAVE_ROUTINE else None,
            place=Place(),
            origin=origin,
        )
        if not decision:
            return cast(AdmissionDenialReason, decision.reason)
    return None
