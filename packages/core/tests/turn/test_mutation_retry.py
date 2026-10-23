"""Bounded retries of claimed provider mutations, on the real Postgres store.

Every test drives the real AsyncAnthropic client through `ScriptedTransport`,
so each HTTP attempt is counted at the wire and an unscripted extra request
fails the test. A duplicate provider mutation is worse than a failed turn:
each test asserts both the HTTP attempts and the durable operation records.

The classification evidence (which statuses prove "not executed") lives in
`mux.drivers.anthropic.transport.classify_mutation_failure`; the policy rules
are unit-tested in `packages/mux/tests/test_mutation_retry.py`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import anthropic
import httpx
import pytest
import pytest_asyncio
from anthropic.types.beta.sessions import BetaManagedAgentsEventParams
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.driver import run_turn
from daimon.core.turn.io import (
    TrackedLegacyTurnIO,
    TurnConnectionLost,
    default_mux_turn_io,
    neutral_inputs,
)
from daimon.core.turn.persistence import MutationRetry, TurnPersistence, UncertainSend
from daimon.core.turn.posture import BillingExempt
from daimon.testing.factories import make_tenant
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import ProviderError
from mux.state.lease import Lease, Slot, StaleFence
from mux.state.mutation_retry import MutationFailure, MutationRetryPolicy
from mux.state.operations import Begun, OperationRecord
from mux.state.store import binding_slot
from mux.testing.drivers.anthropic.ma import send_events_response
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import make_agent_message, make_status_idle

NOW = datetime(2026, 10, 23, tzinfo=UTC)
TENANT = uuid.UUID(int=4105)
SCOPE = Scope(
    tenant_id=str(TENANT), account_id="caller", principal_id="daimon", authorization_id="admitted"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="anthropic",
    account_scope_id="provider",
    tenant_id=SCOPE.tenant_id,
    account_id=SCOPE.account_id,
)
BINDING = ProviderBinding(
    id="binding",
    thread=ThreadRef(
        channel=ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="channel"),
        thread_id="thread",
    ),
    provider="anthropic",
    profile="anthropic.managed_agents",
    native_refs={"session": "session"},
    generation=1,
    config_revision=0,
    legacy_account_id=SCOPE.account_id,
)
EVENTS_PATH = "/v1/sessions/session/events"
INPUTS: tuple[BetaManagedAgentsEventParams, ...] = (
    {"type": "user.message", "content": [{"type": "text", "text": "question"}]},
)
FAST = MutationRetryPolicy(max_attempts=3, base_delay_s=0.01, max_delay_s=0.02)


@dataclass
class TimedTransport(ScriptedTransport):
    """Also records when each HTTP attempt reached the wire."""

    sent_at: list[float] = field(default_factory=list[float])

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        self.sent_at.append(time.monotonic())
        return super().dispatch(request)


def rate_limited(
    headers: dict[str, str] | None = None, *, error_code: str | None = None
) -> ScriptedReply:
    error: dict[str, object] = {"type": "rate_limit_error", "message": "slow down"}
    if error_code is not None:
        error["details"] = {"error_code": error_code}
    body = {"type": "error", "error": error}
    return ScriptedReply("POST", EVENTS_PATH, httpx.Response(429, headers=headers, json=body))


def refused(status: int, kind: str) -> ScriptedReply:
    body = {"type": "error", "error": {"type": kind, "message": "refused"}}
    return ScriptedReply("POST", EVENTS_PATH, httpx.Response(status, json=body))


def accepted() -> ScriptedReply:
    return ScriptedReply("POST", EVENTS_PATH, send_events_response())


def notes(error: BaseException) -> str:
    """Every note on the error and its causes: where attempt counts surface."""
    found: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        found += getattr(current, "__notes__", [])
        current = current.__cause__ or current.__context__
    return "\n".join(found)


@pytest_asyncio.fixture
async def stores(db_engine: AsyncEngine, db_clean: None) -> Callable[[], PostgresStateStore]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as db, db.begin():
        await make_tenant(db, id=TENANT, workspace_id="p8-retry")
    store = PostgresStateStore(sessions, trust_caller_clock=True)
    await store.put_binding(BINDING, expected_generation=0)
    return lambda: PostgresStateStore(sessions, trust_caller_clock=True)


def context(
    store: PostgresStateStore,
    *,
    retry: MutationRetry | None = None,
    now: Callable[[], datetime] = lambda: NOW,
    send_timeout_s: float = 120.0,
) -> TurnPersistence:
    return TurnPersistence(
        store,
        BINDING,
        SCOPE,
        operation_key="root",
        now=now,
        retry=retry or MutationRetry(policy=FAST),
        send_timeout_s=send_timeout_s,
    )


async def send(persistence: TurnPersistence, client: anthropic.AsyncAnthropic) -> None:
    io = default_mux_turn_io(client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence)
    await persistence.run(lambda: io.send(INPUTS))


async def statuses(
    store: PostgresStateStore, attempts: int = 6, *, kind: str = "send"
) -> list[str | None]:
    """Attempt `n` is the durable operation `root:<kind>:<n>`; None if never begun.

    `pending` means the intent was recorded but never claimed: nothing was sent.
    """
    found: list[str | None] = []
    for attempt in range(attempts):
        record = await store.get_operation(SCOPE, f"root:{kind}:{attempt}")
        found.append(record.operation.status if record is not None else None)
    return found


async def test_rate_limit_then_success_completes_the_turn_with_one_accepted_send(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    raw = [
        make_agent_message(event_id="message", text="answer").model_dump(mode="json"),
        make_status_idle(event_id="ended").model_dump(mode="json"),
    ]
    transport = TimedTransport()
    transport.queue(
        ScriptedReply.stream("/v1/sessions/session/events/stream", raw),
        rate_limited({"retry-after": "0.2"}),
        accepted(),
    )
    persistence = context(store)
    lifecycle = RecordingLifecycle()
    # Production's client retries 8 times; a claimed mutation must not.
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        with persistence.activate():
            state = await run_turn(
                anthropic=client,
                session_id="session",
                user_message="question",
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path="mux",
                scope=SCOPE,
                now=lambda: NOW,
                render_interval_s=3600,
            )
    transport.assert_consumed()
    assert state.error is None and state.stop_reason is not None
    assert [getattr(block, "text", None) for block in state.content] == ["answer"]
    posts = [request for request in transport.requests if request.method == "POST"]
    assert len(posts) == 2 and posts[0] == posts[1]
    # Never earlier than Retry-After.
    assert transport.sent_at[-1] - transport.sent_at[-2] >= 0.2
    # The mux persistence ran (a legacy turn records no operation at all).
    assert await statuses(store) == ["failed", "accepted", None, None, None, None]
    rejected = await store.get_operation(SCOPE, "root:send:0")
    assert rejected is not None
    assert rejected.result["failure"] == {
        "delivery": "rejected",
        "retryable": True,
        "reason": "http_429",
        "retry_after_s": 0.2,
    }


async def test_exhausted_attempts_stop_with_a_clear_error_and_a_restart_cannot_reset_them(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    transport = ScriptedTransport()
    transport.queue(*(rate_limited({"retry-after-ms": "10"}) for _ in range(3)))
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        with pytest.raises(anthropic.RateLimitError) as caught:
            await send(context(store), client)
    transport.assert_consumed()
    # Three durable attempts, three HTTP requests: no hidden SDK retries.
    assert len(transport.requests) == 3
    assert "attempt 3 of 3 rejected (http_429); not retried: attempts_exhausted" in notes(
        caught.value
    )
    assert await statuses(store) == ["failed", "failed", "failed", "pending", None, None]

    restarted = ScriptedTransport()
    async with restarted.client(max_retries=MA_MAX_RETRIES) as client:
        with pytest.raises(ProviderError) as exhausted:
            await send(context(stores()), client)
    assert exhausted.value.native_code == "mutation_attempts_exhausted"
    assert "nothing was sent" in notes(exhausted.value)
    assert restarted.requests == [] and not restarted.violations


async def test_a_restart_during_backoff_continues_the_same_allowance(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    first = ScriptedTransport()
    first.queue(rate_limited({"retry-after": "30"}))
    clock = [NOW]
    persistence = context(store, now=lambda: clock[0])
    async with first.client() as client:
        task = asyncio.create_task(send(persistence, client))
        # Wait until the refusal is recorded: the turn is now in its backoff.
        while (await statuses(store))[:1] != ["failed"]:
            await asyncio.sleep(0.01)
        task.cancel()  # The process dies while waiting out Retry-After.
        with pytest.raises(asyncio.CancelledError):
            await task
    first.assert_consumed()
    assert await statuses(store) == ["failed", "pending", None, None, None, None]

    # The restarted process comes back after the recorded Retry-After.
    clock[0] += timedelta(seconds=31)
    second = ScriptedTransport()
    second.queue(*(rate_limited({"retry-after-ms": "10"}) for _ in range(2)))
    async with second.client() as client:
        with pytest.raises(anthropic.RateLimitError) as caught:
            await send(context(stores(), now=lambda: clock[0]), client)
    second.assert_consumed()
    assert len(first.requests) + len(second.requests) == FAST.max_attempts
    assert "attempts_exhausted" in notes(caught.value)
    assert await statuses(store) == ["failed", "failed", "failed", "pending", None, None]


@pytest.mark.parametrize(
    ("headers", "retry", "stop"),
    [
        # Retry-After (seconds) longer than the turn's remaining deadline.
        ({"retry-after": "5"}, MutationRetry(policy=FAST, deadline=1.0), "budget_exhausted"),
        # Retry-After as an HTTP-date, 30 s after the server's own clock.
        (
            {
                "date": "Fri, 23 Oct 2026 12:00:00 GMT",
                "retry-after": "Fri, 23 Oct 2026 12:00:30 GMT",
            },
            MutationRetry(policy=FAST, deadline=5.0),
            "budget_exhausted",
        ),
        # The policy's own elapsed budget.
        (
            {"retry-after-ms": "500"},
            MutationRetry(policy=MutationRetryPolicy(max_attempts=3, max_elapsed_s=0.2)),
            "budget_exhausted",
        ),
        # A wait no policy would accept, even with the deadline far away.
        ({"retry-after": "120"}, MutationRetry(policy=FAST), "retry_after_too_long"),
    ],
)
async def test_a_wait_past_the_budget_fails_now_without_an_early_retry(
    stores: Callable[[], PostgresStateStore],
    headers: dict[str, str],
    retry: MutationRetry,
    stop: str,
) -> None:
    store = stores()
    if retry.deadline is not None:
        retry = MutationRetry(policy=retry.policy, deadline=time.monotonic() + retry.deadline)
    transport = ScriptedTransport()
    transport.queue(rate_limited(headers))
    started = time.monotonic()
    async with transport.client() as client:
        with pytest.raises(anthropic.RateLimitError) as caught:
            await send(context(store, retry=retry), client)
    assert time.monotonic() - started < 1.0  # It failed at once instead of waiting.
    transport.assert_consumed()
    assert len(transport.requests) == 1
    assert f"not retried: {stop}" in notes(caught.value)
    assert await statuses(store) == ["failed", "pending", None, None, None, None]


async def test_the_lease_wait_deadline_bounds_retries_by_default(
    stores: Callable[[], PostgresStateStore],
) -> None:
    from daimon.core.turn.persistence import LeaseWait

    store = stores()
    persistence = TurnPersistence(
        store,
        BINDING,
        SCOPE,
        operation_key="root",
        now=lambda: NOW,
        lease_wait=LeaseWait(timeout_s=1.0),
    )
    transport = ScriptedTransport()
    transport.queue(rate_limited({"retry-after": "5"}))
    async with transport.client() as client:
        with pytest.raises(anthropic.RateLimitError) as caught:
            await send(persistence, client)
    transport.assert_consumed()
    assert len(transport.requests) == 1
    assert "not retried: budget_exhausted" in notes(caught.value)


async def test_cancellation_during_the_backoff_wait_stops_without_another_send(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    cancel = asyncio.Event()
    transport = ScriptedTransport()
    transport.queue(rate_limited({"retry-after": "10"}))
    persistence = context(store, retry=MutationRetry(policy=FAST, cancel=cancel))

    async def cancel_after_the_first_attempt() -> None:
        # Once the refusal is recorded the sender is in its 10 s backoff.
        while (await statuses(store))[:1] != ["failed"]:
            await asyncio.sleep(0.01)
        cancel.set()

    started = time.monotonic()
    async with transport.client() as client:
        canceller = asyncio.create_task(cancel_after_the_first_attempt())
        with pytest.raises(anthropic.RateLimitError) as caught:
            await send(persistence, client)
        await canceller
    assert time.monotonic() - started < 2.0
    transport.assert_consumed()
    assert len(transport.requests) == 1
    assert "retry abandoned: cancelled" in notes(caught.value)
    assert await statuses(store) == ["failed", "pending", None, None, None, None]


async def test_lease_lost_before_the_retry_sends_nothing(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    transport = ScriptedTransport()
    transport.queue(rate_limited({"retry-after-ms": "300"}))
    persistence = context(store)

    async def steal_the_lease() -> None:
        # Once the refusal is recorded the sender is inside its 300 ms wait.
        while (await statuses(store))[:1] != ["failed"]:
            await asyncio.sleep(0.01)
        # A successor whose clock says the sender's lease expired takes it over.
        await stores().acquire_lease(
            binding_slot(BINDING),
            holder="successor",
            turn_id="other",
            now=NOW + timedelta(minutes=10),
            ttl=timedelta(minutes=5),
        )

    async with transport.client() as client:
        thief = asyncio.create_task(steal_the_lease())
        with pytest.raises(StaleFence):
            await send(persistence, client)
        await thief
    transport.assert_consumed()
    assert len(transport.requests) == 1
    # The retry's intent exists but was never claimed: pending means never sent.
    assert await statuses(store) == ["failed", "pending", None, None, None, None]


async def test_concurrent_replay_during_backoff_never_sends_before_retry_after(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    transport = TimedTransport()
    transport.queue(rate_limited({"retry-after": "0.4"}), accepted())
    persistence = context(store, now=lambda: datetime.now(UTC))
    async with transport.client() as client:
        io = default_mux_turn_io(
            client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence
        )

        async def pump() -> tuple[BaseException | None, BaseException | None]:
            waiting = asyncio.create_task(io.send(INPUTS))
            # Once the refusal is recorded the first sender is in its backoff.
            # (Before that, a replay is refused: attempt 0 may still be in flight.)
            while (await statuses(store))[:1] != ["failed"]:
                await asyncio.sleep(0.01)
            # A replay of the same operation reads the same recorded Retry-After.
            replay = asyncio.create_task(io.send(INPUTS))
            return await asyncio.gather(waiting, replay, return_exceptions=True)

        outcomes = await persistence.run(pump)
    transport.assert_consumed()
    assert len(transport.requests) == 2
    assert transport.sent_at[1] - transport.sent_at[0] >= 0.4
    # One sender sent the retry; the other restored its receipt or saw it in flight.
    assert None in outcomes
    assert all(outcome is None or isinstance(outcome, TurnConnectionLost) for outcome in outcomes)
    assert await statuses(store) == ["failed", "accepted", None, None, None, None]


@pytest.mark.parametrize(
    ("reply", "raised"),
    [
        (httpx.ReadError("accepted; acknowledgement lost"), TurnConnectionLost),
        (httpx.WriteError("body partly written"), TurnConnectionLost),
        (httpx.Response(500, json={"type": "error", "error": {"type": "api_error"}}), None),
        (httpx.Response(504, json={"type": "error", "error": {"type": "timeout_error"}}), None),
        (
            httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error"}}),
            None,
        ),
        (httpx.Response(408, json={"type": "error", "error": {"type": "timeout_error"}}), None),
        # The server's own retry hint is not proof that nothing ran.
        (
            httpx.Response(
                503,
                headers={"x-should-retry": "true", "retry-after": "0"},
                json={"type": "error", "error": {"type": "api_error"}},
            ),
            None,
        ),
    ],
)
async def test_an_ambiguous_failure_is_outcome_unknown_and_never_resent(
    stores: Callable[[], PostgresStateStore],
    reply: httpx.Response | Exception,
    raised: type[Exception] | None,
) -> None:
    store = stores()
    transport = ScriptedTransport()
    transport.queue(ScriptedReply("POST", EVENTS_PATH, reply))
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        with pytest.raises(raised or anthropic.APIStatusError):
            await send(context(store), client)
        transport.assert_consumed()
        assert len(transport.requests) == 1
        assert await statuses(store) == ["outcome_unknown", None, None, None, None, None]
        # A restart replays; it never sends the batch again.
        with pytest.raises(TurnConnectionLost, match="already claimed"):
            await send(context(stores()), client)
    assert len(transport.requests) == 1 and not transport.violations


async def test_a_send_timeout_after_possible_acceptance_is_never_resent(
    stores: Callable[[], PostgresStateStore],
) -> None:
    from mux.contracts.receipts import SendReceipt

    store = stores()
    sends: list[str] = []

    async def hang(key: str) -> SendReceipt:
        sends.append(key)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def deliver(persistence: TurnPersistence) -> SendReceipt:
        return await persistence.mutate(
            SESSION, "send", {"text": "q"}, hang, SendReceipt, lambda receipt: "accepted"
        )

    first = context(store, send_timeout_s=0.05)
    with pytest.raises(UncertainSend, match="timed out"):
        await first.run(lambda: deliver(first))
    restarted = context(stores())
    with pytest.raises(UncertainSend, match="already claimed"):
        await restarted.run(lambda: deliver(restarted))
    assert sends == ["root:send:0"]
    assert await statuses(store) == ["outcome_unknown", None, None, None, None, None]


async def test_a_crash_after_the_claim_is_never_resent(
    stores: Callable[[], PostgresStateStore], db_engine: AsyncEngine
) -> None:
    class Crash(BaseException):
        """The process died between the provider's reply and the receipt commit."""

    class CrashingStore(PostgresStateStore):
        async def advance_operation(self, *args: object, **kwargs: object) -> OperationRecord:
            raise Crash

    store = stores()
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    crashing = CrashingStore(sessions, trust_caller_clock=True)
    transport = ScriptedTransport()
    transport.queue(accepted())
    async with transport.client() as client:
        with pytest.raises(Crash):
            await send(context(crashing), client)
        assert await statuses(store) == ["sent", None, None, None, None, None]
        with pytest.raises(TurnConnectionLost, match="already claimed"):
            await send(context(stores()), client)
    transport.assert_consumed()
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "reply",
    [
        refused(400, "invalid_request_error"),
        refused(401, "authentication_error"),
        refused(403, "permission_error"),
        refused(404, "not_found_error"),
        refused(413, "request_too_large"),
        # A 429 without Retry-After is the tier spend cap: it keeps failing.
        rate_limited(),
        rate_limited({"retry-after": "1"}, error_code="enforced_spend_limit_reached"),
        rate_limited({"retry-after": "1", "x-should-retry": "false"}),
    ],
)
async def test_a_definite_refusal_that_cannot_succeed_is_not_retried(
    stores: Callable[[], PostgresStateStore], reply: ScriptedReply
) -> None:
    store = stores()
    transport = ScriptedTransport()
    transport.queue(reply)
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        with pytest.raises(anthropic.APIStatusError) as caught:
            await send(context(store), client)
    transport.assert_consumed()
    assert len(transport.requests) == 1
    assert "not retried: not_retryable" in notes(caught.value)
    # Refused, so a later invocation may still take a fresh attempt.
    assert await statuses(store) == ["failed", None, None, None, None, None]


