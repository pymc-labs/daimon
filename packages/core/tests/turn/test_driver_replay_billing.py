"""Model calls the driver learns about only from the replay are billed.

When a stream generation ends without its terminal event, the driver replays
the session history and folds the current turn's suffix onto its state. A
`span.model_request_end` that MA emitted while no stream was attached exists
only in that replay. The driver must bill it, once, through the turn's own
recorder -- otherwise the call reaches the ledger only if the scheduler's
usage sweep later finds it, under the sweep's attribution (`turn_debit`, the
session's account stamp) instead of the turn's.

Found by `formal/metering/Metering.tla` (`LiveMetersWholeTurn`).
"""

from __future__ import annotations

import asyncio
import functools
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.turn import run_turn
from daimon.core.turn.posture import Billed
from daimon.core.usage_recording import record_turn_usage
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_model_usage
from daimon.testing.turn_fakes import (
    FakeAnthropic,
    RaiseConnection,
    RaiseReadTimeout,
    RecordingLifecycle,
    YieldEvent,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import make_end_turn, make_status_idle

_T = datetime(2026, 1, 1, tzinfo=UTC)
_MODEL = "claude-opus-4-7"


def _cast(fa: FakeAnthropic) -> AsyncAnthropic:
    return cast(AsyncAnthropic, fa)


def _now() -> datetime:
    return _T


def _mre(event_id: str) -> BetaManagedAgentsSpanModelRequestEndEvent:
    return BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        type="span.model_request_end",
        model_request_start_id=f"mrs_{event_id}",
        model_usage=ma_model_usage(input_tokens=1000, output_tokens=500),
        processed_at=_T,
        is_error=False,
    )


class _Recorder:
    def __init__(self) -> None:
        self.ids: list[str] = []

    async def __call__(self, *, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        self.ids.append(event.id)


async def _drive(fa: FakeAnthropic, recorder: object) -> None:
    await run_turn(
        anthropic=_cast(fa),
        session_id="sess_1",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        now=_now,
        billing=Billed(record=recorder),  # pyright: ignore[reportArgumentType]
    )


async def test_finalize_from_replay_bills_model_calls_the_stream_missed() -> None:
    """Stream stalls after call 1; MA runs call 2 and goes idle; the driver
    finalizes from the replay without reopening the stream."""
    first, second = _mre("sevt_mre_1"), _mre("sevt_mre_2")
    done = make_status_idle(event_id="sevt_idle", stop_reason=make_end_turn())
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(first), RaiseReadTimeout()]]
    fa.beta.sessions.events.replay_events = [first, second, done]
    fa.beta.sessions.retrieve_statuses = ["idle"]
    recorder = _Recorder()

    await _drive(fa, recorder)

    assert fa.beta.sessions.events.stream_calls == 1, "idle finalize must not reopen the stream"
    assert recorder.ids == ["sevt_mre_1", "sevt_mre_2"], (
        "a model call seen only in the finalize replay must still be billed, exactly once"
    )


async def test_reconnect_bills_model_calls_only_in_the_replay() -> None:
    """The connection drops after call 1; call 2 happens before the new
    stream attaches, so only the reconnect replay carries it."""
    first, second = _mre("sevt_mre_1"), _mre("sevt_mre_2")
    done = make_status_idle(event_id="sevt_idle", stop_reason=make_end_turn())
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(first), RaiseConnection()],
        [YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [first, second]
    recorder = _Recorder()

    await _drive(fa, recorder)

    assert recorder.ids == ["sevt_mre_1", "sevt_mre_2"], (
        "a model call seen only in the reconnect replay must be billed, exactly once"
    )


async def test_replay_billed_call_redelivered_live_is_not_billed_again() -> None:
    """If the new stream re-emits a call the replay already billed, the
    recorder is not invoked a second time."""
    first, second = _mre("sevt_mre_1"), _mre("sevt_mre_2")
    done = make_status_idle(event_id="sevt_idle", stop_reason=make_end_turn())
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(first), RaiseConnection()],
        [YieldEvent(first), YieldEvent(second), YieldEvent(done)],
    ]
    fa.beta.sessions.events.replay_events = [first, second]
    recorder = _Recorder()

    await _drive(fa, recorder)

    assert recorder.ids == ["sevt_mre_1", "sevt_mre_2"], (
        "each model call is billed once across the replay and a re-emitting stream"
    )


async def test_replayed_call_is_debited_with_the_turns_attribution(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End to end on Postgres: the replay-only call lands on the ledger under
    the turn's own reason and platform user, before any sweep runs."""
    tenant = await make_tenant(db_session)
    first, second = _mre("sevt_mre_1"), _mre("sevt_mre_2")
    done = make_status_idle(event_id="sevt_idle", stop_reason=make_end_turn())
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [[YieldEvent(first), RaiseReadTimeout()]]
    fa.beta.sessions.events.replay_events = [first, second, done]
    fa.beta.sessions.retrieve_statuses = ["idle"]
    recorder = functools.partial(
        record_turn_usage,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        platform_user_id="U_AUTHOR",
        managed_session_id="sess_1",
        model_id=_MODEL,
        markup=Decimal("1.0"),
        pricing=MODEL_PRICING[_MODEL],
        reason="checkpoint_debit",
    )

    await _drive(fa, recorder)

    rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert sorted(r.event_id for r in rows) == ["sevt_mre_1", "sevt_mre_2"], (
        "both model calls of the turn must have a usage row once the turn returns"
    )
    assert {r.platform_user_id for r in rows} == {"U_AUTHOR"}, (
        "the replay-only call must carry the turn author's platform user"
    )
    debits = [
        e
        for e in await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
        if e.idempotency_key.startswith("turn:")
    ]
    assert sorted(e.idempotency_key for e in debits) == [
        "turn:sess_1:sevt_mre_1",
        "turn:sess_1:sevt_mre_2",
    ], "each model call must be debited exactly once"
    assert {e.reason for e in debits} == {"checkpoint_debit"}, (
        "the replay-only call must keep the turn's ledger reason"
    )
