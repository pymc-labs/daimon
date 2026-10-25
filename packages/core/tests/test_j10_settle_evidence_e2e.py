"""J10: what proves a finished worker is done spending, per provider, on real row shapes.

- Gemini: its send stays `accepted`. It is answered only when the native turn its
  own receipt names has a measured, applied usage revision on the worker's own
  binding; anything less keeps the hold.
- Managed Agents: its debits are the ledger rows keyed exactly
  `turn:<its session>:<event>` for its own usage_events; it is settled only when
  every call has its debit.
- OpenAI: a turn recorded before OpenAI metered it is read again within bounds.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import structlog
from daimon.core._models import AccountingOutbox, TenantLedger
from daimon.core.fleet import worker_slot
from daimon.core.pricing import usage_tokens
from daimon.core.stores import fleet, tenant_ledger, usage_events
from daimon.core.usage_reread import REREAD_AFTER, REREAD_MAX_AGE, reread_unmeasured_usage
from mux.contracts.ids import ModelRef
from mux.contracts.usage import UsageObservation
from sqlalchemy import select

from .test_fleet_budget import add, setup
from .test_fleet_budget import sm as sm
from .test_fleet_budget_batching import _run
from .test_j10_fleet_settles_every_provider_e2e import (
    _available,
    _gemini_interaction,
    _ma_call,
    _openai_turn,
    _parent,
    _prices,
    _record,
    _send,
    _worker,
)


async def _end(sm, run, *workers):
    for worker in workers:
        await fleet.finish(
            sm,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=worker.version,
            status="failed",
            error_code="test",
        )
    current, _ = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    await fleet.set_run_status(
        sm,
        tenant_id=run.tenant_id,
        run_id=run.id,
        expected_version=current.version,
        status="failed",
    )


async def _held(sm, parent, run, prices, worker) -> Decimal:
    """The worker's hold after a later producer pass."""
    p2, _ = await _run(sm, parent, run, cap="1")
    await add(sm, p2, ordinal=0)
    await _available(sm, p2, prices)
    _, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    return next(w.reserved_usd for w in workers if w.id == worker.id)


@pytest.mark.parametrize("evidence", ["measured_applied", "no_usage", "another_binding"])
async def test_an_accepted_gemini_send_is_answered_only_by_its_own_measured_turn(sm, evidence):
    parent, run = await setup(sm, parent=_parent(), funds="10", cap="4", max_workers=4)
    prices = _prices(parent)
    model = parent.models[0]
    worker, binding, native = await _worker(sm, parent, run, 0, model, prices)
    # An earlier interaction, fully on the ledger: the worker's usage state is settled.
    await _record(
        sm, run, binding, _gemini_interaction(native, model, "v1_first", revision=2), prices
    )
    if evidence == "measured_applied":
        await _record(
            sm, run, binding, _gemini_interaction(native, model, "v1_second", revision=2), prices
        )
    elif evidence == "another_binding":
        # The same turn id metered on someone else's binding proves nothing here.
        await _record(
            sm,
            run,
            binding.model_copy(update={"id": "root-binding"}),
            _gemini_interaction(native, model, "v1_second", revision=2).model_copy(
                update={"session": native.model_copy(update={"id": "root-session"})}
            ),
            prices,
        )
    await _send(sm, run, worker, status="accepted", turn_id="v1_second")
    await _end(sm, run, worker)
    held = await _held(sm, parent, run, prices, worker)
    if evidence == "measured_applied":
        assert Decimal(0) < held < Decimal("0.01"), held
    else:
        assert held == Decimal(1)  # its last interaction may still be spending


async def test_a_managed_agents_worker_counts_only_its_own_sessions_debits(sm):
    parent, run = await setup(sm, parent=_parent(), funds="10", cap="4", max_workers=4)
    prices = _prices(parent)
    claude = parent.models[2]
    worker, _, native = await _worker(sm, parent, run, 0, claude, prices)
    own = await _ma_call(sm, run, native, claude.id, "sevt_own")
    assert own is not None and own > 0
    # Another session's call, and a debit on this session's key form with no
    # usage_events row of this session: neither is this worker's spend.
    other = native.model_copy(update={"id": "sesn_someone_else"})
    assert await _ma_call(sm, run, other, claude.id, "sevt_other")
    async with sm() as db, db.begin():
        await tenant_ledger.insert_entry(
            db,
            tenant_id=run.tenant_id,
            delta_usd=Decimal("-0.5"),
            reason="turn_debit",
            idempotency_key=f"turn:{native.id}:sevt_not_a_call_of_it",
            channel_id=run.channel_id,
        )
    await _send(sm, run, worker, status="processed")
    await _end(sm, run, worker)
    assert await _held(sm, parent, run, prices, worker) == own


