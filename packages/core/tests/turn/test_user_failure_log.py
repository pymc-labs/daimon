"""A turn that leaves the user without an answer logs `turn.user_failure` once, content-free."""

from __future__ import annotations

import json
import uuid

import pytest
import structlog
from daimon.core.logging_setup import configure_log_level
from daimon.core.release import current_release
from daimon.core.turn import outcomes
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.core.turn.termination import (
    NOT_USER_FAILURES,
    USER_VISIBLE_FAILURES,
    TerminationReason,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from structlog.testing import capture_logs

_FIELDS = {
    "platform",
    "reason",
    "error_class",
    "origin",
    "release",
    "turn_id",
    "tenant_id",
}
_NOT_FAILURES = [
    TerminationReason.COMPLETED,
    TerminationReason.REFUSED,
    TerminationReason.ADMISSION_DENIED,
    TerminationReason.INTERRUPTED,
]


def test_every_reason_is_classified_exactly_once() -> None:
    members = set(TerminationReason)
    assert not USER_VISIBLE_FAILURES & NOT_USER_FAILURES, "a reason cannot be both"
    assert members == USER_VISIBLE_FAILURES | NOT_USER_FAILURES, (
        f"classify {sorted(members - USER_VISIBLE_FAILURES - NOT_USER_FAILURES)} in "
        "daimon.core.turn.termination"
    )


@pytest.fixture
def observation(monkeypatch: pytest.MonkeyPatch) -> TurnObservation:
    async def no_write(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(outcomes, "record", no_write)
    engine = create_async_engine("postgresql+asyncpg://test:test@localhost/test")
    return TurnObservation(
        async_sessionmaker(engine),
        uuid.uuid4(),
        "slack",
        channel_id="C-private",
        thread_id="T-private",
        session_id="sesn_private",
    )


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
    failures = [entry for entry in logs if entry["event"] == "turn.user_failure"]
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
    assert [entry for entry in logs if entry["event"] == "turn.user_failure"] == []
