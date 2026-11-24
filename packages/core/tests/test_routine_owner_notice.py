"""A routine's owner is told of a failure once per failing streak, from fixed facts only."""

import asyncio
import uuid
from datetime import UTC, datetime

from daimon.core.lead_intake_execution import current_intake_execution
from daimon.core.routine_owner_notice import (
    owner_notice_card,
    owner_notice_facts,
    owner_notice_prompt,
)
from daimon.core.scheduler import drain_owner_notices, tell_owner
from daimon.core.stores.domain import RoutineRow
from daimon.core.turn.termination import TerminationReason

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
RAW = "RuntimeError: quota for sk-secret exceeded on file q3-salaries.xlsx"


def _row(**fields: object) -> RoutineRow:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "created_by_user_id": "123",
        "agent_id": "ag_1",
        "agent_name": "analyst",
        "name": "Weekly readout",
        "cron_expr": "0 9 * * 1",
        "timezone": "UTC",
        "trigger_message": "summarise",
        "enabled": True,
        "next_fire_at": None,
        "last_fired_at": None,
        "last_error": None,
        "last_result_tail": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return RoutineRow.model_validate({**base, **fields})


def test_the_notice_says_fixed_facts_and_never_the_raw_error() -> None:
    facts = owner_notice_facts(
        _row(last_error=RAW),
        TerminationReason.CEILING,
        platform="discord",
        agent_name="Analyst",
        channel_told=True,
        now=NOW,
        link="https://app.example.com/routines/1",
    )
    title, body = owner_notice_card(facts)
    for text in (owner_notice_prompt(facts), title, body):
        assert "sk-secret" not in text and "salaries" not in text, "no raw error text"
    assert "Weekly readout" in title, "the card names the routine"
    assert "ran too long" in body and "/routines" in body, "why it failed and how to pause"
    assert facts.next_run == f"<t:{int(datetime(2026, 11, 23, 9, tzinfo=UTC).timestamp())}:f>", (
        "the next run, shown in the reader's time"
    )


async def test_the_owner_is_told_only_when_a_failing_streak_starts() -> None:
    told: list[tuple[str | None, bool]] = []

    async def notify(row: RoutineRow, reason: str, channel_told: bool) -> None:
        told.append((row.last_error, channel_told))

    tell_owner(notify, _row(), TerminationReason.UPSTREAM, channel_told=True)
    tell_owner(notify, _row(last_error="earlier"), TerminationReason.UPSTREAM, channel_told=False)
    tell_owner(notify, _row(), None, channel_told=False)
    await drain_owner_notices(timeout_s=5)
    assert told == [(None, True)], (
        "the first failure only; a run cut short by a deploy tells nobody"
    )


async def test_the_notice_runs_outside_the_fire_and_shutdown_ends_it() -> None:
    """The fire returns at once and the notice holds none of its grant; a stalled notice
    is cancelled by the shutdown drain."""
    seen: list[object] = []
    started = asyncio.Event()

    async def notify(row: RoutineRow, reason: str, channel_told: bool) -> None:
        seen.append(current_intake_execution.get())
        started.set()
        await asyncio.Event().wait()

    token = current_intake_execution.set((uuid.uuid4(), uuid.uuid4()))
    try:
        tell_owner(notify, _row(), TerminationReason.UPSTREAM, channel_told=False)
    finally:
        current_intake_execution.reset(token)
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.wait_for(drain_owner_notices(timeout_s=0), timeout=5)
    assert seen == [None], "the notice runs without the fire's execution grant"