async def test_a_managed_agents_call_without_its_debit_keeps_the_hold(sm):
    parent, run = await setup(sm, parent=_parent(), funds="10", cap="4", max_workers=4)
    prices = _prices(parent)
    claude = parent.models[2]
    worker, _, native = await _worker(sm, parent, run, 0, claude, prices)
    assert await _ma_call(sm, run, native, claude.id, "sevt_debited")
    observation = UsageObservation(
        id="sevt_undebited",
        revision=1,
        session=native,
        model=ModelRef(provider="anthropic", id=claude.id),
        grain="model_request",
        basis="increment",
        input_tokens=10,
        input_cached_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=1,
        native_meter={},
        completeness="measured",
        observed_at=datetime.now(UTC),
    )
    tokens = usage_tokens(observation)
    assert tokens is not None
    async with sm() as db, db.begin():
        await usage_events.record(
            db,
            tenant_id=run.tenant_id,
            platform_user_id="tester",
            managed_session_id=native.id,
            model=claude.id,
            model_usage=tokens,
            event_id=observation.id,
            channel_id=run.channel_id,
        )
    await _send(sm, run, worker, status="processed")
    await _end(sm, run, worker)
    assert await _held(sm, parent, run, prices, worker) == Decimal(1)


async def _unmetered_openai_row(sm):
    parent, run = await setup(sm, parent=_parent(), funds="10", cap="4", max_workers=4)
    prices = _prices(parent)
    worker, binding, native = await _worker(sm, parent, run, 0, parent.models[1], prices)
    assert not await _record(
        sm, run, binding, _openai_turn(native, "turn_late", metered=False), prices
    )
    async with sm() as db:
        created = await db.scalar(
            select(AccountingOutbox.created_at).where(AccountingOutbox.binding_id == binding.id)
        )
    return run, binding, native, created


async def _pending(sm, binding_id) -> list[tuple[int, bool]]:
    async with sm() as db:
        return [
            (revision, applied is not None)
            for revision, applied in (
                await db.execute(
                    select(AccountingOutbox.revision, AccountingOutbox.applied_at)
                    .where(AccountingOutbox.binding_id == binding_id)
                    .order_by(AccountingOutbox.revision)
                )
            ).all()
        ]


async def test_a_failed_provider_read_leaves_the_row_for_the_next_pass(sm):
    _, binding, _, created = await _unmetered_openai_row(sm)

    async def failing(native, revision):
        raise TimeoutError("provider unavailable")

    with structlog.testing.capture_logs() as logs:
        recorded = await reread_unmeasured_usage(
            sm, markup=Decimal(1), list_usage=failing, now=created + REREAD_AFTER * 2
        )
    assert recorded == 0 and await _pending(sm, binding.id) == [(1, False)]
    assert any(e["event"] == "accounting.usage_reread_failed" for e in logs)


async def test_a_turn_openai_still_has_not_metered_stays_pending(sm):
    _, binding, native, created = await _unmetered_openai_row(sm)

    async def still_null(session, revision):
        return [_openai_turn(session, "turn_late", revision=revision("x"), metered=False)]

    assert (
        await reread_unmeasured_usage(
            sm, markup=Decimal(1), list_usage=still_null, now=created + REREAD_AFTER * 2
        )
        == 0
    )
    assert await _pending(sm, binding.id) == [(1, False)]


