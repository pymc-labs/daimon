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
        {"delivery_status": "pending"},
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
        (
            {
                "last_fired_at": NOW,
                "last_fired_slot": NOW,
                "last_finished_at": NOW,
                "last_finished_claim_at": NOW,
            },
            True,
        ),
        (
            {
                "last_fired_at": NOW,
                "last_fired_slot": NOW,
                "last_finished_at": NOW - timedelta(seconds=1),
                "last_finished_claim_at": NOW,
            },
            True,
        ),
        ({"last_fired_slot": NOW}, False),
        ({"delivery_status": "pending"}, False),
        ({"delivery_status": "claimed"}, False),
        ({"delivery_status": "delivered"}, False),
        ({"delivery_status": "delivered", "delivered_at": NOW}, True),
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
        "expected_next_run",
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


@pytest.mark.parametrize("shift", [-60, 60])
def test_old_scheduler_claim_after_new_completion_is_never_final(shift):
    previous_claim = NOW - timedelta(hours=1)
    claimed_at = previous_claim + timedelta(seconds=shift)
    document = routine_rollback_document(
        [
            row(
                last_fired_slot=previous_claim,
                last_fired_at=claimed_at,
                last_finished_at=previous_claim + timedelta(seconds=5),
                last_finished_claim_at=previous_claim,
            )
        ],
        exported_at=NOW,
    )
    assert not document["final"]
    assert "older scheduler" in " ".join(document["not_final_reasons"])
    assert document["routines"][0]["resume_after"] == previous_claim.isoformat()


def test_empty_export_cannot_be_final():
    with pytest.raises(ValueError, match="at least one"):
        routine_rollback_document([], exported_at=NOW)


def test_edit_only_skipped_delivery_is_not_a_fire():
    routine = row(delivery_status="skipped", delivery_note="private_revision_changed")
    assert seed_first_slot(entry(routine, "2026-10-27T10:00:00Z"), routine, now=NOW)


@pytest.mark.parametrize(
    "zone, cron, next_run, expected_previous",
    [
        ("UTC", "0 * * * *", "2026-10-27T13:00:00+00:00", "2026-10-27T12:00:00+00:00"),
        ("America/New_York", "0 9 * * *", "2026-03-08T13:00:00+00:00", "2026-03-07T14:00:00+00:00"),
        ("America/New_York", "0 9 * * *", "2026-11-01T14:00:00+00:00", "2026-10-31T13:00:00+00:00"),
    ],
)
def test_handback_preserves_enabled_and_next_run_across_dst(
    zone, cron, next_run, expected_previous
):
    from daimon.core.cron import next_slot_at_or_after

    next_run = datetime.fromisoformat(next_run)
    previous = datetime.fromisoformat(expected_previous)
    # A run-once claim coalesced several old slots. The reverse seed must
    # consume those slots too, rather than resuming after the old claimed slot.
    claim = previous - timedelta(days=3)
    document = routine_rollback_document(
        [
            row(
                timezone=zone,
                cron_expr=cron,
                catch_up_policy="run-once",
                handback_paused_at=next_run - timedelta(minutes=1),
                handback_enabled=True,
                handback_next_fire_at=next_run,
                last_fired_slot=claim,
                last_fired_at=previous,
                last_finished_at=previous,
                last_finished_claim_at=previous,
            )
        ],
        exported_at=next_run,
    )
    assert document["final"], document["not_final_reasons"]
    result = document["routines"][0]
    assert result["enabled"] is True
    assert result["resume_after"] == expected_previous
    assert result["expected_next_run"] == next_run.isoformat()
    assert (
        next_slot_at_or_after(cron, zone, datetime.fromisoformat(result["resume_after"]))
        == next_run
    )


def test_user_paused_routine_remains_off_without_expected_next_run():
    document = routine_rollback_document([row(seeded_after=NOW)], exported_at=NOW)
    result = document["routines"][0]
    assert document["final"]
    assert result["enabled"] is False
    assert result["expected_next_run"] is None
    assert result["resume_after"] == NOW.isoformat()


@pytest.mark.parametrize(
    "changes, reason",
    [
        (
            {
                "handback_paused_at": NOW,
                "handback_enabled": True,
                "handback_next_fire_at": NOW,
                "last_fired_slot": NOW,
                "last_fired_at": NOW,
            },
            "before the last fired slot",
        ),
        ({"seeded_after": NOW + timedelta(seconds=1)}, "in the future"),
        ({"last_fired_slot": NOW}, "fired slot and claim time are incomplete"),
        ({"last_fired_at": NOW}, "fired slot and claim time are incomplete"),
        ({"delivery_status": "delivered", "seeded_after": NOW}, "no delivery time"),
        ({"handback_paused_at": NOW, "handback_enabled": True}, "no captured next run"),
        (
            {
                "handback_paused_at": NOW,
                "handback_enabled": True,
                "handback_next_fire_at": NOW + timedelta(minutes=1),
            },
            "preceding slot",
        ),
    ],
)
def test_export_refuses_inconsistent_handback_records(changes, reason):
    document = routine_rollback_document([row(**changes)], exported_at=NOW)
    assert not document["final"]
    assert reason in " ".join(document["not_final_reasons"])


def test_uninvertible_dst_slot_is_not_final():
    # No 02:30 exists on New York's spring-forward day. Never bless a
    # captured 03:30 as 02:30 just because a different cron engine did.
    document = routine_rollback_document(
        [
            row(
                cron_expr="30 2 * * *",
                timezone="America/New_York",
                handback_paused_at=NOW,
                handback_enabled=True,
                handback_next_fire_at=datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
            )
        ],
        exported_at=NOW,
    )
    assert not document["final"]
    assert "preceding slot" in " ".join(document["not_final_reasons"])


def test_handback_of_user_paused_routine_preserves_off_state():
    document = routine_rollback_document(
        [row(seeded_after=NOW, handback_paused_at=NOW, handback_enabled=False)], exported_at=NOW
    )
    assert document["final"]
    result = document["routines"][0]
    assert result["enabled"] is False
    assert result["expected_next_run"] is None
    assert result["resume_after"] == NOW.isoformat()