async def test_a_connection_refused_before_the_request_was_written_is_retried(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("POST", EVENTS_PATH, httpx.ConnectError("connection refused")),
        accepted(),
    )
    persistence = context(store)
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        # Main's native recovery path raises the SDK's own errors unnormalized.
        io = TrackedLegacyTurnIO(client, SESSION.id, scope=SCOPE, persistence=persistence)
        await persistence.run(lambda: io.send(INPUTS))
    transport.assert_consumed()
    assert len(transport.requests) == 2
    assert await statuses(store) == ["failed", "accepted", None, None, None, None]


async def test_a_claimed_mutation_runs_with_sdk_retries_off(
    stores: Callable[[], PostgresStateStore],
) -> None:
    from mux.contracts.receipts import SendReceipt
    from mux.drivers.anthropic.transport import mutation_client

    store = stores()
    transport = ScriptedTransport()
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        observed: list[int] = []

        async def deliver(key: str) -> SendReceipt:
            observed.append(mutation_client(client).max_retries)
            return SendReceipt(operation_id=key, status="queued", input_ids=())

        persistence = context(store)
        await persistence.run(
            lambda: persistence.mutate(
                SESSION, "send", {"text": "q"}, deliver, SendReceipt, lambda receipt: "accepted"
            )
        )
        assert observed == [0]
        # Reads and unclaimed work keep the client's own retry policy.
        assert mutation_client(client).max_retries == client.max_retries == MA_MAX_RETRIES


