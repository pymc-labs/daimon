"""A turn that leaves the user without an answer logs `turn.user_failure` once, content-free."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
import structlog
from daimon.core import scheduler
from daimon.core.logging_setup import configure_log_level
from daimon.core.release import current_release
from daimon.core.scheduler import run_one_tick
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.turn_outcomes import OutcomeRecord
from daimon.core.turn import outcomes
from daimon.core.turn.outcomes import (
    TurnObservation,
    drain_outcomes,
    finish_with_error,
    observe_turn,
    record_refusal,
)
from daimon.core.turn.termination import (
    NOT_USER_FAILURES,
    USER_VISIBLE_FAILURES,
    TerminationReason,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from structlog.testing import capture_logs

_FIELDS = {
    "platform",
    "reason",
    "error_class",
    "origin",
    "backend",
    "release",
    "turn_id",
    "tenant_id",
}
_NOT_FAILURES = [
    TerminationReason.COMPLETED,
    TerminationReason.REFUSED,
    TerminationReason.ADMISSION_DENIED,
    TerminationReason.INTERRUPTED,
    TerminationReason.INTERRUPT_TIMEOUT,
    TerminationReason.MISSING_CONFIG,
    TerminationReason.SESSION_AGENT_MISMATCH,
]


def test_every_reason_is_classified_exactly_once() -> None:
    members = set(TerminationReason)
    assert not USER_VISIBLE_FAILURES & NOT_USER_FAILURES, "a reason cannot be both"
    assert members == USER_VISIBLE_FAILURES | NOT_USER_FAILURES, (
        f"classify {sorted(members - USER_VISIBLE_FAILURES - NOT_USER_FAILURES)} in "
        "daimon.core.turn.termination"
    )


def test_setup_stop_and_handover_ends_are_not_user_failures() -> None:
    assert {
        TerminationReason.MISSING_CONFIG,
        TerminationReason.INTERRUPT_TIMEOUT,
        TerminationReason.SESSION_AGENT_MISMATCH,
    } <= NOT_USER_FAILURES


@pytest.fixture(autouse=True)
def fresh_release() -> Iterator[None]:
    current_release.cache_clear()
    yield
    current_release.cache_clear()


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[OutcomeRecord]:
    """The outcome rows the turn boundary writes, without a database."""
    rows: list[OutcomeRecord] = []

    async def capture(session: object, row: OutcomeRecord) -> None:
        rows.append(row)

    monkeypatch.setattr(outcomes, "record", capture)
    return rows


@pytest.fixture
def sessionmaker(written: list[OutcomeRecord]) -> async_sessionmaker[AsyncSession]:
    # Never connects: the write is captured above.
    return async_sessionmaker(create_async_engine("postgresql+asyncpg://test:test@localhost/test"))


@pytest.fixture
def observation(sessionmaker: async_sessionmaker[AsyncSession]) -> TurnObservation:
    return TurnObservation(
        sessionmaker,
        uuid.uuid4(),
        "slack",
        channel_id="C-private",
        thread_id="T-private",
        session_id="sesn_private",
        backend="gemini/gemini.inline_reuse",
    )


def _failures(logs: list[structlog.typing.EventDict]) -> list[structlog.typing.EventDict]:
    return [entry for entry in logs if entry["event"] == "turn.user_failure"]


async def test_a_failed_turn_logs_one_content_free_line(
    observation: TurnObservation, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_log_level("INFO")
    try:
        observation.finish(reason=TerminationReason.UPSTREAM, error=ValueError("sensitive text"))
        observation.finish(reason=TerminationReason.UPSTREAM)  # finish is once-only
        await drain_outcomes()
    finally:
        structlog.reset_defaults()

    (line,) = capsys.readouterr().out.splitlines()
    record = json.loads(line)
    assert record["event"] == "turn.user_failure"
    assert record["severity"] == "WARNING"
    assert {key: record[key] for key in _FIELDS} == {
        "platform": "slack",
        "reason": "upstream",
        "error_class": "ValueError",
        "origin": "chat",
        "backend": "gemini/gemini.inline_reuse",
        "release": current_release(),
        "turn_id": str(observation.id),
        "tenant_id": str(observation.tenant_id),
    }
    for private in ("C-private", "T-private", "sesn_private", "sensitive text"):
        assert private not in line
    for field in ("channel_id", "thread_id", "session_id"):
        assert field not in record


@pytest.mark.parametrize("reason", sorted(USER_VISIBLE_FAILURES))
async def test_every_user_visible_failure_logs(
    observation: TurnObservation, reason: TerminationReason
) -> None:
    with capture_logs() as logs:
        observation.finish(reason=reason)
        await drain_outcomes()
    failures = _failures(logs)
    assert [entry["reason"] for entry in failures] == [reason.value]
    assert failures[0]["log_level"] == "warning"
    assert failures[0]["error_class"] is None
    assert set(failures[0]) - {"event", "log_level"} == _FIELDS


@pytest.mark.parametrize("reason", _NOT_FAILURES)
async def test_ends_that_are_not_failures_log_nothing(
    observation: TurnObservation, reason: TerminationReason
) -> None:
    with capture_logs() as logs:
        observation.finish(reason=reason)
        await drain_outcomes()
    assert _failures(logs) == []


async def test_a_command_line_turn_never_logs(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    cli = TurnObservation(sessionmaker, None, "cli")
    with capture_logs() as logs:
        cli.finish(reason=TerminationReason.UPSTREAM)
        await drain_outcomes()
    assert _failures(logs) == []


async def test_an_unprompted_turn_records_its_failure_but_never_logs_it(
    sessionmaker: async_sessionmaker[AsyncSession], written: list[OutcomeRecord]
) -> None:
    tenant = uuid.uuid4()
    with capture_logs() as logs:
        with observe_turn(sessionmaker, tenant_id=tenant, platform="discord", alertable=False):
            outcome = outcomes.current_outcome.get()
            assert outcome is not None
            # An unprompted turn ending in a resolver miss, as admission finishes it.
            outcome.finish(reason=TerminationReason.RESOLVER_MISS)
        await drain_outcomes()
    assert _failures(logs) == []
    assert [row.reason for row in written] == [TerminationReason.RESOLVER_MISS]


async def test_an_unprompted_concurrency_shed_is_recorded_but_not_logged(
    sessionmaker: async_sessionmaker[AsyncSession], written: list[OutcomeRecord]
) -> None:
    tenant = uuid.uuid4()
    with capture_logs() as logs:
        record_refusal(sessionmaker, tenant_id=tenant, platform="discord", channel_id="c")
        record_refusal(
            sessionmaker, tenant_id=tenant, platform="discord", channel_id="c", alertable=False
        )
        await drain_outcomes()
    assert len(written) == 2, "both are recorded"
    assert len(_failures(logs)) == 1, "only the one somebody asked for is logged"


async def test_a_boundary_that_returns_without_finishing_records_unknown_but_logs_nothing(
    sessionmaker: async_sessionmaker[AsyncSession], written: list[OutcomeRecord]
) -> None:
    """An MCP dispatch returns before the turn ends; that is not a failed answer."""
    with capture_logs() as logs:
        with observe_turn(sessionmaker, tenant_id=uuid.uuid4(), platform="mcp"):
            pass
        await drain_outcomes()
    assert _failures(logs) == []
    assert [row.reason for row in written] == [TerminationReason.UNKNOWN]


async def test_an_exception_inside_the_boundary_still_logs_once(
    sessionmaker: async_sessionmaker[AsyncSession], written: list[OutcomeRecord]
) -> None:
    with (
        capture_logs() as logs,
        pytest.raises(RuntimeError),
        observe_turn(sessionmaker, tenant_id=uuid.uuid4(), platform="mcp"),
    ):
        raise RuntimeError("boom")
    await drain_outcomes()
    (failure,) = _failures(logs)
    assert failure["reason"] == "unknown"
    assert failure["error_class"] == "RuntimeError"
    assert len(written) == 1


async def test_an_explicit_finish_inside_the_boundary_logs(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    with capture_logs() as logs:
        with observe_turn(sessionmaker, tenant_id=uuid.uuid4(), platform="discord") as row:
            row.finish(reason=TerminationReason.UPSTREAM)
        await drain_outcomes()
    assert [entry["reason"] for entry in _failures(logs)] == ["upstream"]


async def test_a_routine_fire_that_returns_without_finishing_logs_nothing(
    monkeypatch: pytest.MonkeyPatch,
    sessionmaker: async_sessionmaker[AsyncSession],
    written: list[OutcomeRecord],
) -> None:
    """A policy-skipped fire ends normally without finishing its observation."""
    row = cast(
        RoutineRow,
        SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            agent_id="agent",
            created_by_user_id=None,
            updated_at=None,
        ),
    )

    async def nothing(*args: object, **kwargs: object) -> None:
        return None

    async def claim(*args: object, **kwargs: object) -> list[RoutineRow]:
        return [row]

    fired: list[uuid.UUID] = []

    async def skipped(fired_row: RoutineRow) -> None:
        fired.append(fired_row.id)

    class NoCaps:
        async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool:
            return False

    monkeypatch.setattr(scheduler, "advance_stale", nothing)
    monkeypatch.setattr(scheduler, "claim_due_fireable", claim)
    monkeypatch.setattr(scheduler, "skip_slots_during_fire", nothing)

    async def no_intake_routines(*args: object) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
        return frozenset()

    monkeypatch.setattr(scheduler, "configured_routines", no_intake_routines)
    with capture_logs() as logs:
        await run_one_tick(
            now=datetime(2026, 1, 1, tzinfo=UTC),
            sm=sessionmaker,
            caps=NoCaps(),
            fire=skipped,
            max_age=timedelta(hours=1),
            max_concurrent_fires=1,
            dispatch_timeout_s=5,
            wait_for_completion=True,
        )
        await drain_outcomes()
    assert fired == [row.id]
    assert _failures(logs) == []
    assert [r.reason for r in written] == [TerminationReason.UNKNOWN]


async def test_a_failure_the_adapter_shows_and_swallows_still_logs_once(
    sessionmaker: async_sessionmaker[AsyncSession], written: list[OutcomeRecord]
) -> None:
    """A Teams or wizard turn that posts its own failure card and returns."""
    with capture_logs() as logs:
        with observe_turn(sessionmaker, tenant_id=uuid.uuid4(), platform="teams"):
            try:
                raise ConnectionError("graph unavailable")
            except ConnectionError as exc:
                finish_with_error(exc)
        await drain_outcomes()
    failures = _failures(logs)
    assert [(f["reason"], f["error_class"]) for f in failures] == [("unknown", "ConnectionError")]
    assert [row.reason for row in written] == [TerminationReason.UNKNOWN]


async def test_swallowing_after_the_turn_already_ended_changes_nothing(
    sessionmaker: async_sessionmaker[AsyncSession], written: list[OutcomeRecord]
) -> None:
    with capture_logs() as logs:
        with observe_turn(sessionmaker, tenant_id=uuid.uuid4(), platform="teams"):
            outcome = outcomes.current_outcome.get()
            assert outcome is not None
            outcome.finish(reason=TerminationReason.COMPLETED)
            finish_with_error(RuntimeError("card update failed after the answer"))
        await drain_outcomes()
    assert _failures(logs) == []
    assert [row.reason for row in written] == [TerminationReason.COMPLETED]
