"""Slot-safe routine handover validation and the versioned rollback wire format."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from daimon.core.cron import next_slot_at_or_after, slot_before_next
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

    source_routine_id: UUID
    last_completed_slot: datetime
    source_next_run: datetime
    cron_expr: str
    timezone: str

    @field_validator("last_completed_slot", "source_next_run", mode="before")
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


def routine_never_fired(row: RoutineRow) -> bool:
    """No execution or delivery evidence, as required for linking and seeding.

    Edits may mark delivery skipped with a note before any fire. Skip-window
    bookkeeping also records scheduling decisions, not executions. Neither
    rules out handover; retained payloads, leases and fire evidence still do.
    """
    return not any(
        value is not None
        for value in (
            row.last_fired_at,
            row.last_fired_slot,
            row.last_finished_at,
            row.last_finished_claim_at,
            row.delivery_status if row.delivery_status != "skipped" else None,
            row.delivered_at,
            row.delivery_payload,
            row.delivery_note if row.delivery_status != "skipped" else None,
            row.delivery_lease_owner,
            row.delivery_lease_expires_at,
            row.last_result_tail,
            row.last_error,
        )
    )


def seed_first_slot(entry: RoutineSeedEntry, row: RoutineRow, *, now: datetime) -> datetime:
    """Validate the handover against Daimon state and use its cron semantics."""
    if row.source_routine_id != entry.source_routine_id:
        raise ValueError("source_routine_id differs from the Daimon routine")
    if entry.last_completed_slot > now:
        raise ValueError("last_completed_slot is in the future")
    if entry.cron_expr != row.cron_expr:
        raise ValueError("cron_expr differs from the Daimon routine")
    if entry.timezone != row.timezone:
        raise ValueError("timezone differs from the Daimon routine")
    if row.enabled:
        raise ValueError("routine is already enabled")
    if not routine_never_fired(row):
        raise ValueError("routine has already fired on Daimon or has a delivery record")
    slot = next_slot_at_or_after(row.cron_expr, row.timezone, entry.last_completed_slot)
    if slot != entry.source_next_run:
        raise ValueError(
            f"first Daimon slot {slot.isoformat()} differs from source_next_run "
            f"{entry.source_next_run.isoformat()}"
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

    Handback consumes every slot before the captured next run. Other paused
    rows use claim or seed evidence. Incomplete evidence prevents finality.
    """
    if not rows:
        raise ValueError("Select at least one routine to export.")
    reasons: list[str] = []
    entries: list[dict[str, Any]] = []

    def iso(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat() if value is not None else None

    for row in rows:
        label = str(row.source_routine_id or row.id)
        # Each claim clears completion evidence; do not compare wall clocks,
        # which can move backwards between a claim and its completion.
        in_flight = (
            row.last_fired_at is not None or row.last_fired_slot is not None
        ) and row.last_finished_at is None
        if row.last_finished_at is not None and (
            row.last_fired_at is None or row.last_finished_claim_at != row.last_fired_at
        ):
            reasons.append(
                f"{label}: claim and completion records disagree; an older scheduler may have run"
            )
        if row.enabled:
            reasons.append(f"{label}: routine is enabled")
        if in_flight:
            reasons.append(f"{label}: claimed fire has no recorded completion")
        if row.delivery_status in ("pending", "claimed"):
            reasons.append(f"{label}: delivery is {row.delivery_status}")
        if (row.last_fired_slot is None) != (row.last_fired_at is None):
            reasons.append(f"{label}: fired slot and claim time are incomplete")
        if row.delivery_status == "delivered" and row.delivered_at is None:
            reasons.append(f"{label}: delivered output has no delivery time")
        resume_after = row.last_fired_slot or row.seeded_after
        expected_next_run = None
        enabled = row.enabled
        if row.handback_paused_at is not None:
            enabled = row.handback_enabled
            expected_next_run = row.handback_next_fire_at
            if enabled is None:
                reasons.append(f"{label}: handback has no recorded on or off value")
                enabled = False
            if expected_next_run is not None:
                try:
                    resume_after = slot_before_next(row.cron_expr, row.timezone, expected_next_run)
                except (ValueError, KeyError) as exc:
                    resume_after = None
                    reasons.append(f"{label}: {exc}")
            elif enabled:
                reasons.append(f"{label}: handback has no captured next run")
        if resume_after is not None:
            if row.last_fired_slot is not None and resume_after < row.last_fired_slot:
                reasons.append(f"{label}: resume_after is before the last fired slot")
            if resume_after > exported_at:
                reasons.append(f"{label}: resume_after is in the future")
        if row.source_routine_id is not None and resume_after is None:
            reasons.append(f"{row.source_routine_id}: no fired slot or seeded T recorded")
        entries.append(
            {
                "source_routine_id": (
                    str(row.source_routine_id) if row.source_routine_id is not None else None
                ),
                "routine_id": str(row.id),
                "agent_name": row.agent_name,
                "cron_expr": row.cron_expr,
                "timezone": row.timezone,
                "enabled": enabled,
                "last_fired_slot": iso(row.last_fired_slot),
                "last_fired_at": iso(row.last_fired_at),
                "in_flight": in_flight,
                "delivery_status": row.delivery_status,
                "delivered_at": iso(row.delivered_at),
                "resume_after": iso(resume_after),
                "expected_next_run": iso(expected_next_run),
            }
        )
    return {
        "format_version": 2,
        "exported_at": exported_at.astimezone(UTC).isoformat(),
        "final": not reasons,
        "not_final_reasons": reasons,
        "routines": entries,
    }