DECISIONS: tuple[BetaManagedAgentsEventParams, ...] = (
    {"type": "user.tool_confirmation", "tool_use_id": "action", "result": "allow"},
)


class CancellingStore(PostgresStateStore):
    """Fires the turn's cancel signal inside one store call of the retry."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        cancel: asyncio.Event,
        during: str,
    ) -> None:
        super().__init__(sessions, trust_caller_clock=True)
        self._cancel, self._during, self._renewals = cancel, during, 0

    async def begin_operation(
        self,
        scope: Scope,
        *,
        key: str,
        request_digest: str,
        operation_id: str,
        now: datetime,
        slot: Slot | None = None,
    ) -> Begun:
        begun = await super().begin_operation(
            scope,
            key=key,
            request_digest=request_digest,
            operation_id=operation_id,
            now=now,
            slot=slot,
        )
        if self._during == "begin_operation" and key.endswith(":1"):
            self._cancel.set()
        return begun

    async def claim_send(
        self, scope: Scope, key: str, *, now: datetime, fence: Lease | None
    ) -> OperationRecord:
        claimed = await super().claim_send(scope, key, now=now, fence=fence)
        if self._during == "claim_send" and key.endswith(":1"):
            self._cancel.set()
        return claimed

    async def renew_lease(self, lease: Lease, *, now: datetime, ttl: timedelta) -> Lease:
        renewed = await super().renew_lease(lease, now=now, ttl=ttl)
        self._renewals += 1
        # The first renewal precedes attempt 0; the second is the retry's.
        if self._during == "renew_lease" and self._renewals == 2:
            self._cancel.set()
        return renewed


@pytest.mark.parametrize(
    ("during", "retry_status"),
    [
        ("begin_operation", "pending"),
        ("claim_send", "failed"),
        # The last await before the provider call.
        ("renew_lease", "failed"),
    ],
)
async def test_a_cancel_after_the_backoff_but_before_the_send_never_sends_a_tool_decision(
    stores: Callable[[], PostgresStateStore],
    db_engine: AsyncEngine,
    during: str,
    retry_status: str,
) -> None:
    from daimon.core.turn.io import _send_kind  # pyright: ignore[reportPrivateUsage]

    store = stores()
    cancel = asyncio.Event()
    cancelling = CancellingStore(
        async_sessionmaker(db_engine, expire_on_commit=False), cancel=cancel, during=during
    )
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("POST", EVENTS_PATH, rate_limited({"retry-after-ms": "10"}).response)
    )
    persistence = context(cancelling, retry=MutationRetry(policy=FAST, cancel=cancel))
    async with transport.client() as client:
        io = default_mux_turn_io(
            client, SCOPE, SESSION.id, read_timeout_s=120, persistence=persistence
        )
        with pytest.raises(anthropic.RateLimitError) as caught:
            await persistence.run(lambda: io.send(DECISIONS))
    transport.assert_consumed()
    # The approval reached the provider once, as the refused attempt 0, never again.
    assert [request.method for request in transport.requests] == ["POST"]
    assert "retry abandoned: cancelled" in notes(caught.value)
    kind = _send_kind(neutral_inputs(DECISIONS))
    assert await statuses(store, kind=kind) == ["failed", retry_status, None, None, None, None]
    if retry_status == "failed":
        retry = await store.get_operation(SCOPE, f"root:{kind}:1")
        assert retry is not None
        assert retry.result["failure"] == {
            "delivery": "rejected",
            "retryable": False,
            "reason": "cancelled_before_send",
            "retry_after_s": None,
        }


async def test_a_stop_request_is_still_retried_after_the_turn_is_cancelled(
    stores: Callable[[], PostgresStateStore],
) -> None:
    from mux.contracts.receipts import SendReceipt
    from mux.drivers.anthropic.transport import mutation_client

    store = stores()
    cancel = asyncio.Event()
    cancel.set()  # A stop request is sent because the turn was cancelled.
    transport = ScriptedTransport()
    transport.queue(rate_limited({"retry-after-ms": "10"}), accepted())
    persistence = context(store, retry=MutationRetry(policy=FAST, cancel=cancel))
    async with transport.client() as client:

        async def interrupt(key: str) -> SendReceipt:
            await mutation_client(client).beta.sessions.events.send(
                SESSION.id, events=[{"type": "user.interrupt"}]
            )
            return SendReceipt(operation_id=key, status="queued", input_ids=())

        await persistence.run(
            lambda: persistence.mutate(
                SESSION,
                "cancel",
                {"turn_id": "turn"},
                interrupt,
                SendReceipt,
                lambda receipt: "accepted",
            )
        )
    transport.assert_consumed()
    assert len(transport.requests) == 2
    assert await statuses(store, kind="cancel") == ["failed", "accepted", None, None, None, None]


@pytest.mark.parametrize(
    "redirected",
    [
        rate_limited({"retry-after-ms": "10"}).response,
        httpx.ConnectError("redirect target unreachable"),
    ],
)
async def test_a_redirected_mutation_is_outcome_unknown_and_never_resent(
    stores: Callable[[], PostgresStateStore],
    redirected: httpx.Response | Exception,
) -> None:
    store = stores()
    elsewhere = "/v1/sessions/session/elsewhere"
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST",
            EVENTS_PATH,
            httpx.Response(303, headers={"location": f"https://anthropic.invalid{elsewhere}"}),
        ),
        # Reached only if the claimed POST followed the redirect.
        ScriptedReply("GET", elsewhere, redirected),
    )
    # Like the SDK's default HTTP client (`follow_redirects=True`) in production.
    following = httpx.AsyncClient(
        transport=httpx.MockTransport(transport.dispatch), follow_redirects=True, trust_env=False
    )
    async with transport.client(max_retries=MA_MAX_RETRIES).with_options(
        http_client=following
    ) as client:
        with pytest.raises(anthropic.APIStatusError) as caught:
            await send(context(store), client)
        assert caught.value.status_code == 303
        assert await statuses(store) == ["outcome_unknown", None, None, None, None, None]
        with pytest.raises(TurnConnectionLost, match="already claimed"):
            await send(context(stores()), client)
    assert [request.method for request in transport.requests] == ["POST"]
    assert not transport.violations


async def test_a_later_invocation_never_sends_before_the_recorded_retry_after(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    clock = [NOW]
    first = ScriptedTransport()
    first.queue(rate_limited({"retry-after": "30"}))
    # This invocation's own deadline cannot hold the 30 s wait.
    soon = MutationRetry(policy=FAST, deadline=time.monotonic() + 1.0)
    async with first.client() as client:
        with pytest.raises(anthropic.RateLimitError):
            await send(context(store, retry=soon, now=lambda: clock[0]), client)
    first.assert_consumed()

    # 10 s later a fresh invocation would have to wait 20 s more: it may not.
    clock[0] = NOW + timedelta(seconds=10)
    early = ScriptedTransport()
    later = MutationRetry(policy=FAST, deadline=time.monotonic() + 5.0)
    async with early.client() as client:
        with pytest.raises(ProviderError) as refused:
            await send(context(stores(), retry=later, now=lambda: clock[0]), client)
    assert refused.value.native_code == "mutation_budget_exhausted"
    assert refused.value.category == "rate_limited"
    assert "nothing was sent" in notes(refused.value)
    assert early.requests == [] and not early.violations

    # 0.2 s before Retry-After ends, it waits out the rest and only then sends.
    clock[0] = NOW + timedelta(seconds=29.8)
    last = TimedTransport()
    last.queue(accepted())
    started = time.monotonic()
    async with last.client() as client:
        await send(
            context(
                stores(),
                retry=MutationRetry(policy=FAST, deadline=time.monotonic() + 5.0),
                now=lambda: clock[0],
            ),
            client,
        )
    last.assert_consumed()
    assert last.sent_at[0] - started >= 0.2
    assert await statuses(store) == ["failed", "accepted", None, None, None, None]


async def test_a_later_invocation_after_the_elapsed_budget_sends_nothing(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    clock = [NOW]
    first = ScriptedTransport()
    first.queue(rate_limited({"retry-after-ms": "10"}))
    spent = MutationRetry(policy=FAST, deadline=time.monotonic())
    async with first.client() as client:
        with pytest.raises(anthropic.RateLimitError):
            await send(context(store, retry=spent, now=lambda: clock[0]), client)
    first.assert_consumed()

    clock[0] = NOW + timedelta(seconds=FAST.max_elapsed_s + 1)
    later = ScriptedTransport()
    async with later.client() as client:
        with pytest.raises(ProviderError) as refused:
            await send(context(stores(), now=lambda: clock[0]), client)
    assert refused.value.native_code == "mutation_budget_exhausted"
    assert later.requests == [] and not later.violations
    assert await statuses(store) == ["failed", "pending", None, None, None, None]


async def test_a_refusal_recorded_without_a_classification_is_never_followed(
    stores: Callable[[], PostgresStateStore],
) -> None:
    from mux.state.operations import request_digest

    store = stores()
    # What the code before classification wrote for any 4xx, 408 included.
    digest = request_digest(
        {
            "session": SESSION.id,
            "kind": "send",
            "request": [event.model_dump(mode="json") for event in neutral_inputs(INPUTS)],
        }
    )
    slot = binding_slot(BINDING)
    lease = await store.acquire_lease(
        slot, holder="old", turn_id="old", now=NOW, ttl=timedelta(minutes=5)
    )
    await store.begin_operation(
        SCOPE,
        key="root:send:0",
        request_digest=digest,
        operation_id="root:send:0",
        now=NOW,
        slot=slot,
    )
    await store.claim_send(SCOPE, "root:send:0", now=NOW, fence=lease)
    await store.advance_operation(SCOPE, "root:send:0", "failed", now=NOW, fence=lease)
    await store.release_lease(lease)

    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(ProviderError) as refused:
            await send(context(stores()), client)
    assert refused.value.native_code == "mutation_unclassified_refusal"
    assert transport.requests == [] and not transport.violations


async def test_a_connection_refused_on_the_mux_send_path_is_retried(
    stores: Callable[[], PostgresStateStore],
) -> None:
    store = stores()
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("POST", EVENTS_PATH, httpx.ConnectError("connection refused")),
        accepted(),
    )
    async with transport.client(max_retries=MA_MAX_RETRIES) as client:
        await send(context(store), client)
    transport.assert_consumed()
    assert len(transport.requests) == 2
    assert await statuses(store) == ["failed", "accepted", None, None, None, None]
    refused = await store.get_operation(SCOPE, "root:send:0")
    assert refused is not None
    assert MutationFailure.from_json(refused.result["failure"]) == MutationFailure(
        "rejected", retryable=True, reason="connect_error"
    )