@pytest.mark.parametrize("age", ["too_young", "too_old"])
async def test_a_turn_outside_the_reread_window_is_not_read(sm, age):
    _, binding, native, created = await _unmetered_openai_row(sm)
    calls: list[object] = []

    async def lister(session, revision):
        calls.append(session)
        return [_openai_turn(session, "turn_late", revision=revision("openai:turn:turn_late"))]

    now = (
        created + REREAD_AFTER - timedelta(seconds=1)
        if age == "too_young"
        else created + REREAD_MAX_AGE + timedelta(seconds=30)
    )
    with structlog.testing.capture_logs() as logs:
        assert await reread_unmeasured_usage(sm, markup=Decimal(1), list_usage=lister, now=now) == 0
    assert calls == [] and await _pending(sm, binding.id) == [(1, False)]
    expired = [e for e in logs if e["event"] == "accounting.usage_reread_expired"]
    # Past the max age it is logged once, and still held: nothing releases on age.
    assert len(expired) == (1 if age == "too_old" else 0)


async def test_a_turn_metered_later_is_recorded_once_and_closes_the_unmetered_one(sm):
    run, binding, native, created = await _unmetered_openai_row(sm)

    async def metered(session, revision):
        return [_openai_turn(session, "turn_late", revision=revision("openai:turn:turn_late"))]

    now = created + REREAD_AFTER * 2
    assert await reread_unmeasured_usage(sm, markup=Decimal(1), list_usage=metered, now=now) == 1
    assert await _pending(sm, binding.id) == [(1, True), (2, True)]
    # Idempotent: nothing left to read, nothing debited twice.
    assert await reread_unmeasured_usage(sm, markup=Decimal(1), list_usage=metered, now=now) == 0
    async with sm() as db:
        debits = (
            await db.execute(
                select(TenantLedger.delta_usd, TenantLedger.estimate).where(
                    TenantLedger.tenant_id == run.tenant_id, TenantLedger.delta_usd < 0
                )
            )
        ).all()
    assert debits == [(Decimal("-0.001366"), "cache_writes_unreported")]


async def test_a_slot_lease_still_keeps_an_answered_gemini_worker_held(sm):
    """The receipt rule only answers the send; a live lease is still in flight."""
    from daimon.core.stores import mux_state

    parent, run = await setup(sm, parent=_parent(), funds="10", cap="4", max_workers=4)
    prices = _prices(parent)
    model = parent.models[0]
    worker, binding, native = await _worker(sm, parent, run, 0, model, prices)
    await _record(sm, run, binding, _gemini_interaction(native, model, "v1_t", revision=2), prices)
    await _send(sm, run, worker, status="accepted", turn_id="v1_t")
    async with sm() as db, db.begin():
        await mux_state.acquire_lease(
            db,
            worker_slot(run, worker),
            holder="worker",
            turn_id="current",
            clock=mux_state.database_clock,
            ttl=timedelta(minutes=5),
        )
    await _end(sm, run, worker)
    assert await _held(sm, parent, run, prices, worker) == Decimal(1)


async def test_the_openai_lister_reads_one_session_under_its_own_tenant(monkeypatch):
    """No platform scope: the read acts for the session's tenant and account, and the
    driver's own mapper builds the observation at the revision the pass asked for."""
    import mux.drivers.openai as openai_driver
    from daimon.core.usage_reread import openai_usage_lister
    from mux.contracts.ids import ResourceRef

    session = ResourceRef(
        provider="openai",
        kind="session",
        id="conv_1",
        tenant_id="tenant-1",
        account_id="account-1",
        account_scope_id="daimon-deployment",
    )
    requests: list[tuple[str, str]] = []

    class Transport:
        async def request(self, method, path, *, body=None, query=None, key=None):
            requests.append((method, path))
            return {
                "data": [
                    {
                        "id": "turn_b",
                        "session_id": "conv_1",
                        "usage": {
                            "input_tokens": 13351,
                            "output_tokens": 61,
                            "input_tokens_details": {"cached_tokens": 0},
                        },
                    }
                ],
                "has_more": False,
                "last_id": "turn_b",
            }

    monkeypatch.setattr(openai_driver, "configured_transport", lambda **_: Transport())
    list_usage = openai_usage_lister(api_key="test-key")
    assert list_usage is not None
    (observation,) = await list_usage(session, lambda observation_id: 2)
    assert requests == [("GET", "/agents/sessions/conv_1/turns")]
    assert (observation.id, observation.revision, observation.completeness) == (
        "openai:turn:turn_b",
        2,
        "measured",
    )
    assert observation.input_cache_write_tokens is None  # OpenAI never counts writes
    with pytest.raises(ValueError, match="tenant and account"):
        await list_usage(session.model_copy(update={"account_id": None}), lambda _: 1)
