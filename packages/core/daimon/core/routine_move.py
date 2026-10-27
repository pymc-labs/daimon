"""Slot-safe routine handover validation and the versioned rollback wire format."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from daimon.core.cron import next_slot_at_or_after
from daimon.core.stores.domain import RoutineRow
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RoutineFreshnessSettings(BaseSettings):
    """Shared scheduler freshness setting, also read by the operator preview."""

    model_config = SettingsConfigDict(env_prefix="DAIMON_SCHEDULER__", extra="ignore")
    max_age_s: float = Field(
        default=900.0,
        description=(
            "Freshness window for routines with catch_up_policy=skip. Older slots "
            "are recorded as skipped and advanced; run-once routines ignore this window."
        ),
    )


class RoutineSeedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_routine_id: UUID
    last_completed_slot: datetime
    internal_next_run: datetime
    cron_expr: str
    timezone: str

    @field_validator("last_completed_slot", "internal_next_run", mode="before")
    @classmethod
    def require_iso_offset(cls, value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("timestamp must be an ISO-8601 timestamp with an offset")
        # Pydantic also accepts numeric epoch strings as datetimes. Handover
        # input must explicitly name an ISO timestamp and its offset instead.
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO-8601 timestamp with an offset") from exc
        if parsed.utcoffset() is None:
            raise ValueError("timestamp must have an offset")
        return parsed


def seed_first_slot(entry: RoutineSeedEntry, row: RoutineRow, *, now: datetime) -> datetime:
    """Validate the handover against OS state and use OS cron semantics."""
    if row.internal_routine_id != entry.internal_routine_id:
        raise ValueError("internal_routine_id differs from the Daimon routine")
    if entry.last_completed_slot > now:
        raise ValueError("last_completed_slot is in the future")
    if entry.cron_expr != row.cron_expr:
        raise ValueError("cron_expr differs from the Daimon routine")
    if entry.timezone != row.timezone:
        raise ValueError("timezone differs from the Daimon routine")
    if row.enabled:
        raise ValueError("routine is already enabled")
    if any(
        value is not None
        for value in (
            row.last_fired_at,
            row.last_fired_slot,
            row.last_finished_at,
            row.delivery_status,
            row.delivered_at,
            row.delivery_payload,
            row.delivery_note,
            row.delivery_lease_owner,
            row.delivery_lease_expires_at,
            row.last_result_tail,
            row.last_error,
        )
    ):
        raise ValueError("routine has already fired on Daimon or has a delivery record")
    slot = next_slot_at_or_after(row.cron_expr, row.timezone, entry.last_completed_slot)
    if slot != entry.internal_next_run:
        raise ValueError(
            f"first OS slot {slot.isoformat()} differs from internal_next_run "
            f"{entry.internal_next_run.isoformat()}"
        )
    return slot


def seed_scheduler_action(
    row: RoutineRow, slot: datetime, *, now: datetime, max_age: timedelta
) -> str:
    if slot > now:
        return "wait"
    if row.catch_up_policy == "skip" and slot < now - max_age:
        return "roll forward unrun under skip: older than the freshness window"
    return "fire next tick"


def routine_rollback_document(rows: list[RoutineRow], *, exported_at: datetime) -> dict[str, Any]:
    """The complete versioned rollback format; keep wire-format changes local here.

    Claims count as fired even if execution fails. Older claims without slot
    evidence cannot provide a safe watermark and prevent a final export.
    """
    reasons: list[str] = []
    entries: list[dict[str, Any]] = []

    def iso(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat() if value is not None else None

    for row in rows:
        label = str(row.internal_routine_id or row.id)
        # Each claim clears completion evidence; do not compare wall clocks,
        # which can move backwards between a claim and its completion.
        in_flight = (
            row.last_fired_at is not None or row.last_fired_slot is not None
        ) and row.last_finished_at is None
        if row.enabled:
            reasons.append(f"{label}: routine is enabled")
        if in_flight:
            reasons.append(f"{label}: claimed fire has no recorded completion")
        if row.delivery_status in ("pending", "claimed"):
            reasons.append(f"{label}: delivery is {row.delivery_status}")
        if row.last_fired_at is not None and row.last_fired_slot is None:
            reasons.append(f"{label}: historical fire has no recorded slot")
        resume_after = row.last_fired_slot or row.seeded_after
        if row.internal_routine_id is not None and resume_after is None:
            reasons.append(f"{row.internal_routine_id}: no fired slot or seeded T recorded")
        entries.append(
            {
                "internal_routine_id": (
                    str(row.internal_routine_id) if row.internal_routine_id is not None else None
                ),
                "routine_id": str(row.id),
                "agent_name": row.agent_name,
                "cron_expr": row.cron_expr,
                "timezone": row.timezone,
                "enabled": row.enabled,
                "last_fired_slot": iso(row.last_fired_slot),
                "last_fired_at": iso(row.last_fired_at),
                "in_flight": in_flight,
                "delivery_status": row.delivery_status,
                "delivered_at": iso(row.delivered_at),
                "resume_after": iso(resume_after),
            }
        )
    return {
        "format_version": 1,
        "exported_at": exported_at.astimezone(UTC).isoformat(),
        "final": not reasons,
        "not_final_reasons": reasons,
        "routines": entries,
    }
