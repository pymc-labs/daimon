"""Owned original billing rows joined through the immutable outcome references."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from daimon.core._models import TenantLedger, TurnOutcome, UsageEvent
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import UnflushedReadState, mux_state
from daimon.core.stores.turn_usage import read_accounting_revisions
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.core.turn.termination import TerminationReason
from daimon.core.usage_delegation import root_usage_outcome_id
from daimon.core.usage_recording import record_provider_usage, record_turn_usage
from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation
from mux.state.lease import Slot
from sqlalchemy import event, text

from ..test_fleet_budget import setup
from ..test_fleet_budget import sm as sm


def measurement(parent, identity):
    native = ResourceRef(
        id="root-session",
        kind="session",
        provider=parent.spec.model.provider,
        tenant_id=parent.fleet.scope.tenant_id,
        account_id=parent.fleet.scope.account_id,
        account_scope_id="owned-offline",
    )
    return UsageObservation(
        id=identity,
        revision=1,
        session=native,
        model=parent.spec.model,
        grain="model_request" if native.provider == "anthropic" else "turn",
        basis="increment" if native.provider == "anthropic" else "cumulative",
        input_tokens=100,
        input_cached_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=10,
        completeness="measured",
        observed_at=datetime.now(UTC),
    )


async def billed(sm, parent, value):
    if value.session.provider == "anthropic":
        await record_turn_usage(
            sessionmaker=sm,
            tenant_id=UUID(parent.fleet.scope.tenant_id),
            platform_user_id="tester",
            observation=value,
            pricing=ModelRates(2, 10, 3, 1),
            channel_id="channel",
        )
    else:
        price = ProviderPrice(
            value.session.provider,
            value.model.id,
            datetime.now(UTC).date(),
            Decimal(2),
            Decimal(10),
            Decimal(1),
        )
        await record_provider_usage(
            sessionmaker=sm,
            binding_id="root-binding",
            observation=value,
            tenant_id=UUID(parent.fleet.scope.tenant_id),
            platform_user_id="tester",
            provider_price=price,
            infrastructure_usd=Decimal(0),
            billing_grain="turn",
            channel_id="channel",
        )


async def attributed(sm, provider="anthropic"):
    parent, run = await setup(sm, provider=provider)
    await billed(sm, parent, measurement(parent, "old-shared-session-work"))
    outcome = TurnObservation(
        sm,
        run.tenant_id,
        "discord",
        "channel",
        "root-thread",
        account_id=parent.fleet.account_id,
        session_id="root-session",
    )
    value = measurement(parent, "exact-current-work")
    with outcome.activate():
        await billed(sm, parent, value)
        if provider == "anthropic":
            outcome.note_usage(value, metered=True)
        outcome.finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()
    async with sm.begin() as db:
        binding = await mux_state.get_binding(
            db, Slot(thread=parent.fleet.root, account_id=str(parent.fleet.account_id))
        )
    return parent, run, outcome, value, binding


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_exact_outcome_join_excludes_old_session_work_and_keeps_one_original_debit(
    sm, provider
):
    parent, run, outcome, value, binding = await attributed(sm, provider)
    async with sm.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))

        def forbidden(*args):
            raise RuntimeError("root reader flushed caller state")

        event.listen(db.sync_session, "before_flush", forbidden)
        rows = await read_accounting_revisions(
            db,
            tenant_id=run.tenant_id,
            binding=binding,
            account_id=parent.fleet.account_id,
            outcome_ids=(outcome.id, outcome.id),
        )
    if rows.usage_refs != ((value.session.id, value.id),) or rows.attribution_complete is not True:
        raise RuntimeError("exact immutable outcome association missing")
    if len(rows.usage_events) != 1 or rows.usage_events[0].event_id != value.id:
        raise RuntimeError("legacy/mirror usage read included another host input")
    if [(row.idempotency_key, row.delta_usd) for row in rows.ledger] != [
        (f"turn:{value.session.id}:{value.id}", Decimal("-.000300"))
    ]:
        raise RuntimeError("original debit was dropped or counted twice")
    if {r.observation_id for r in rows.revisions} != (
        set() if provider == "anthropic" else {value.id}
    ):
        raise RuntimeError("reader fabricated a neutral legacy observation")


@pytest.mark.parametrize("join", [(), (None,), "missing", "mixed"])
async def test_missing_outcome_association_never_falls_back_to_session_history(sm, join):
    parent, run, outcome, value, binding = await attributed(sm)
    ids = (uuid4(),) if join == "missing" else (outcome.id, None) if join == "mixed" else join
    async with sm.begin() as db:
        rows = await read_accounting_revisions(
            db,
            tenant_id=run.tenant_id,
            binding=binding,
            account_id=parent.fleet.account_id,
            outcome_ids=ids,
        )
    if rows.attribution_complete is not False:
        raise RuntimeError("unknown join promoted to complete")
    if join != "mixed" and (rows.ledger or rows.usage_events):
        raise RuntimeError("unknown join summed session history")
    if join == "mixed" and len(rows.ledger) != 1:
        raise RuntimeError("unknown sibling dropped the valid floor")


@pytest.mark.parametrize("source", [TurnOutcome, UsageEvent, TenantLedger])
async def test_exact_root_read_refuses_dirty_source_without_flushing(sm, source):
    parent, run, outcome, value, binding = await attributed(sm)
    from sqlalchemy import select

    async with sm.begin() as db:
        cached = await db.scalar(
            select(source).where(source.id == outcome.id)
            if source is TurnOutcome
            else select(source).where(source.tenant_id == run.tenant_id).order_by(source.id)
        )
        if source is TurnOutcome:
            cached.usage_refs = []
        elif source is UsageEvent:
            cached.input_tokens = 0
        else:
            cached.delta_usd = Decimal(0)

        def forbidden(*args):
            raise RuntimeError("root read flushed dirty caller state")

        event.listen(db.sync_session, "before_flush", forbidden)
        with pytest.raises(UnflushedReadState):
            await read_accounting_revisions(
                db,
                tenant_id=run.tenant_id,
                binding=binding,
                account_id=parent.fleet.account_id,
                outcome_ids=(outcome.id,),
            )
        await db.rollback()


async def test_root_claim_association_keeps_first_uuid_or_unknown_on_replay(sm):
    parent, run = await setup(sm, provider="anthropic")
    value = measurement(parent, "one")
    observer = TurnObservation(
        sm,
        run.tenant_id,
        "discord",
        account_id=parent.fleet.account_id,
        session_id=value.session.id,
    )
    original = uuid4()
    with observer.activate():
        if (
            root_usage_outcome_id(parent.fleet.scope, value.session, previous=None, fresh=True)
            != observer.id
        ):
            raise RuntimeError("first claimed observer not retained")
        if (
            root_usage_outcome_id(parent.fleet.scope, value.session, previous=original, fresh=False)
            != original
        ):
            raise RuntimeError("replay relabeled a fresh observer")
        if (
            root_usage_outcome_id(parent.fleet.scope, value.session, previous=None, fresh=False)
            is not None
        ):
            raise RuntimeError("unknown old claim inferred a fresh outcome")
        with pytest.raises(ValueError):
            root_usage_outcome_id(parent.fleet.scope, value.session, previous=original, fresh=True)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_attributed_native_usage_with_unknown_runtime_is_still_pending(sm, provider):
    parent, run = await setup(sm, provider=provider)
    value = measurement(parent, "unknown-runtime")
    observer = TurnObservation(
        sm,
        run.tenant_id,
        "discord",
        account_id=parent.fleet.account_id,
        session_id=value.session.id,
    )
    with observer.activate():
        settled = await record_provider_usage(
            sessionmaker=sm,
            binding_id="root-binding",
            observation=value,
            tenant_id=run.tenant_id,
            platform_user_id="tester",
            provider_price=None,
            infrastructure_usd=None,
            billing_grain="turn",
            channel_id="channel",
        )
        observer.finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()
    async with sm.begin() as db:
        binding = await mux_state.get_binding(
            db, Slot(thread=parent.fleet.root, account_id=str(parent.fleet.account_id))
        )
        rows = await read_accounting_revisions(
            db,
            tenant_id=run.tenant_id,
            binding=binding,
            account_id=parent.fleet.account_id,
            outcome_ids=(observer.id,),
        )
    if settled or rows.ledger or rows.usage_events or not rows.outbox or rows.outbox[0].settled:
        raise RuntimeError("unknown native charge was promoted to a posted zero")
    if rows.attribution_complete is not True:
        raise RuntimeError("valid association dropped with unknown billing sibling")


@pytest.mark.parametrize("source", ["outcome", "usage", "ledger"])
async def test_root_join_reads_core_update_without_overwriting_cached_values(sm, source):
    from sqlalchemy import select

    parent, run, outcome, value, binding = await attributed(sm)
    async with sm.begin() as db:
        if source == "outcome":
            cached = await db.get(TurnOutcome, outcome.id)
            before = cached.usage_refs
            await db.execute(
                text(
                    'UPDATE turn_outcomes SET usage_refs=\'[{"session_id":"root-session","event_id":"old-shared-session-work"}]\'::jsonb WHERE id=:id'
                ),
                {"id": outcome.id},
            )
        elif source == "usage":
            cached = await db.scalar(
                select(UsageEvent).where(
                    UsageEvent.tenant_id == run.tenant_id, UsageEvent.event_id == value.id
                )
            )
            before = cached.input_tokens
            await db.execute(
                text("UPDATE usage_events SET input_tokens=42 WHERE id=:id"), {"id": cached.id}
            )
        else:
            cached = await db.scalar(
                select(TenantLedger).where(
                    TenantLedger.tenant_id == run.tenant_id,
                    TenantLedger.idempotency_key == f"turn:{value.session.id}:{value.id}",
                )
            )
            before = cached.delta_usd
            await db.execute(
                text("UPDATE tenant_ledger SET delta_usd=-.000700 WHERE id=:id"), {"id": cached.id}
            )
        rows = await read_accounting_revisions(
            db,
            tenant_id=run.tenant_id,
            binding=binding,
            account_id=parent.fleet.account_id,
            outcome_ids=(outcome.id,),
        )
        if source == "outcome":
            if (
                rows.usage_refs != (("root-session", "old-shared-session-work"),)
                or cached.usage_refs != before
            ):
                raise RuntimeError("outcome read used or overwrote cached refs")
        elif source == "usage":
            if rows.usage_events[0].input_tokens != 42 or cached.input_tokens != before:
                raise RuntimeError("usage read used or overwrote cached amount")
        elif rows.ledger[0].delta_usd != Decimal("-.000700") or cached.delta_usd != before:
            raise RuntimeError("ledger read used or overwrote cached debit")
        if db.dirty:
            raise RuntimeError("root reader dirtied caller cache")
        await db.rollback()


async def test_missing_or_foreign_outcome_cannot_expose_another_account_usage(sm):
    parent, run, outcome, value, binding = await attributed(sm)
    async with sm.begin() as db:
        with pytest.raises(ValueError):
            await read_accounting_revisions(
                db,
                tenant_id=run.tenant_id,
                binding=binding,
                account_id=uuid4(),
                outcome_ids=(outcome.id,),
            )
        rows = await read_accounting_revisions(
            db,
            tenant_id=run.tenant_id,
            binding=binding,
            account_id=parent.fleet.account_id,
            outcome_ids=(uuid4(),),
        )
    if rows.attribution_complete is not False or rows.ledger or rows.usage_refs:
        raise RuntimeError("unknown outcome fell back to another claimed observer")
