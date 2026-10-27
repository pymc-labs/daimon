"""Pure slot handover and rollback format invariants."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.routine_move import (
    RoutineFreshnessSettings,
    RoutineSeedEntry,
    routine_rollback_document,
    seed_first_slot,
    seed_scheduler_action,
)
from daimon.core.stores.domain import RoutineRow
from pydantic import ValidationError

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def row(**changes: object) -> RoutineRow:
    values = dict(
        id=uuid.uuid4(),
        internal_routine_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=None,
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 * * * *",
        timezone="UTC",
        trigger_message="run",
        enabled=False,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(changes)
    return RoutineRow.model_validate(values)


def entry(routine: RoutineRow, timestamp: str, **changes: object) -> RoutineSeedEntry:
    return RoutineSeedEntry.model_validate(
        dict(
            internal_routine_id=routine.internal_routine_id,
            last_completed_slot=timestamp,
            internal_next_run="2026-10-27T11:00:00Z",
            cron_expr=routine.cron_expr,
            timezone=routine.timezone,
        )
        | changes
    )


def test_exact_slot_is_strictly_after() -> None:
    routine = row()
    assert seed_first_slot(
        entry(routine, "2026-10-27T10:00:00+00:00"), routine, now=NOW
    ) == datetime(2026, 10, 27, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-03-07T09:00:00-05:00", datetime(2026, 3, 8, 13, tzinfo=UTC)),
        ("2026-10-31T09:00:00-04:00", datetime(2026, 11, 1, 14, tzinfo=UTC)),
    ],
)
def test_dst_uses_routine_zone(timestamp: str, expected: datetime) -> None:
    routine = row(cron_expr="0 9 * * *", timezone="America/New_York")
    assert (
        seed_first_slot(
            entry(routine, timestamp, internal_next_run=expected.isoformat()),
            routine,
            now=datetime(2026, 12, 1, tzinfo=UTC),
        )
        == expected
    )


@pytest.mark.parametrize(
    "timestamp", ["2026-10-27T10:00:00", "2026-10-27", None, 1793095200, "1793095200"]
)
def test_seed_requires_iso_offset(timestamp: object) -> None:
    with pytest.raises(ValidationError):
        RoutineSeedEntry.model_validate(
            dict(
                internal_routine_id=uuid.uuid4(),
                last_completed_slot=timestamp,
                internal_next_run="2026-10-27T11:00:00Z",
                cron_expr="0 * * * *",
                timezone="UTC",
            )
        )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"last_completed_slot": "2026-10-28T00:00:00Z"}, "future"),
        ({"cron_expr": "* * * * *"}, "cron_expr differs"),
        ({"timezone": "Europe/London"}, "timezone differs"),
        ({"internal_next_run": "2026-10-27T11:01:00Z"}, "differs from internal_next_run"),
    ],
)
def test_seed_refuses_future_and_mismatch(changes: dict[str, str], message: str) -> None:
    routine = row()
    values = entry(routine, "2026-10-27T10:00:00Z").model_dump(mode="json") | changes
    with pytest.raises(ValueError, match=message):
        seed_first_slot(RoutineSeedEntry.model_validate(values), routine, now=NOW)


@pytest.mark.parametrize(
    "evidence",
    [
        {"enabled": True},
        {"last_fired_at": NOW},
        {"last_fired_slot": NOW},
        {"delivery_status": "skipped"},
        {"delivered_at": NOW},
        {"delivery_payload": "pending"},
    ],
)
def test_seed_refuses_os_evidence(evidence: dict[str, object]) -> None:
    routine = row(**evidence)
    with pytest.raises(ValueError, match="already"):
        seed_first_slot(entry(routine, "2026-10-27T10:00:00Z"), routine, now=NOW)


@pytest.mark.parametrize(
    "policy, age, expected",
    [
        ("skip", -1, "wait"),
        ("skip", 900, "fire next tick"),
        ("skip", 901, "roll forward unrun"),
        ("run-once", 90000, "fire next tick"),
    ],
)
def test_scheduler_preview(policy: str, age: int, expected: str) -> None:
    assert seed_scheduler_action(
        row(catch_up_policy=policy),
        NOW - timedelta(seconds=age),
        now=NOW,
        max_age=timedelta(minutes=15),
    ).startswith(expected)


def test_freshness_setting_reads_scheduler_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_SCHEDULER__MAX_AGE_S", "123")
    assert RoutineFreshnessSettings().max_age_s == 123


@pytest.mark.parametrize(
    "changes, final",
    [
        ({}, True),
        ({"enabled": True}, False),
        ({"last_fired_at": NOW, "last_fired_slot": NOW}, False),
        ({"last_fired_at": NOW, "last_fired_slot": NOW, "last_finished_at": NOW}, True),
        (
            {
                "last_fired_at": NOW,
                "last_fired_slot": NOW,
                "last_finished_at": NOW - timedelta(seconds=1),
            },
            True,
        ),
        ({"last_fired_slot": NOW}, False),
        ({"delivery_status": "pending"}, False),
        ({"delivery_status": "claimed"}, False),
        ({"delivery_status": "delivered"}, True),
        ({"last_fired_at": NOW, "last_finished_at": NOW}, False),
    ],
)
def test_export_final(changes: dict[str, object], final: bool) -> None:
    document = routine_rollback_document(
        [row(**({"internal_routine_id": None} | changes))], exported_at=NOW
    )
    assert document["final"] is final
    assert bool(document["not_final_reasons"]) is not final


def test_export_watermark_is_slot_never_claim_wall_time() -> None:
    slot = NOW - timedelta(days=1)
    document = routine_rollback_document(
        [row(last_fired_slot=slot, last_fired_at=NOW)], exported_at=NOW
    )
    assert document["routines"][0]["resume_after"] == slot.isoformat()
    seeded = routine_rollback_document([row(seeded_after=slot)], exported_at=NOW)
    assert seeded["routines"][0]["resume_after"] == slot.isoformat()
    assert seeded["final"]
    empty = routine_rollback_document([row(internal_routine_id=None)], exported_at=NOW)["routines"][
        0
    ]
    assert empty["resume_after"] is None
    assert set(empty) == {
        "internal_routine_id",
        "routine_id",
        "agent_name",
        "cron_expr",
        "timezone",
        "enabled",
        "last_fired_slot",
        "last_fired_at",
        "in_flight",
        "delivery_status",
        "delivered_at",
        "resume_after",
    }
    assert empty["internal_routine_id"] is None


@pytest.mark.parametrize(
    "field",
    ["internal_routine_id", "last_completed_slot", "internal_next_run", "cron_expr", "timezone"],
)
def test_every_frozen_seed_field_is_required(field):
    values = entry(row(), "2026-10-27T10:00:00Z").model_dump(mode="json")
    del values[field]
    with pytest.raises(ValidationError, match=field):
        RoutineSeedEntry.model_validate(values)


def test_internal_next_run_requires_offset():
    with pytest.raises(ValidationError, match="internal_next_run"):
        entry(row(), "2026-10-27T10:00:00Z", internal_next_run="2026-10-27T11:00:00")


def test_same_instant_with_different_offset_agrees():
    routine = row()
    assert seed_first_slot(
        entry(routine, "2026-10-27T06:00:00-04:00", internal_next_run="2026-10-27T07:00:00-04:00"),
        routine,
        now=NOW,
    ) == NOW - timedelta(hours=1)


def test_export_seed_fallback_never_hides_historical_unknown_claim():
    document = routine_rollback_document(
        [row(seeded_after=NOW, last_fired_at=NOW)], exported_at=NOW
    )
    assert not document["final"]
    assert "historical fire" in " ".join(document["not_final_reasons"])
