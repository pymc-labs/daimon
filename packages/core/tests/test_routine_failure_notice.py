"""A failed routine run tells its destination once per failing streak (QA #3)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import anthropic
import httpx
import pytest
from daimon.core._models import TurnOutcome
from daimon.core.errors import TurnError
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    failure_notice_text,
    poll_deliveries_once,
    record_failure,
    render_fallback_dm,
    render_fallback_post,
)
from daimon.core.scheduler import run_one_tick
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    FAILURE_NOTICE,
    create_routine,
    get_routine,
    record_result,
)
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.testing.factories import make_tenant
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_PDF_ERROR = "TurnError: upstream: The PDF specified was not valid"
_MODEL_NOTICE = (
    "The request to the AI model failed.\n\nIt will try again at its next scheduled time."
)


@pytest.fixture
def db_session_factory(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def _routine(
    session: AsyncSession,
    *,
    destination: bool = True,
    next_fire_at: datetime | None = None,
) -> RoutineRow:
    tenant = await make_tenant(session)
    row = await create_routine(
        session,
        tenant_id=tenant.id,
        created_by_user_id="u1",
        agent_id="agent_a",
        agent_name="team-020-analyst",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="summarize",
        next_fire_at=next_fire_at,
        destination_kind="channel" if destination else None,
        destination_id="111222333" if destination else None,
    )
    await session.commit()
    return row


async def _fail(
    sm: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    error: str,
    reason: TerminationReason | None = TerminationReason.UPSTREAM,
) -> RoutineRow:
    async with sm() as s, s.begin():
        await record_failure(s, row.id, error=error, reason=reason)
    return await _get(sm, row)


async def _get(sm: async_sessionmaker[AsyncSession], row: RoutineRow) -> RoutineRow:
    async with sm() as s:
        fetched = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert fetched is not None
    return fetched


class _NoCap:
    async def is_over_cap(self, tenant_id: object, user_id: str) -> bool:
        return False


async def _tick(
    sm: async_sessionmaker[AsyncSession],
    now: datetime,
    fire: Callable[[RoutineRow], Awaitable[None]],
) -> None:
    await run_one_tick(
        now=now,
        sm=sm,
        caps=_NoCap(),
        fire=fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=1,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )


async def _deliver(sm: async_sessionmaker[AsyncSession]) -> list[str]:
    posted: list[str] = []

    async def post(claimed: RoutineRow) -> DeliveryOutcome:
        posted.append(render_fallback_post(claimed))
        return DeliveryOutcome(status="delivered")

    await poll_deliveries_once(sm, platform="discord", post=post, now=datetime.now(UTC))
    return posted


@pytest.mark.parametrize(
    ("reason", "platform", "expected"),
    [
        (TerminationReason.UPSTREAM, "discord", _MODEL_NOTICE),
        (
            TerminationReason.CEILING,
            "discord",
            "It took too long, so Daimon stopped it.\n\n"
            "It will try again at its next scheduled time.",
        ),
        (
            TerminationReason.ADMISSION_BALANCE_DEPLETED,
            "slack",
            "Your team's Daimon credit has run out.\n\nAn admin can top up with `/billing`."
            "\n\nIt will fail the same way at every scheduled time until that's fixed.",
        ),
        (
            TerminationReason.ADMISSION_BALANCE_DEPLETED,
            "teams",
            "Your team's Daimon credit has run out.\n\n"
            "An admin can top up with `billing` in a 1:1 chat with me."
            "\n\nIt will fail the same way at every scheduled time until that's fixed.",
        ),
        (
            TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
            "discord",
            "This channel's budget is used up.\n\nAn admin can raise it."
            "\n\nIt won't run until the budget has room again.",
        ),
        (
            TerminationReason.ADMISSION_CAP_EXCEEDED,
            "discord",
            "The person who made it has used their monthly limit.\n\n"
            "It will run again at its next scheduled time once the limit resets or is raised.",
        ),
        (
            TerminationReason.REFUSED,
            "discord",
            "The AI model declined this request.\n\n"
            "Rewording the routine's prompt may help. It will try again at its next scheduled time.",
        ),
        (
            TerminationReason.UNKNOWN,
            "discord",
            "Something went wrong while it was running.\n\n"
            "It will try again at its next scheduled time.",
        ),
        (
            TerminationReason.MISSING_CONFIG,
            "discord",
            "Daimon isn't set up in this channel yet.\n\nAsk an admin to run `/agent-setup`."
            "\n\nIt will fail the same way at every scheduled time until that's fixed.",
        ),
        (
            TerminationReason.ADMISSION_CHANNEL_ISOLATED,
            "slack",
            "This channel only uses its own agents.\n\nAsk an admin to pick one."
            "\n\nIt will fail the same way at every scheduled time until that's fixed.",
        ),
    ],
)
def test_notice_says_what_failed_in_plain_words(
    reason: TerminationReason, platform: str, expected: str
) -> None:
    assert failure_notice_text(reason, platform=platform) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            AdmissionDenied(
                reason="backend_unsupported", cause="provider_not_allowed", provider="gemini"
            ),
            "Gemini isn't turned on for this server.",
        ),
        (
            AdmissionDenied(reason="backend_unsupported"),
            "This agent's model provider can't run here.\n\nAsk an admin to check it.",
        ),
    ],
)
def test_a_refusal_no_later_run_gets_past_names_its_cause_and_says_so(
    error: AdmissionDenied, expected: str
) -> None:
    reason = termination_reason(error)
    text = failure_notice_text(reason, platform="discord", error=error)
    assert text.startswith(expected), "the turn refusal's own words"
    assert "next scheduled time" not in text, "it does not promise a retry that fails again"
    assert text.endswith("until that's fixed.")


def test_a_provider_hold_that_frees_up_by_itself_says_it_will_try_again() -> None:
    """Holds settle as attempts finish and age out, so a later run can pass."""
    error = AdmissionDenied(reason="provider_hold_cap_reached")
    assert failure_notice_text(termination_reason(error), platform="discord", error=error) == (
        "This model's prepaid spending hold is used up.\n\nAsk an admin to release or "
        "raise it.\n\nIt will try again at its next scheduled time."
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            AdmissionDenied(reason="provider_hold_cap_reached"),
            "This model's prepaid spending hold is used up.",
        ),
        (TurnError(kind="upstream", message="The PDF specified was not valid"), _MODEL_NOTICE),
        (
            anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
            "Something went wrong while it was running.",
        ),
        (KeyError("x"), "Something went wrong while it was running."),
    ],
)
async def test_a_fire_that_raises_is_classified_where_it_is_caught(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    error: Exception,
    expected: str,
) -> None:
    now = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    row = await _routine(db_session, next_fire_at=now - timedelta(minutes=1))

    async def failing_fire(_row: RoutineRow) -> None:
        raise error

    await _tick(db_session_factory, now, failing_fire)
    await drain_outcomes()

    after = await _get(db_session_factory, row)
    assert after.delivery_note == FAILURE_NOTICE
    assert after.delivery_payload is not None and after.delivery_payload.startswith(expected)
    assert "PDF" not in after.delivery_payload, "no raw error text"
    async with db_session_factory() as s:
        outcomes = (await s.execute(select(TurnOutcome.reason))).scalars().all()
    reason = termination_reason(error)
    assert outcomes == [reason.value], "the outcome and the notice share one reason"


async def test_failure_notice_reads_as_a_failure_not_a_result(db_session: AsyncSession) -> None:
    saved = await _routine(db_session)
    row = saved.model_copy(
        update={"delivery_note": FAILURE_NOTICE, "delivery_payload": _MODEL_NOTICE}
    )
    assert render_fallback_post(row) == (
        "team-020-analyst's daily routine at 09:00 UTC failed, so there's no result this time."
        f"\n\n{_MODEL_NOTICE}"
    )
    assert render_fallback_post(row, as_agent=True).startswith(
        "The daily routine at 09:00 UTC failed, so there's no result this time."
    )
    dm = render_fallback_dm(row, "destination_unavailable")
    assert "Here's the result" not in dm
    assert dm.endswith(f"Your routine failed, so there's no result this time.\n\n{_MODEL_NOTICE}")


async def test_a_failed_run_posts_one_notice_per_failing_streak(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    row = await _routine(db_session)

    failed = await _fail(sm, row, _PDF_ERROR)
    assert (failed.delivery_status, failed.delivery_note) == ("pending", FAILURE_NOTICE)
    assert failed.delivery_payload == _MODEL_NOTICE
    assert failed.last_error == _PDF_ERROR, "the panel still sees the raw error"

    posted = await _deliver(sm)
    assert posted == [
        "team-020-analyst's daily routine at 09:00 UTC failed, so there's no result this time."
        f"\n\n{_MODEL_NOTICE}"
    ]

    again = await _fail(sm, row, _PDF_ERROR)
    assert again.delivery_status == "delivered", "a repeat failure queues nothing"
    assert await _deliver(sm) == []

    async with sm() as s, s.begin():
        await record_result(s, row.id, tail="All green.", error=None, delivery="pending")
    assert len(await _deliver(sm)) == 1, "the success posts its result"

    after_recovery = await _fail(sm, row, "timeout: exceeded 3000.0s", TerminationReason.CEILING)
    assert (after_recovery.delivery_status, after_recovery.delivery_note) == (
        "pending",
        FAILURE_NOTICE,
    ), "failing again after a success is a new streak"


async def test_a_pending_result_is_not_replaced_by_a_notice(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    row = await _routine(db_session)
    async with sm() as s, s.begin():
        await record_result(s, row.id, tail="All green.", error=None, delivery="pending")

    failed = await _fail(sm, row, _PDF_ERROR)

    assert (failed.delivery_status, failed.delivery_payload) == ("pending", "All green.")
    assert failed.delivery_note is None


async def test_no_notice_without_a_destination_or_for_a_quiet_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sm = db_session_factory
    headless = await _routine(db_session, destination=False)
    assert (await _fail(sm, headless, _PDF_ERROR)).delivery_status is None

    deployed = await _routine(db_session)
    assert (await _fail(sm, deployed, "scheduler_shutdown", None)).delivery_status is None


async def test_a_fire_that_raises_queues_the_notice_through_the_tick(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    row = await _routine(db_session, next_fire_at=now - timedelta(minutes=1))

    async def failing_fire(_row: RoutineRow) -> None:
        raise TurnError(kind="upstream", message="The PDF specified was not valid")

    await _tick(db_session_factory, now, failing_fire)

    after = await _get(db_session_factory, row)
    assert after.last_error == _PDF_ERROR
    assert (after.delivery_status, after.delivery_payload) == ("pending", _MODEL_NOTICE)


# The previous release's outbox writes: its `queue_failure_notice` and its
# `record_result(delivery="pending")`. Both write the note this release reads.
_OLD_FAILURE = (
    "UPDATE routines SET delivery_status = 'pending', delivery_note = 'failure_notice', "
    "delivery_payload = :payload WHERE id = :id"
)
_OLD_RESULT = (
    "UPDATE routines SET delivery_status = 'pending', delivery_note = NULL, "
    "delivery_payload = 'All green.' WHERE id = :id"
)


@pytest.mark.parametrize(
    ("first", "then", "expected"),
    [
        (_OLD_FAILURE, "result", "Result from team-020-analyst's daily routine"),
        (_OLD_FAILURE, _OLD_RESULT, "Result from team-020-analyst's daily routine"),
        ("result", _OLD_FAILURE, "team-020-analyst's daily routine at 09:00 UTC failed"),
    ],
)
async def test_either_release_writing_last_decides_what_the_post_says(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    first: str,
    then: str,
    expected: str,
) -> None:
    sm = db_session_factory
    row = await _routine(db_session)
    for step in (first, then):
        async with sm() as s, s.begin():
            if step == "result":
                await record_result(s, row.id, tail="All green.", error=None, delivery="pending")
            else:
                await s.execute(text(step), {"id": row.id, "payload": _MODEL_NOTICE})

    assert (await _deliver(sm))[0].startswith(expected)
