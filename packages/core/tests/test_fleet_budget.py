"""Owned PostgreSQL product admission/exposure, with mock tariffs and no SDK I/O."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from daimon.core._models import FleetRun, FleetWorker, ProviderBindingSlot, TenantLedger
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.authz import AgentRef, FleetFacts, Place, Subject
from daimon.core.channel_budget import (
    admit_fleet_worker,
    produce_fleet_budget,
    watch_fleet_budget,
)
from daimon.core.fleet import FleetConflict, holds_slot, worker_slot
from daimon.core.neutral_resources import WorkerLimits, WorkerPermissions, resolve_worker_spec
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import (
    UnflushedReadState,
    channel_budgets,
    fleet,
    mux_state,
    security_audit,
    tenant_ledger,
    tenants,
)
from daimon.core.stores.turn_usage import (
    read_accounting_revisions,
    read_fleet_accounting_revisions,
)
from daimon.core.usage_delegation import HostUsageBinding, read_delegated_usage
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    NetworkPolicy,
    ProviderBinding,
    ToolSpec,
)
from mux.contracts.usage import UsageObservation
from mux.profiles import backend_selection, get_profile
from sqlalchemy import delete, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

POLICY = TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini"))


def root(provider="gemini", *, budget="10"):
    now = datetime.now(UTC)
    selection = backend_selection(provider)
    model = ModelRef(provider=provider, id=selection.model or "claude-haiku-5-5")
    scope = Scope(
        tenant_id=str(uuid4()),
        account_id=str(uuid4()),
        principal_id="host",
        authorization_id="owned-origin",
    )
    thread = ThreadRef(
        channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="channel"),
        thread_id="root-thread",
    )
    profile = get_profile(selection.profile)
    spec = AgentSpec(
        name="root",
        model=model,
        system="Protected root instruction",
        tools=(ToolSpec(name=profile.execution_tool, kind="builtin"),)
        if profile.implicit_execution
        else (),
    )
    return WorkerPermissions(
        fleet=FleetFacts(
            scope=scope,
            root=thread,
            origin_id=uuid4(),
            account_id=UUID(scope.account_id),
            run_id=uuid4(),
            place=Place(channel_id=thread.thread_id, parent_channel_id=thread.channel.channel_id),
            enabled=True,
            active=True,
        ),
        subject=Subject(platform_user_id="tester"),
        agent=AgentRef.of("root"),
        spec=spec,
        base_system=spec.system,
        backend_grants=(selection,),
        models=(model,),
        limits=WorkerLimits(
            budget_usd=Decimal(budget), deadline=now + timedelta(minutes=10), max_turns=4
        ),
        environment=EnvironmentSpec(
            name="owned-box",
            network=NetworkPolicy(mode="limited", allowed_hosts=("offline.invalid",)),
        ),
        agent_id=uuid4(),
    )


def tariffs(parent):
    model = parent.spec.model
    return {
        (model.provider, model.id): ModelRates(2, 10, 3, 1)
        if model.provider == "anthropic"
        else ProviderPrice(
            model.provider,
            model.id,
            datetime.now(UTC).date(),
            Decimal(2),
            Decimal(10),
            Decimal(1),
            Decimal(3),
        )
    }


@pytest_asyncio.fixture
async def sm(db_preflight, db_engine, db_clean):
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def setup(sm, *, provider="gemini", funds="10", cap="3", parent=None, max_workers=8):
    parent = parent or root(provider)
    async with sm() as db, db.begin():
        tenant = await make_tenant(db, id=UUID(parent.fleet.scope.tenant_id))
        await make_account(db, id=parent.fleet.account_id, tenant=tenant)
        await tenants.set_funding_mode(db, tenant_id=tenant.id, funding_mode="prepaid")
        await tenant_ledger.insert_entry(
            db,
            tenant_id=tenant.id,
            delta_usd=Decimal(funds),
            reason="trial_credit",
            idempotency_key=f"trial:{tenant.id}",
        )
        await mux_state.put_binding(
            db,
            ProviderBinding(
                id="root-binding",
                thread=parent.fleet.root,
                legacy_account_id=str(parent.fleet.account_id),
                provider=parent.spec.model.provider,
                profile=parent.backend_grants[0].profile,
                native_refs={"session": "root-session"},
                generation=1,
                config_revision=0,
            ),
            expected_generation=0,
        )
        run = await fleet.create_run(
            db,
            tenant_id=tenant.id,
            orchestrator_account_id=parent.fleet.account_id,
            orchestrator_session_id="root-session",
            orchestrator_binding_id="root-binding",
            platform="discord",
            channel_id="channel",
            thread_id="root-thread",
            operation_key="owned-spawn",
            total_budget_usd=Decimal(cap),
            deadline_at=parent.limits.deadline,
            max_workers=max_workers,
            created_by="host",
        )
    parent = replace(parent, fleet=replace(parent.fleet, run_id=run.id))
    return parent, run


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
async def test_concurrent_worker_reservations_cannot_borrow_another_provider_line(sm, provider):
    other = "openai" if provider != "openai" else "gemini"
    parent = root(provider)
    parent = replace(
        parent,
        limits=WorkerLimits.model_validate(
            parent.limits.model_dump()
            | {
                "provider_limits_usd": {provider: Decimal("0.9"), other: Decimal("2.1")},
            }
        ),
    )
    parent, _ = await setup(sm, provider=provider, parent=parent)
    first = await add(sm, parent, ordinal=0, budget="0.6")
    second = await add(sm, parent, ordinal=1, budget="0.6")
    outcomes = await asyncio.gather(reserve(sm, parent, first), reserve(sm, parent, second))
    assert sum(outcome.admitted for outcome in outcomes) == 1
    refused = next(outcome for outcome in outcomes if not outcome.admitted)
    assert refused.reason == "provider_budget_exceeded"
    snapshot = await read(
        sm, parent, next(outcome.worker for outcome in outcomes if outcome.admitted)
    )
    assert snapshot.run_exposure_usd == Decimal("0.6")
    assert snapshot.worker_exposure_usd == Decimal("0.6")


async def test_provider_reservation_boundary_uses_exact_money_at_low_precision(sm):
    parent = root()
    parent = replace(
        parent,
        limits=WorkerLimits.model_validate(
            parent.limits.model_dump()
            | {
                "provider_limits_usd": {"gemini": Decimal("0.246913"), "openai": Decimal("2")},
            }
        ),
    )
    parent, _ = await setup(sm, parent=parent)
    first = await add(sm, parent, ordinal=0, budget="0.123457")
    second = await add(sm, parent, ordinal=1, budget="0.123457")
    with localcontext() as arithmetic:
        arithmetic.prec = 6
        assert (await reserve(sm, parent, first)).admitted
        refused = await reserve(sm, parent, second)
    assert not refused.admitted
    assert refused.reason == "provider_budget_exceeded"
    assert refused.run_exposure_usd == Decimal("0.123457")


async def test_provider_caps_cannot_claim_more_than_the_run_authority(sm):
    parent = root()
    parent = replace(
        parent,
        limits=WorkerLimits.model_validate(
            parent.limits.model_dump()
            | {
                "provider_limits_usd": {"gemini": Decimal("2"), "openai": Decimal("2")},
            }
        ),
    )
    parent, _ = await setup(sm, parent=parent)
    worker = await add(sm, parent)
    with pytest.raises(FleetConflict, match="provider limits exceed"):
        await reserve(sm, parent, worker)


async def test_a_measured_provider_overrun_retains_its_exposure(sm):
    parent = root()
    parent = replace(
        parent,
        limits=WorkerLimits.model_validate(
            parent.limits.model_dump()
            | {
                "provider_limits_usd": {"gemini": Decimal("1"), "openai": Decimal("2")},
            }
        ),
    )
    parent, run = await setup(sm, parent=parent)
    worker = await add(sm, parent, budget="0.8")
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=600_000, outputs=0)
    current = await read(sm, parent, worker)
    assert not current.admitted
    assert current.reason == "provider_budget_exceeded"
    assert current.worker_exposure_usd == current.run_exposure_usd == Decimal("1.2")
    again = await read(sm, parent, worker)
    assert again.run_exposure_usd == Decimal("1.2")


async def add(sm, parent, *, ordinal=0, budget="1"):
    limits = parent.limits.model_copy(update={"budget_usd": Decimal(budget)})
    resolved = resolve_worker_spec(
        parent, parent.spec, policy=POLICY, now=datetime.now(UTC), limits=limits
    )
    async with sm() as db, db.begin():
        return await fleet.add_worker(
            db,
            tenant_id=UUID(parent.fleet.scope.tenant_id),
            run_id=parent.fleet.run_id,
            ordinal=ordinal,
            role="worker",
            spec=resolved.spec.model_dump(mode="json"),
            spec_digest=resolved.digest,
            backend=resolved.backend.backend,
            profile=resolved.backend.profile,
            model=resolved.spec.model.id,
            budget_usd=Decimal(budget),
            deadline_at=limits.deadline,
            max_turns=limits.max_turns,
        )


async def next_run(sm, parent, run, *, cap="2"):
    async with sm() as db, db.begin():
        other = await fleet.create_run(
            db,
            tenant_id=run.tenant_id,
            orchestrator_account_id=parent.fleet.account_id,
            orchestrator_session_id=run.orchestrator_session_id,
            orchestrator_binding_id=run.orchestrator_binding_id,
            platform=run.platform,
            channel_id=run.channel_id,
            thread_id=run.thread_id,
            operation_key=f"next:{uuid4()}",
            total_budget_usd=Decimal(cap),
            deadline_at=run.deadline_at,
            max_workers=4,
            created_by="host",
        )
    return replace(parent, fleet=replace(parent.fleet, run_id=other.id)), other


async def reserve(sm, parent, worker, *, session=None, prices=None):
    with localcontext() as context:
        context.prec = 80
        budget = worker.budget_usd.normalize()
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": budget}),
    )

    async def admit(db):
        return await admit_fleet_worker(
            db,
            parent=parent,
            policy=POLICY,
            resolved=resolved,
            ordinal=worker.ordinal,
            role=worker.role,
            prices=prices if prices is not None else tariffs(parent),
            markup=Decimal(1),
        )

    if session is not None:
        return await admit(session)
    async with sm() as db, db.begin():
        return await admit(db)


async def read(sm, parent, worker, *, prices=None):
    async with sm() as db, db.begin():
        return (
            await produce_fleet_budget(
                db,
                parent=parent,
                prices=prices if prices is not None else tariffs(parent),
                markup=Decimal(1),
            )
        )[worker.id]


async def usage(
    sm, parent, run, worker, *, inputs=100, outputs=None, revision=1, infrastructure=None
):
    provider = parent.spec.model.provider
    binding = ProviderBinding(
        id=f"worker-{worker.id}",
        thread=worker_slot(run, worker).thread,
        legacy_account_id=str(parent.fleet.account_id),
        provider=provider,
        profile=worker.profile,
        native_refs={"session": f"native-{worker.id}"},
        generation=1,
        config_revision=0,
    )
    native = ResourceRef(
        id=binding.native_refs["session"],
        kind="session",
        provider=provider,
        account_scope_id="owned-workspace",
        tenant_id=str(run.tenant_id),
        account_id=str(run.orchestrator_account_id),
    )
    value = UsageObservation(
        id="original-meter",
        session=native,
        revision=revision,
        model=parent.spec.model,
        turn_id="original-turn",
        grain="model_request" if provider == "anthropic" else "turn",
        basis="cumulative",
        input_tokens=inputs,
        input_cached_tokens=0,
        input_cache_write_tokens=0,
        output_tokens=outputs,
        observed_at=datetime.now(UTC),
        completeness="partial" if outputs is None else "measured",
        native_meter={"offline": True},
    )
    async with sm() as db, db.begin():
        if revision == 1:
            await mux_state.put_binding(db, binding, expected_generation=0)
        await record_observation_usage(
            db,
            binding_id=binding.id,
            observation=value,
            tenant_id=run.tenant_id,
            platform_user_id="tester",
            pricing=ModelRates(2, 10, 3, 1),
            provider_price=tariffs(parent)[(provider, parent.spec.model.id)]
            if provider != "anthropic"
            else None,
            infrastructure_usd=infrastructure,
            billing_grain=value.grain,
            channel_id=run.channel_id,
        )
    return value


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_run_accounting_reader_retains_original_revisions_and_only_owned_debits(sm, provider):
    parent, run = await setup(sm, provider=provider)
    worker = await add(sm, parent)
    await reserve(sm, parent, worker)
    values = [
        await usage(
            sm,
            parent,
            run,
            worker,
            inputs=inputs,
            outputs=outputs,
            revision=revision,
            infrastructure=Decimal(0),
        )
        for revision, inputs, outputs in [(1, 100, 10), (2, 200, 20), (3, 150, 15)]
    ]
    claimed = await fleet.claim_worker(
        sm, tenant_id=run.tenant_id, run_id=run.id, worker_id=worker.id, cap=8
    )
    assert claimed is not None
    await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=claimed.version,
        binding_id=f"worker-{worker.id}",
    )
    other_parent, other_run = await next_run(sm, parent, run)
    other_worker = await add(sm, other_parent)
    await usage(sm, other_parent, other_run, other_worker, outputs=10, infrastructure=Decimal(0))
    async with sm.begin() as db:
        before = await tenant_ledger.list_for_tenant(db, tenant_id=run.tenant_id)
        graph = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
    async with sm.begin() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        with localcontext() as arithmetic:
            arithmetic.prec = 2
            report = await read_fleet_accounting_revisions(
                db, tenant_id=run.tenant_id, run_id=run.id
            )
    assert report.run == graph[0]
    assert len(report.workers) == 1
    member = report.workers[0]
    assert member.worker_id == worker.id and member.worker_version == graph[1][0].version
    assert member.issue is None and member.reserved_usd == Decimal(1)
    assert member.rows is not None
    assert tuple(row.observation for row in member.rows.revisions) == tuple(values)
    assert all(row.settled for row in member.rows.outbox)
    assert len(member.rows.ledger) == 3
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        debit = -sum((row.delta_usd for row in member.rows.ledger), Decimal(0))
    assert debit == Decimal(".000450")
    assert {row.id for row in member.rows.ledger} <= {row.id for row in before}
    assert all(str(other_worker.id) not in row.idempotency_key for row in member.rows.ledger)
    async with sm.begin() as db:
        after = await tenant_ledger.list_for_tenant(db, tenant_id=run.tenant_id)
        assert before == after
        assert graph == await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
    if member.issue is not None or debit != Decimal(".000450") or before != after:
        raise RuntimeError("run accounting lost original exact money or mutated its source")


@pytest.mark.parametrize("fault", ["tenant", "run"])
async def test_run_accounting_refuses_unknown_scope_before_binding_reads(sm, monkeypatch, fault):
    parent, run = await setup(sm)
    await add(sm, parent)

    async def forbidden(*args, **kwargs):
        raise RuntimeError("unknown run reached an accounting binding")

    monkeypatch.setattr(mux_state, "get_binding", forbidden)
    async with sm.begin() as db:
        with pytest.raises(FleetConflict):
            await read_fleet_accounting_revisions(
                db,
                tenant_id=uuid4() if fault == "tenant" else run.tenant_id,
                run_id=uuid4() if fault == "run" else run.id,
            )


async def test_run_accounting_preserves_unknown_usage_and_unaccepted_binding(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    await reserve(sm, parent, worker)
    async with sm.begin() as db:
        report = await read_fleet_accounting_revisions(db, tenant_id=run.tenant_id, run_id=run.id)
    member = report.workers[0]
    assert member.rows is None and member.issue == "binding_not_accepted"
    assert member.reserved_usd == Decimal(1)
    value = await usage(sm, parent, run, worker, inputs=100, outputs=None)
    async with sm.begin() as db:
        report = await read_fleet_accounting_revisions(db, tenant_id=run.tenant_id, run_id=run.id)
    member = report.workers[0]
    assert member.issue == "binding_not_accepted" and member.rows is not None
    assert member.rows.revisions[0].observation == value
    assert member.rows.revisions[0].observation.output_tokens is None
    assert member.rows.ledger == () and not member.rows.outbox[0].settled
    assert member.reserved_usd == Decimal(1)


async def test_run_accounting_refuses_a_swapped_accepted_binding(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    await usage(sm, parent, run, worker)
    claimed = await fleet.claim_worker(
        sm, tenant_id=run.tenant_id, run_id=run.id, worker_id=worker.id, cap=8
    )
    assert claimed is not None
    await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=claimed.version,
        binding_id="foreign-binding",
    )
    async with sm.begin() as db:
        with pytest.raises(ValueError, match="differs from its worker"):
            await read_fleet_accounting_revisions(db, tenant_id=run.tenant_id, run_id=run.id)


@pytest.mark.parametrize("reader", ["fleet", "binding", "delegated"])
async def test_run_accounting_does_not_flush_caller_changes(sm, reader):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    value = await usage(sm, parent, run, worker, outputs=10, infrastructure=Decimal(0))
    async with sm() as db, db.begin():
        await db.execute(text("SET TRANSACTION READ ONLY"))
        rows = (
            (await read_fleet_accounting_revisions(db, tenant_id=run.tenant_id, run_id=run.id))
            .workers[0]
            .rows
        )
        if rows is None or rows.binding is None:
            raise RuntimeError("test requires an original private worker binding")
        pending = TenantLedger(
            tenant_id=run.tenant_id,
            delta_usd=Decimal(1),
            reason="trial_credit",
            idempotency_key="must-not-flush",
        )
        db.add(pending)
        if reader == "fleet":
            await read_fleet_accounting_revisions(db, tenant_id=run.tenant_id, run_id=run.id)
        elif reader == "binding":
            await read_accounting_revisions(db, tenant_id=run.tenant_id, binding=rows.binding)
        else:
            await read_delegated_usage(
                db,
                scope=parent.fleet.scope,
                bindings=(HostUsageBinding(rows.binding, value.session),),
                prices=tariffs(parent),
            )
        if pending not in db.new or not db.autoflush:
            raise RuntimeError("read flushed or reconfigured caller session")
        await db.rollback()


@pytest.mark.parametrize("reader", ["fleet", "binding", "delegated"])
@pytest.mark.parametrize("kind", ["exposure", "ledger"])
@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("readonly", [False, True])
async def test_run_accounting_scopes_unflushed_persistent_state(
    sm, reader, kind, deleted, autoflush, readonly
):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    await reserve(sm, parent, worker)
    value = await usage(sm, parent, run, worker, outputs=10, infrastructure=Decimal(0))
    async with sm() as db, db.begin():
        await db.execute(
            text(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                + (", READ ONLY" if readonly else "")
            )
        )
        original = await read_fleet_accounting_revisions(db, tenant_id=run.tenant_id, run_id=run.id)
        rows = original.workers[0].rows
        if (
            original.run.reserved_usd != Decimal(1)
            or original.workers[0].reserved_usd != Decimal(1)
            or rows is None
            or rows.binding is None
            or tuple(row.delta_usd for row in rows.ledger) != (Decimal("-.000300"),)
        ):
            raise RuntimeError("test requires genuine funded exposure and an original debit")
        if kind == "exposure":
            affected = [await db.get(FleetRun, run.id), await db.get(FleetWorker, worker.id)]
        else:
            affected = [await db.get(TenantLedger, rows.ledger[0].id)]
        with db.no_autoflush:
            for row in affected:
                if row is None:
                    raise RuntimeError("persisted financial source is unavailable")
                if deleted:
                    await db.delete(row)
                elif kind == "exposure":
                    row.reserved_usd = Decimal(0)
                else:
                    row.delta_usd = Decimal(0)
        db.autoflush = autoflush

        def forbid_flush(*args):
            raise RuntimeError("persisted read triggered an ORM flush")

        event.listen(db.sync_session, "before_flush", forbid_flush)

        async def collect():
            if reader == "fleet":
                return await read_fleet_accounting_revisions(
                    db, tenant_id=run.tenant_id, run_id=run.id
                )
            elif reader == "binding":
                return await read_accounting_revisions(
                    db, tenant_id=run.tenant_id, binding=rows.binding
                )
            else:
                return await read_delegated_usage(
                    db,
                    scope=parent.fleet.scope,
                    bindings=(HostUsageBinding(rows.binding, value.session),),
                    prices=tariffs(parent),
                )

        if kind == "exposure" and reader != "fleet":
            await collect()  # This reader does not read fleet exposure tables.
        else:
            with pytest.raises(UnflushedReadState, match="unflushed (update|delete) on"):
                await collect()
        if not all(row in (db.deleted if deleted else db.dirty) for row in affected):
            raise RuntimeError("financial read changed the caller's persistent state")
        if db.autoflush is not autoflush:
            raise RuntimeError("persisted read changed caller autoflush")
        await db.rollback()
    async with sm.begin() as db:
        if original != await read_fleet_accounting_revisions(
            db, tenant_id=run.tenant_id, run_id=run.id
        ):
            raise RuntimeError("financial read changed the original persisted facts")


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize(
    ("reader", "kind"),
    [("fleet", "exposure"), ("fleet", "ledger"), ("binding", "ledger"), ("delegated", "ledger")],
)
async def test_run_accounting_reads_core_updates_without_overwriting_clean_cache(
    sm, external, reader, kind
):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    await reserve(sm, parent, worker)
    value = await usage(sm, parent, run, worker, outputs=10, infrastructure=Decimal(0))
    async with sm() as db:
        async with db.begin():
            original = await read_fleet_accounting_revisions(
                db, tenant_id=run.tenant_id, run_id=run.id
            )
            rows = original.workers[0].rows
            if rows is None or rows.binding is None or len(rows.ledger) != 1:
                raise RuntimeError("test requires a genuine original debit")
            if kind == "exposure":
                cached = [await db.get(FleetRun, run.id), await db.get(FleetWorker, worker.id)]
                changes = [("fleet_run", run.id), ("fleet_worker", worker.id)]
                field, expected = "reserved_usd", Decimal(2)
            else:
                cached = [await db.get(TenantLedger, rows.ledger[0].id)]
                changes = [("tenant_ledger", rows.ledger[0].id)]
                field, expected = "delta_usd", Decimal("-.000700")
            before = tuple(getattr(row, field) for row in cached)

            async def update_source(writer):
                for table, identity in changes:
                    await writer.execute(
                        text(f"UPDATE {table} SET {field}=:amount WHERE id=:id"),
                        {"amount": expected, "id": identity},
                    )

            async def collect():
                def forbid_flush(*args):
                    raise RuntimeError("persisted read triggered an ORM flush")

                event.listen(db.sync_session, "before_flush", forbid_flush)
                if reader == "fleet":
                    report = await read_fleet_accounting_revisions(
                        db, tenant_id=run.tenant_id, run_id=run.id
                    )
                    if kind == "exposure":
                        observed = (report.run.reserved_usd, report.workers[0].reserved_usd)
                    else:
                        if report.workers[0].rows is None:
                            raise RuntimeError("financial read lost the original binding")
                        observed = tuple(row.delta_usd for row in report.workers[0].rows.ledger)
                elif reader == "binding":
                    report = await read_accounting_revisions(
                        db, tenant_id=run.tenant_id, binding=rows.binding
                    )
                    observed = tuple(row.delta_usd for row in report.ledger)
                else:
                    report = await read_delegated_usage(
                        db,
                        scope=parent.fleet.scope,
                        bindings=(HostUsageBinding(rows.binding, value.session),),
                        prices=tariffs(parent),
                    )
                    observed = (-report.ledger_debit_usd,)
                if observed != (expected,) * len(cached):
                    raise RuntimeError("financial read returned stale cached money")
                if tuple(getattr(row, field) for row in cached) != before or db.dirty:
                    raise RuntimeError("financial read overwrote caller cache")

            if not external:
                await update_source(db)
                await collect()
                await db.rollback()
                return
        async with sm.begin() as writer:
            await update_source(writer)
        async with db.begin():
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            await collect()
    async with sm.begin() as verifier:
        for table, identity in changes:
            actual = await verifier.scalar(
                text(f"SELECT {field} FROM {table} WHERE id=:id"), {"id": identity}
            )
            if actual != expected:
                raise RuntimeError("financial read changed the persisted Core update")


async def test_run_accounting_missing_accepted_slot_remains_unknown_with_hold(sm):
    parent, run = await setup(sm)
    worker, binding = await bound_worker(sm, parent, run)
    async with sm.begin() as db:
        await db.execute(
            delete(ProviderBindingSlot).where(ProviderBindingSlot.binding_id == binding.id)
        )
    async with sm.begin() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        report = await read_fleet_accounting_revisions(db, tenant_id=run.tenant_id, run_id=run.id)
    member = report.workers[0]
    assert member.worker_id == worker.id and member.binding_id == binding.id
    assert member.rows is None and member.issue == "binding_unavailable"
    assert member.reserved_usd == report.run.reserved_usd == Decimal(1)


async def test_run_accounting_refuses_binding_change_during_read(sm, monkeypatch):
    parent, run = await setup(sm)
    await bound_worker(sm, parent, run)
    original = mux_state.get_binding
    calls = 0

    async def changing(session, slot, **kwargs):
        nonlocal calls
        binding = await original(session, slot, **kwargs)
        calls += 1
        if calls == 2 and binding is not None:
            return binding.model_copy(update={"generation": binding.generation + 1})
        return binding

    monkeypatch.setattr(mux_state, "get_binding", changing)
    async with sm.begin() as db:
        with pytest.raises(ValueError, match="changed during capture"):
            await read_fleet_accounting_revisions(db, tenant_id=run.tenant_id, run_id=run.id)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_real_reservation_is_idempotent_and_unknown_consumes_bound_after_restart(
    sm, provider
):
    parent, run = await setup(sm, provider=provider)
    worker = await add(sm, parent)
    first = await reserve(sm, parent, worker)
    again = await reserve(sm, parent, worker)
    assert first.admitted and again.admitted
    assert first.worker_exposure_usd == again.worker_exposure_usd == Decimal(1)
    assert first.run_exposure_usd == again.run_exposure_usd == Decimal(1)
    await usage(sm, parent, run, worker, inputs=100, outputs=None)
    # Independent factory/controller lifetime; no retained Python budget cache.
    restarted = async_sessionmaker(sm.kw["bind"], expire_on_commit=False)
    current = await read(restarted, parent, worker)
    assert current.worker_exposure_usd == Decimal(1)
    assert current.worker_floor_usd >= Decimal(".0002")
    assert current.run_exposure_usd == Decimal(1)
    async with sm() as db:
        audits = await security_audit.list_events(db, tenant_id=run.tenant_id)
        ledger = await tenant_ledger.list_for_tenant(db, tenant_id=run.tenant_id)
    assert len(audits) == 1
    # list_for_tenant has no order; compare the reasons as a multiset.
    assert sorted(entry.reason for entry in ledger) == (
        ["trial_credit", "turn_debit"] if provider == "anthropic" else ["trial_credit"]
    )


async def test_concurrent_spawns_cannot_spend_same_run_room(sm):
    parent, _ = await setup(sm, cap="1")
    a = await add(sm, parent, budget=".6")
    b = await add(sm, parent, ordinal=1, budget=".6")
    results = await asyncio.gather(reserve(sm, parent, a), reserve(sm, parent, b))
    assert sum(value.admitted for value in results) == 1
    current = await read(sm, parent, a)
    assert current.run_exposure_usd == Decimal(".6")


async def test_prepaid_tenant_cap_and_authenticated_parent_cap_refuse_spawn(sm):
    parent, _ = await setup(sm, funds=".5", cap="3")
    worker = await add(sm, parent)
    refused = await reserve(sm, parent, worker)
    assert not refused.admitted and refused.reason == "tenant_budget_exceeded"
    assert refused.worker_exposure_usd == 0
    narrow = replace(parent, limits=parent.limits.model_copy(update={"budget_usd": Decimal(".2")}))
    with pytest.raises((FleetConflict, ValueError)):
        await reserve(sm, narrow, worker)


async def test_real_usage_overrun_persists_and_refuses_new_spawn(sm):
    parent, run = await setup(sm, cap="1")
    a = await add(sm, parent, budget=".5")
    b = await add(sm, parent, ordinal=1, budget=".1")
    assert (await reserve(sm, parent, a)).admitted
    await usage(sm, parent, run, a, inputs=1_000_000, outputs=None)
    current = await read(sm, parent, a)
    assert current.worker_floor_usd == Decimal(2)
    assert current.worker_exposure_usd == current.run_exposure_usd == Decimal(2)
    refused = await reserve(sm, parent, b)
    assert not refused.admitted and refused.reason == "run_budget_exceeded"
    assert refused.worker_exposure_usd == 0 and refused.run_exposure_usd == 2


async def test_watchdog_uses_product_usage_and_retains_hold_on_cancel(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent, budget=".01")
    assert (await reserve(sm, parent, worker)).admitted
    cancelled, finished = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled={worker.id: cancelled},
            finished=finished,
            poll_interval=0.01,
            read_timeout=3,
        )
    )
    await usage(sm, parent, run, worker, inputs=10_000, outputs=None)
    try:
        await asyncio.wait_for(cancelled.wait(), 5)
        assert not task.done()
        finished.set()
        assert await asyncio.wait_for(task, 3) == "finished"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    current = await read(sm, parent, worker)
    assert current.worker_exposure_usd == Decimal(".02")


async def test_low_decimal_precision_does_not_round_reservations_or_run_sums(sm):
    parent, _ = await setup(sm, cap="2.45")
    a = await add(sm, parent, budget="1.2345")
    b = await add(sm, parent, ordinal=1, budget="1.23")
    with localcontext() as context:
        context.prec = 2
        first = await reserve(sm, parent, a)
        refused = await reserve(sm, parent, b)
    assert first.admitted and first.run_exposure_usd == Decimal("1.2345")
    assert not refused.admitted
    assert refused.run_exposure_usd == Decimal("1.2345")


async def test_zero_native_usage_and_terminal_status_cannot_free_unknown_hold(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    funded = await reserve(sm, parent, worker)
    assert funded.admitted
    await usage(sm, parent, run, worker, inputs=None, outputs=None)
    current = await read(sm, parent, worker)
    assert current.worker_floor_usd == 0 and current.worker_exposure_usd == 1
    async with sm() as db, db.begin():
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=current.worker.version,
            status="cancelled",
            error_code=None,
        )
    after = await read(sm, parent, worker)
    assert after.reason == "terminal" and after.worker_exposure_usd == 1
    assert after.prepaid_available_usd == Decimal(9)


async def test_active_channel_budget_refuses_total_run_bound(sm):
    parent, run = await setup(sm, cap="2")
    worker = await add(sm, parent, budget=".1")
    async with sm() as db, db.begin():
        await channel_budgets.set_channel_budget(
            db,
            tenant_id=run.tenant_id,
            platform=run.platform,
            channel_id=run.channel_id,
            limit_usd=Decimal(".5"),
            window="total",
            starts_at=None,
            ends_at=None,
            set_by_account_id=parent.fleet.account_id,
        )
    snapshot = await reserve(sm, parent, worker)
    assert not snapshot.admitted and snapshot.reason == "channel_budget_exceeded"
    assert snapshot.worker_exposure_usd == 0


async def test_new_run_counts_unknown_holds_from_other_completed_runs(sm):
    parent, run = await setup(sm, funds="3", cap="3")
    a = await add(sm, parent, budget="2")
    funded = await reserve(sm, parent, a)
    assert funded.admitted
    async with sm() as db, db.begin():
        other = await fleet.create_run(
            db,
            tenant_id=run.tenant_id,
            orchestrator_account_id=parent.fleet.account_id,
            orchestrator_session_id=run.orchestrator_session_id,
            orchestrator_binding_id=run.orchestrator_binding_id,
            platform=run.platform,
            channel_id=run.channel_id,
            thread_id=run.thread_id,
            operation_key="other-root-operation",
            total_budget_usd=Decimal(2),
            deadline_at=run.deadline_at,
            max_workers=4,
            created_by="host",
        )
    other_parent = replace(parent, fleet=replace(parent.fleet, run_id=other.id))
    b = await add(sm, other_parent, budget=".1")
    refused = await reserve(sm, other_parent, b)
    assert not refused.admitted and refused.reason == "tenant_budget_exceeded"
    assert refused.worker_exposure_usd == 0


async def test_unknown_pricing_and_erased_actor_cannot_authorize_dispatch(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    refused = await reserve(sm, parent, worker, prices={})
    assert not refused.admitted and refused.reason == "pricing_unavailable"
    assert (await read(sm, parent, worker)).worker_exposure_usd == 0
    worker = await add(sm, parent, ordinal=1)
    assert (await reserve(sm, parent, worker)).admitted
    from daimon.core.stores import accounts

    async with sm() as db, db.begin():
        await accounts.delete_account(db, account_id=parent.fleet.account_id)
    with pytest.raises(FleetConflict, match="live tenant actor"):
        await read(sm, parent, worker)


async def test_unfunded_pool_claim_is_still_refused_by_product_money_gate(sm):
    parent, _ = await setup(sm, funds=".1", cap="3")
    worker = await add(sm, parent)
    async with sm() as db, db.begin():
        claimed = await fleet.claim_worker(
            db, tenant_id=UUID(parent.fleet.scope.tenant_id), run_id=parent.fleet.run_id, cap=3
        )
    assert claimed.id == worker.id
    refused = await reserve(sm, parent, claimed)
    assert not refused.admitted and refused.worker_exposure_usd == 0
    # A claimed pool slot carries no financial admission or provider call.


async def test_read_unavailable_logs_and_cancels_without_releasing_hold(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=100, outputs=None)
    cancelled = asyncio.Event()
    reason = await watch_fleet_budget(
        sessionmaker=sm,
        parent=parent,
        prices=tariffs(parent),
        markup=Decimal(1),
        cancelled={uuid4(): cancelled},
        finished=asyncio.Event(),
        poll_interval=0.01,
        read_timeout=3,
    )
    assert reason == "usage_unavailable" and cancelled.is_set()
    assert (await read(sm, parent, worker)).worker_exposure_usd == 1


async def test_equal_fully_reserved_run_stays_live_until_driver_finished(sm):
    parent, _ = await setup(sm, cap="1")
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    finished, cancelled = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled={worker.id: cancelled},
            finished=finished,
            poll_interval=0.01,
            read_timeout=3,
        )
    )
    await asyncio.sleep(0.05)
    assert not cancelled.is_set()
    finished.set()
    assert await asyncio.wait_for(task, 3) == "finished"
    assert (await read(sm, parent, worker)).worker_exposure_usd == 1


async def test_customer_debit_is_not_added_again_to_its_held_exposure(sm):
    parent, run = await setup(sm, provider="anthropic")
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=100_000, outputs=100)
    current = await read(sm, parent, worker)
    assert current.run.observed_usd == Decimal(".201")
    assert current.worker_exposure_usd == 1
    assert current.prepaid_available_usd == 9


async def test_preallocation_gate_refuses_unfunded_claim_then_accepts_real_reservation(sm):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, _ = await setup(sm)
    worker = await add(sm, parent)
    async with sm() as db, db.begin():
        claimed = await fleet.claim_worker(
            db, tenant_id=UUID(parent.fleet.scope.tenant_id), run_id=parent.fleet.run_id, cap=3
        )
    async with sm() as db, db.begin():
        refused = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
        )
    assert not refused.admitted
    assert (await reserve(sm, parent, claimed)).admitted
    async with sm() as db, db.begin():
        accepted = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
        )
    assert accepted.admitted and accepted.worker_exposure_usd == 1


@pytest.mark.parametrize("price_state", ["missing", "unverified", "future", "expired", "valid"])
async def test_funded_unbound_preallocation_revalidates_tariff_and_keeps_hold(sm, price_state):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    async with sm() as db, db.begin():
        claimed = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
    assert claimed is not None and claimed.binding_id is None
    key = (worker.backend, worker.model)
    prices = tariffs(parent)
    if price_state == "missing":
        prices = {}
    elif price_state == "unverified":
        prices[key] = replace(prices[key], verified=False)
    elif price_state == "expired":
        prices[key] = replace(
            prices[key],
            checked_on=datetime.now(UTC).date() - timedelta(days=1),
            valid_before=datetime.now(UTC).date(),
        )
    elif price_state == "future":
        prices[key] = replace(prices[key], checked_on=datetime.now(UTC).date() + timedelta(days=1))
    async with sm() as db, db.begin():
        snapshot = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=prices,
            markup=Decimal(1),
        )
    assert snapshot.admitted is (price_state == "valid")
    assert snapshot.reason == (None if price_state == "valid" else "pricing_unavailable")
    assert snapshot.worker_exposure_usd == snapshot.run_exposure_usd == 1
    assert (await read(sm, parent, worker)).worker_exposure_usd == 1


async def test_open_fleet_transaction_does_not_block_another_tenants_ordinary_ledger(sm):
    parent, _ = await setup(sm)
    worker = await add(sm, parent)
    async with sm() as db, db.begin():
        other = await make_tenant(db)
    async with sm() as admission, admission.begin():
        snapshot = await reserve(sm, parent, worker, session=admission)
        assert snapshot.admitted
        # The original append-only writer must commit while admission stays open.
        async with sm() as ordinary, ordinary.begin():
            await ordinary.execute(text("SET LOCAL lock_timeout = '3s'"))
            for amount, reason in ((Decimal("-.25"), "turn_debit"), (Decimal(".5"), "topup")):
                assert await tenant_ledger.insert_entry(
                    ordinary,
                    tenant_id=other.id,
                    delta_usd=amount,
                    reason=reason,
                    idempotency_key=f"ordinary:{reason}:{other.id}",
                )
        async with sm() as observer:
            assert await tenant_ledger.get_balance(observer, tenant_id=other.id) == Decimal(".25")


async def test_same_tenant_ordinary_debit_serializes_before_fleet_funds_check(sm):
    parent, run = await setup(sm, funds="1", cap="1")
    worker = await add(sm, parent)
    started = asyncio.Event()
    admission_pid = None

    async def compete():
        nonlocal admission_pid
        async with sm() as db, db.begin():
            admission_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            return await reserve(sm, parent, worker, session=db)

    async with sm() as ordinary:
        await ordinary.begin()
        ordinary_pid = await ordinary.scalar(text("SELECT pg_backend_pid()"))
        await tenant_ledger.insert_entry(
            ordinary,
            tenant_id=run.tenant_id,
            delta_usd=Decimal("-.75"),
            reason="turn_debit",
            idempotency_key=f"ordinary:{run.tenant_id}",
        )
        task = asyncio.create_task(compete())
        try:
            await asyncio.wait_for(started.wait(), 3)
            async with asyncio.timeout(3):
                while ordinary_pid not in await ordinary.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": admission_pid}
                ):
                    assert not task.done()
                    await asyncio.sleep(0.01)
            await ordinary.commit()
            snapshot = await asyncio.wait_for(task, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not snapshot.admitted and snapshot.reason == "tenant_budget_exceeded"
    assert snapshot.worker_exposure_usd == 0
    assert snapshot.prepaid_available_usd == Decimal(".25")


async def test_flag_off_financial_admission_does_not_touch_database():
    parent = root()
    disabled = replace(parent, fleet=replace(parent.fleet, enabled=False))

    class NoDB:
        def __getattr__(self, name):
            raise AssertionError("flag-off admission must not read or allocate")

    with pytest.raises(FleetConflict, match="root authorization"):
        await admit_fleet_worker(
            NoDB(),
            parent=disabled,
            policy=POLICY,
            resolved=resolve_worker_spec(parent, parent.spec, policy=POLICY, now=datetime.now(UTC)),
            ordinal=0,
            role="worker",
            prices=tariffs(parent),
            markup=Decimal(1),
        )


@pytest.mark.parametrize("fault", ["foreign_account", "foreign_run", "root_binding", "no_audit"])
async def test_root_scope_or_missing_actor_audit_blocks_allocation(sm, fault):
    parent, _ = await setup(sm)
    worker = await add(sm, parent)
    if fault == "foreign_account":
        parent = replace(parent, fleet=replace(parent.fleet, account_id=uuid4()))
    elif fault == "foreign_run":
        parent = replace(parent, fleet=replace(parent.fleet, run_id=uuid4()))
    elif fault == "root_binding":
        parent = replace(
            parent,
            fleet=replace(
                parent.fleet, root=parent.fleet.root.model_copy(update={"thread_id": "foreign"})
            ),
        )
    else:
        parent = replace(parent, agent_id=None)
    with pytest.raises((FleetConflict, ValueError)):
        await reserve(sm, parent, worker)


async def test_monetary_producer_retains_floor_even_when_a_sibling_is_unpriced(sm):
    parent, run = await setup(sm, provider="anthropic", cap="1")
    worker = await add(sm, parent, budget=".5")
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=1_000_000, outputs=1)
    current = await read(sm, parent, worker, prices={})
    assert current.worker_exposure_usd >= Decimal(2)
    assert not current.admitted


async def test_old_native_attempt_cannot_reopen_financial_admission(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    funded = await reserve(sm, parent, worker)
    async with sm() as db, db.begin():
        claimed = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
        await fleet.requeue(db, tenant_id=run.tenant_id, worker=claimed, max_attempts=2)
    async with sm() as db, db.begin():
        _, workers = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
    refused = await reserve(sm, parent, workers[0])
    assert not refused.admitted and refused.reason == "accepted_attempt_inventory_unavailable"
    assert refused.worker_exposure_usd == funded.worker_exposure_usd == 1


async def test_never_claimed_cancel_releases_zero_spend_and_next_run_can_use_funds(sm):
    parent, run = await setup(sm, funds="3", cap="3")
    worker = await add(sm, parent, budget="2")
    funded = await reserve(sm, parent, worker)
    async with sm() as db, db.begin():
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=funded.worker.version,
            status="cancelled",
            error_code="cancelled_before_claim",
        )
        await fleet.set_run_status(
            db,
            tenant_id=run.tenant_id,
            run_id=run.id,
            expected_version=funded.run.version,
            status="cancelled",
        )
    other_parent, other = await next_run(sm, parent, run)
    other_worker = await add(sm, other_parent, budget="2")
    accepted = await reserve(sm, other_parent, other_worker)
    assert accepted.admitted and accepted.prepaid_available_usd == 1
    released = await read(sm, parent, worker)
    assert released.worker_exposure_usd == released.run_exposure_usd == 0
    assert released.reason == "terminal"
    async with sm() as db, db.begin():
        graph, stamps = await fleet.lock_budget(
            db, tenant_id=run.tenant_id, actor_id=parent.fleet.account_id, run_id=other.id
        )
        assert run.id not in {item.id for item, _ in graph}
        assert worker.id not in stamps
        ledger = await tenant_ledger.list_for_tenant(db, tenant_id=run.tenant_id)
    assert len(ledger) == 1 and ledger[0].delta_usd == 3
    cancelled, finished = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled={worker.id: cancelled},
            finished=finished,
            read_timeout=3,
        )
    )
    try:
        await asyncio.wait_for(cancelled.wait(), 3)
        assert not task.done()
        finished.set()
        assert await asyncio.wait_for(task, 3) == "finished"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("evidence", ["claim", "requeue", "zero_revision"])
async def test_ambiguous_cancel_without_bound_binding_keeps_its_whole_hold(sm, evidence):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    funded = await reserve(sm, parent, worker)
    current = funded.worker
    if evidence in ("claim", "requeue"):
        async with sm() as db, db.begin():
            current = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
            if evidence == "requeue":
                current = await fleet.requeue(
                    db, tenant_id=run.tenant_id, worker=current, max_attempts=2
                )
    else:
        await usage(sm, parent, run, worker, inputs=0, outputs=0)
    assert current.binding_id is None
    async with sm() as db, db.begin():
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=current.version,
            status="cancelled",
            error_code="ambiguous_allocation",
        )
    after = await read(sm, parent, worker)
    assert after.reason == "terminal" and after.worker_exposure_usd == 1
    assert after.prepaid_available_usd == 9


async def test_another_runs_verified_overrun_does_not_latch_current_run(sm):
    parent, run = await setup(sm, provider="anthropic", funds="10", cap="1")
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=1_000_000, outputs=1)
    overrun = await read(sm, parent, worker)
    assert overrun.reason == "run_budget_exceeded"
    other_parent, _ = await next_run(sm, parent, run, cap="1")
    other_worker = await add(sm, other_parent, budget=".1")
    current = await reserve(sm, other_parent, other_worker)
    assert current.admitted and current.reason is None
    assert current.prepaid_available_usd == Decimal("7.899990")
    assert (await read(sm, parent, worker)).reason == "run_budget_exceeded"


async def test_financially_refused_spawn_is_terminal_and_cannot_claim_pool_slot(sm):
    parent, run = await setup(sm, funds=".1", cap="3")
    worker = await add(sm, parent)
    refused = await reserve(sm, parent, worker)
    assert not refused.admitted and refused.worker.status == "cancelled"
    async with sm() as db, db.begin():
        assert await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3) is None


async def test_terminal_fully_debited_history_is_not_in_the_next_budget_graph(sm):
    parent, run = await setup(sm, provider="anthropic", funds="3", cap="2")
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    await usage(sm, parent, run, worker, inputs=500_000, outputs=0)
    spent = await read(sm, parent, worker)
    assert spent.worker_exposure_usd == spent.run.observed_usd == 1
    async with sm() as db, db.begin():
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=worker.id,
            expected_version=spent.worker.version,
            status="cancelled",
            error_code="terminal",
        )
        await fleet.set_run_status(
            db,
            tenant_id=run.tenant_id,
            run_id=run.id,
            expected_version=spent.run.version,
            status="cancelled",
        )
    other_parent, other = await next_run(sm, parent, run, cap="1")
    other_worker = await add(sm, other_parent)
    accepted = await reserve(sm, other_parent, other_worker)
    assert accepted.admitted and accepted.prepaid_available_usd == 1
    async with sm() as db, db.begin():
        graph, _ = await fleet.lock_budget(
            db, tenant_id=run.tenant_id, actor_id=parent.fleet.account_id, run_id=other.id
        )
        assert run.id not in {item.id for item, _ in graph}


async def test_store_cannot_lower_a_hold_without_the_producers_zero_spend_evidence(sm):
    parent, run = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    async with sm() as db, db.begin():
        graph, _ = await fleet.lock_budget(
            db, tenant_id=run.tenant_id, actor_id=parent.fleet.account_id, run_id=run.id
        )
        with pytest.raises(FleetConflict, match="cannot be released"):
            await fleet.save_budget(
                db,
                tenant_id=run.tenant_id,
                graph=graph,
                exposures={worker.id: Decimal(0)},
                debits={run.id: Decimal(0)},
            )
    assert (await read(sm, parent, worker)).worker_exposure_usd == 1


async def test_unrelated_open_ordinary_write_does_not_cancel_actual_budget_watcher(sm, monkeypatch):
    from daimon.core import channel_budget

    parent, _ = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    async with sm() as db, db.begin():
        other = await make_tenant(db)
    finished, cancelled, polled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = channel_budget.produce_fleet_budget

    async def observed_read(*args, **kwargs):
        value = await original(*args, **kwargs)
        assert value[worker.id].admitted and value[worker.id].worker_exposure_usd == 1
        polled.set()
        return value

    monkeypatch.setattr(channel_budget, "produce_fleet_budget", observed_read)
    async with sm() as ordinary, ordinary.begin():
        await tenant_ledger.insert_entry(
            ordinary,
            tenant_id=other.id,
            delta_usd=Decimal(1),
            reason="topup",
            idempotency_key=f"ordinary:{other.id}",
        )
        task = asyncio.create_task(
            watch_fleet_budget(
                sessionmaker=sm,
                parent=parent,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled={worker.id: cancelled},
                finished=finished,
                read_timeout=3,
            )
        )
        try:
            await asyncio.wait_for(polled.wait(), 3)
            finished.set()
            assert await asyncio.wait_for(task, 3) == "finished"
            assert not cancelled.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_atomic_spawn_reservation_and_audit_roll_back_together(sm):
    from daimon.core.channel_budget import admit_fleet_worker

    parent, run = await setup(sm)
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        limits=parent.limits.model_copy(update={"budget_usd": Decimal(1)}),
    )
    with pytest.raises(RuntimeError, match="crash"):
        async with sm() as db, db.begin():
            admitted = await admit_fleet_worker(
                db,
                parent=parent,
                policy=POLICY,
                resolved=resolved,
                ordinal=0,
                role="worker",
                prices=tariffs(parent),
                markup=Decimal(1),
            )
            assert admitted.admitted
            raise RuntimeError("crash before commit")
    async with sm() as db, db.begin():
        _, workers = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
        assert workers == ()
        assert await security_audit.list_events(db, tenant_id=run.tenant_id) == []
        admitted = await admit_fleet_worker(
            db,
            parent=parent,
            policy=POLICY,
            resolved=resolved,
            ordinal=0,
            role="worker",
            prices=tariffs(parent),
            markup=Decimal(1),
        )
    assert admitted.admitted and admitted.worker_exposure_usd == 1


async def test_one_run_watch_fans_out_floors_without_cancelling_a_funded_sibling(sm, monkeypatch):
    from daimon.core import channel_budget

    parent, run = await setup(sm)
    a = await add(sm, parent, budget=".01")
    b = await add(sm, parent, ordinal=1, budget="1")
    assert (await reserve(sm, parent, a)).admitted
    assert (await reserve(sm, parent, b)).admitted
    await usage(sm, parent, run, a, inputs=5_000, outputs=None)
    await usage(sm, parent, run, b, inputs=100, outputs=None)
    cancelled = {a.id: asyncio.Event(), b.id: asyncio.Event()}
    finished, polled = asyncio.Event(), asyncio.Event()
    original = channel_budget.produce_fleet_budget
    passes = 0

    async def observed(*args, **kwargs):
        nonlocal passes
        result = await original(*args, **kwargs)
        passes += 1
        assert set(result) == {a.id, b.id}
        assert result[a.id].worker_floor_usd == Decimal(".01")
        assert result[b.id].admitted
        polled.set()
        return result

    monkeypatch.setattr(channel_budget, "produce_fleet_budget", observed)
    task = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled=cancelled,
            finished=finished,
        )
    )
    try:
        await asyncio.wait_for(polled.wait(), 2)
        await asyncio.wait_for(cancelled[a.id].wait(), 2)
        assert not cancelled[b.id].is_set() and not task.done()
        assert passes == 1
        finished.set()
        assert await asyncio.wait_for(task, 2) == "finished"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (await read(sm, parent, a)).worker_exposure_usd == Decimal(".01")
    assert (await read(sm, parent, b)).worker_exposure_usd == 1


@pytest.mark.parametrize("operation", ["refresh", "reserve", "preallocation"])
async def test_passive_refresh_allows_same_tenant_writes_but_financial_admission_fences(
    sm, monkeypatch, operation
):
    from daimon.core.channel_budget import require_fleet_worker_admission
    from sqlalchemy.exc import DBAPIError

    parent, run = await setup(sm)
    worker = await add(sm, parent)
    assert (await reserve(sm, parent, worker)).admitted
    if operation == "preallocation":
        async with sm.begin() as db:
            claimed = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
            assert claimed is not None and claimed.id == worker.id
    locked, release = asyncio.Event(), asyncio.Event()
    original = tenant_ledger.lock_funds

    async def paused_lock(session, **kwargs):
        value = await original(session, **kwargs)
        assert kwargs["financial"] is (operation != "refresh")
        locked.set()
        await release.wait()
        return value

    monkeypatch.setattr(tenant_ledger, "lock_funds", paused_lock)

    async def refresh_or_admit():
        async with sm.begin() as db:
            if operation == "refresh":
                return (
                    await produce_fleet_budget(
                        db,
                        parent=parent,
                        prices=tariffs(parent),
                        markup=Decimal(1),
                    )
                )[worker.id]
            if operation == "reserve":
                return await reserve(sm, parent, worker, session=db)
            return await require_fleet_worker_admission(
                db,
                parent=parent,
                policy=POLICY,
                worker_id=worker.id,
                prices=tariffs(parent),
                markup=Decimal(1),
            )

    async def ordinary_write():
        async with sm.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout = '100ms'"))
            await tenant_ledger.insert_entry(
                db,
                tenant_id=run.tenant_id,
                delta_usd=Decimal("-.5"),
                reason="turn_debit",
                idempotency_key=f"concurrent:{uuid4()}",
            )

    task = asyncio.create_task(refresh_or_admit())
    try:
        await asyncio.wait_for(locked.wait(), 2)
        if operation == "refresh":
            await asyncio.wait_for(ordinary_write(), 2)
        else:
            with pytest.raises(DBAPIError) as error:
                await asyncio.wait_for(ordinary_write(), 2)
            assert error.value.orig.sqlstate == "55P03"
        release.set()
        current = await asyncio.wait_for(task, 3)
        assert current.admitted
        assert current.prepaid_available_usd == (Decimal("8.5") if operation == "refresh" else 9)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cohort_end", ["cancelled", "failed", "event_already_set"])
async def test_run_watch_survives_ended_cohort_and_cancels_a_later_workers_usage(
    sm, monkeypatch, cohort_end
):
    from daimon.core import channel_budget

    parent, run = await setup(sm, cap="3")
    first = await add(sm, parent)
    assert (await reserve(sm, parent, first)).admitted
    async with sm.begin() as db:
        claimed = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
        assert claimed is not None and claimed.id == first.id
        if cohort_end != "event_already_set":
            await fleet.finish(
                db,
                tenant_id=run.tenant_id,
                worker_id=first.id,
                expected_version=claimed.version,
                status=cohort_end,
                error_code="cohort_ended",
            )
    cancelled = {first.id: asyncio.Event()}
    if cohort_end == "event_already_set":
        cancelled[first.id].set()
    finished, refreshed_ended_cohort = asyncio.Event(), asyncio.Event()
    original = channel_budget.produce_fleet_budget
    passes = 0

    async def observed(*args, **kwargs):
        nonlocal passes
        result = await original(*args, **kwargs)
        passes += 1
        if passes >= 2 and cancelled[first.id].is_set():
            refreshed_ended_cohort.set()
        return result

    monkeypatch.setattr(channel_budget, "produce_fleet_budget", observed)
    task = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled=cancelled,
            finished=finished,
            poll_interval=0.01,
            read_timeout=3,
        )
    )
    try:
        await asyncio.wait_for(cancelled[first.id].wait(), 3)
        assert not task.done()
        await asyncio.wait_for(refreshed_ended_cohort.wait(), 3)
        assert not task.done()
        async with sm.begin() as db:
            current_run, _ = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
        assert current_run.status == "running"

        later = await add(sm, parent, ordinal=1)
        assert (await reserve(sm, parent, later)).admitted
        cancelled[later.id] = asyncio.Event()
        await usage(sm, parent, run, later, inputs=600_000, outputs=None)
        await asyncio.wait_for(cancelled[later.id].wait(), 3)
        assert not task.done()
        current = await read(sm, parent, later)
        assert current.worker_floor_usd == current.worker_exposure_usd == Decimal("1.2")
        assert current.reason == "worker_budget_exceeded"
        assert current.run.status == "running"
        finished.set()
        assert await asyncio.wait_for(task, 0.5) == "finished"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_run_watch_timeout_still_cancels_every_registered_worker_and_retains_holds(
    sm, monkeypatch
):
    from daimon.core import channel_budget as _cb

    # The fail-closed limits themselves, without the 30 s read-failure window.
    monkeypatch.setattr(_cb, "BUDGET_READ_FAILURE_WINDOW_S", 0.0)
    from daimon.core import channel_budget

    parent, _ = await setup(sm)
    a = await add(sm, parent)
    b = await add(sm, parent, ordinal=1)
    assert (await reserve(sm, parent, a)).admitted
    assert (await reserve(sm, parent, b)).admitted
    cancelled = {a.id: asyncio.Event(), b.id: asyncio.Event()}
    cancelled[a.id].set()
    original = channel_budget.produce_fleet_budget
    blocked = asyncio.Event()

    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        await blocked.wait()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(channel_budget, "produce_fleet_budget", delayed)
        result = await asyncio.wait_for(
            watch_fleet_budget(
                sessionmaker=sm,
                parent=parent,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled=cancelled,
                finished=asyncio.Event(),
                poll_interval=0.01,
                read_timeout=0.1,
            ),
            3,
        )
    assert result == "usage_unavailable"
    assert all(event.is_set() for event in cancelled.values())
    assert (await read(sm, parent, a)).worker_exposure_usd == 1
    assert (await read(sm, parent, b)).worker_exposure_usd == 1


async def bound_worker(sm, parent, run, *, funded=True, running=True, session_ref=True):
    worker = await add(sm, parent)
    if funded:
        assert (await reserve(sm, parent, worker)).admitted
    binding = ProviderBinding(
        id=f"worker-{worker.id}",
        thread=worker_slot(run, worker).thread,
        legacy_account_id=str(parent.fleet.account_id),
        provider=worker.backend,
        profile=worker.profile,
        generation=1,
        config_revision=0,
        native_refs={"session": f"native-{worker.id}", "agent": f"agent-{worker.id}"}
        if session_ref
        else {},
    )
    async with sm.begin() as db:
        claimed = await fleet.claim_worker(db, tenant_id=run.tenant_id, run_id=run.id, cap=3)
        assert claimed is not None and claimed.id == worker.id
        await mux_state.put_binding(db, binding, expected_generation=0)
        if running:
            await fleet.mark_running(
                db,
                tenant_id=run.tenant_id,
                worker_id=worker.id,
                expected_version=claimed.version,
                binding_id=binding.id,
            )
    return worker, binding


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_bound_input_guard_accepts_current_funded_identity_without_extra_money(sm, provider):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm, provider=provider)
    worker, binding = await bound_worker(sm, parent, run)
    snapshots = []
    for _ in range(2):
        async with sm.begin() as db:
            snapshots.append(
                await require_fleet_worker_admission(
                    db,
                    parent=parent,
                    policy=POLICY,
                    worker_id=worker.id,
                    prices=tariffs(parent),
                    markup=Decimal(1),
                    binding=binding,
                )
            )
    assert all(s.admitted and s.worker.status == "running" for s in snapshots)
    assert all(s.worker_exposure_usd == s.run_exposure_usd == 1 for s in snapshots)
    assert all(s.worker_floor_usd == 0 and s.prepaid_available_usd == 9 for s in snapshots)
    async with sm() as db:
        assert len(await security_audit.list_events(db, tenant_id=run.tenant_id)) == 1
        ledger = await tenant_ledger.list_for_tenant(db, tenant_id=run.tenant_id)
    assert [(r.reason, r.delta_usd) for r in ledger] == [("trial_credit", Decimal(10))]


@pytest.mark.parametrize(
    "fault",
    [
        "id",
        "generation",
        "config",
        "session",
        "agent",
        "empty_session",
        "thread",
        "tenant",
        "account",
        "provider",
        "profile",
    ],
)
async def test_bound_input_guard_refuses_changed_caller_binding_and_keeps_hold(sm, fault):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker, binding = await bound_worker(sm, parent, run)
    changes = {
        "id": {"id": "foreign"},
        "generation": {"generation": 0},
        "config": {"config_revision": 1},
        "session": {"native_refs": {**binding.native_refs, "session": "other"}},
        "agent": {"native_refs": {**binding.native_refs, "agent": "other"}},
        "empty_session": {"native_refs": {}},
        "thread": {"thread": binding.thread.model_copy(update={"thread_id": "other"})},
        "tenant": {
            "thread": binding.thread.model_copy(
                update={
                    "channel": binding.thread.channel.model_copy(update={"tenant_id": str(uuid4())})
                }
            )
        },
        "account": {"legacy_account_id": str(uuid4())},
        "provider": {"provider": "openai"},
        "profile": {"profile": "foreign"},
    }
    async with sm.begin() as db:
        refused = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
            binding=binding.model_copy(update=changes[fault]),
        )
    assert not refused.admitted and refused.reason == "not_current_worker_binding"
    assert refused.worker_exposure_usd == 1


@pytest.mark.parametrize("fault", ["unfunded", "starting", "no_session", "no_binding_argument"])
async def test_bound_input_guard_requires_funding_running_row_and_native_session(sm, fault):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker, binding = await bound_worker(
        sm,
        parent,
        run,
        funded=fault != "unfunded",
        running=fault != "starting",
        session_ref=fault != "no_session",
    )
    async with sm.begin() as db:
        refused = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
            binding=None if fault == "no_binding_argument" else binding,
        )
    assert not refused.admitted
    assert refused.worker_exposure_usd == (0 if fault == "unfunded" else 1)


@pytest.mark.parametrize("price_state", ["missing", "unverified", "future", "expired", "valid"])
async def test_bound_input_guard_rechecks_tariff_and_retains_unknown_bound(sm, price_state):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker, binding = await bound_worker(sm, parent, run)
    prices = tariffs(parent)
    key = (worker.backend, worker.model)
    if price_state == "missing":
        prices = {}
    elif price_state == "unverified":
        prices[key] = replace(prices[key], verified=False)
    elif price_state == "expired":
        prices[key] = replace(
            prices[key],
            checked_on=datetime.now(UTC).date() - timedelta(days=1),
            valid_before=datetime.now(UTC).date(),
        )
    elif price_state == "future":
        prices[key] = replace(prices[key], checked_on=datetime.now(UTC).date() + timedelta(days=1))
    async with sm.begin() as db:
        current = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=prices,
            markup=Decimal(1),
            binding=binding,
        )
    assert current.admitted is (price_state == "valid")
    assert current.reason == (None if price_state == "valid" else "pricing_unavailable")
    assert current.worker_exposure_usd == 1


async def test_bound_input_guard_rejects_actual_binding_successor_with_stale_snapshot(sm):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker, binding = await bound_worker(sm, parent, run)
    successor = ProviderBinding.model_validate(
        {
            **binding.model_dump(mode="json"),
            "generation": 2,
            "native_refs": {**binding.native_refs, "session": "replacement-session"},
        }
    )
    async with sm.begin() as db:
        await mux_state.put_binding(db, successor, expected_generation=1)
    async with sm.begin() as db:
        refused = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
            binding=binding,
        )
    assert not refused.admitted and refused.reason == "not_current_worker_binding"
    assert refused.worker_exposure_usd == 1


async def test_bound_input_guard_persists_real_usage_overrun_before_refusing_input(sm):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker, binding = await bound_worker(sm, parent, run)
    await usage(sm, parent, run, worker, inputs=600_000, outputs=None, revision=2)
    async with sm.begin() as db:
        refused = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
            binding=binding,
        )
    assert not refused.admitted and refused.reason == "worker_budget_exceeded"
    assert refused.worker_exposure_usd == refused.worker_floor_usd == Decimal("1.2")
    assert (await read(sm, parent, worker)).worker_exposure_usd == Decimal("1.2")


async def test_bound_input_guard_rechecks_ordinary_debit_against_funded_snapshot(sm):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm, funds="3")
    worker, binding = await bound_worker(sm, parent, run)
    async with sm.begin() as db:
        await tenant_ledger.insert_entry(
            db,
            tenant_id=run.tenant_id,
            delta_usd=Decimal("-2.5"),
            reason="turn_debit",
            idempotency_key=f"ordinary:{uuid4()}",
        )
    async with sm.begin() as db:
        refused = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
            binding=binding,
        )
    assert not refused.admitted and refused.reason == "tenant_budget_exceeded"
    assert refused.prepaid_available_usd == Decimal("-.5")
    assert refused.worker_exposure_usd == 1


@pytest.mark.parametrize("turns,attempts,allowed", [(1, 0, True), (2, 0, False), (1, 1, False)])
async def test_financial_recapture_accepts_counted_first_input_without_recount_or_release(
    sm, turns, attempts, allowed
):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker, binding = await bound_worker(sm, parent, run)
    # Controlled persisted facts. The once-only count transition is store-owned.
    async with sm.begin() as db:
        await db.execute(
            text(
                "UPDATE fleet_worker SET turns_used=:turns, attempts=:attempts, version=version+1 "
                "WHERE id=:worker_id AND tenant_id=:tenant_id"
            ),
            {
                "turns": turns,
                "attempts": attempts,
                "worker_id": worker.id,
                "tenant_id": run.tenant_id,
            },
        )
    for _ in range(2):
        async with sm.begin() as db:
            current = await require_fleet_worker_admission(
                db,
                parent=parent,
                policy=POLICY,
                worker_id=worker.id,
                prices=tariffs(parent),
                markup=Decimal(1),
                binding=binding,
            )
        assert current.admitted is allowed
        assert current.worker.turns_used == turns and current.worker.attempts == attempts
        assert current.worker_exposure_usd == 1
        assert current.reason == (None if allowed else "accepted_attempt_inventory_unavailable")
    async with sm() as db:
        assert len(await security_audit.list_events(db, tenant_id=run.tenant_id)) == 1
        assert len(await tenant_ledger.list_for_tenant(db, tenant_id=run.tenant_id)) == 1


async def test_financial_recapture_refuses_used_unallocated_claim(sm):
    from daimon.core.channel_budget import require_fleet_worker_admission

    parent, run = await setup(sm)
    worker, _ = await bound_worker(sm, parent, run, running=False)
    async with sm.begin() as db:
        await db.execute(
            text(
                "UPDATE fleet_worker SET turns_used=1, version=version+1 "
                "WHERE id=:worker_id AND tenant_id=:tenant_id"
            ),
            {"worker_id": worker.id, "tenant_id": run.tenant_id},
        )
    async with sm.begin() as db:
        refused = await require_fleet_worker_admission(
            db,
            parent=parent,
            policy=POLICY,
            worker_id=worker.id,
            prices=tariffs(parent),
            markup=Decimal(1),
        )
    assert not refused.admitted
    assert refused.reason == "accepted_attempt_inventory_unavailable"
    assert refused.worker_exposure_usd == 1


async def test_run_watch_keeps_counted_first_input_live_until_owner_finished(sm, monkeypatch):
    from daimon.core import channel_budget

    parent = root()
    parent = replace(parent, limits=parent.limits.model_copy(update={"max_turns": 1}))
    parent, run = await setup(sm, parent=parent)
    worker, _ = await bound_worker(sm, parent, run)
    async with sm.begin() as db:
        await db.execute(
            text(
                "UPDATE fleet_worker SET turns_used=1, version=version+1 "
                "WHERE id=:worker_id AND tenant_id=:tenant_id"
            ),
            {"worker_id": worker.id, "tenant_id": run.tenant_id},
        )
    finished, cancelled, polled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = channel_budget.produce_fleet_budget
    passes = 0

    async def observed(*args, **kwargs):
        nonlocal passes
        result = await original(*args, **kwargs)
        assert result[worker.id].admitted
        passes += 1
        if passes >= 2:
            polled.set()
        return result

    monkeypatch.setattr(channel_budget, "produce_fleet_budget", observed)
    task = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled={worker.id: cancelled},
            finished=finished,
            poll_interval=0.01,
            read_timeout=3,
        )
    )
    try:
        await asyncio.wait_for(polled.wait(), 3)
        assert not cancelled.is_set() and not task.done()
        finished.set()
        assert await asyncio.wait_for(task, 3) == "finished"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# The watch wraps each whole poll (checkout, SET LOCAL, lock wait, read) in its read timeout,
# and its lock wait is 75% of it, so 25% is the headroom for checkout and round trips.
# BUDGET_READ_TIMEOUT_LIMIT polls over it in a row fail closed. A loaded CI runner went over
# 25ms (at 0.1s) and 100ms (at 0.4s); 1s leaves 250ms. The lock tests measure the busy
# limit on a virtual clock; in the back-to-back test the watch queues behind 30ms holds,
# so it gets the lock well inside its 0.75s wait and the 1s busy limit is never reached.
READ_TIMEOUT_S = 1.0


async def test_run_watch_retries_one_slow_read_without_cancelling_workers(sm, monkeypatch):
    # Staging r13/r14 (2026-10-18): one budget read exceeded the 2s timeout while
    # sibling admissions held the run lock, and every registered worker was cancelled.
    from daimon.core import channel_budget

    parent, _ = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    cancelled = {a.id: asyncio.Event()}
    finished = asyncio.Event()
    original = channel_budget.produce_fleet_budget
    calls = 0

    async def slow_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(3600)  # a read that never returns; only the read timeout ends it
        result = await original(*args, **kwargs)
        if calls >= 3:
            finished.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(channel_budget, "produce_fleet_budget", slow_once)
        await asyncio.wait_for(
            watch_fleet_budget(
                sessionmaker=sm,
                parent=parent,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled=cancelled,
                finished=finished,
                poll_interval=0.01,
                read_timeout=READ_TIMEOUT_S,
            ),
            10,
        )
    assert calls >= 3 and not cancelled[a.id].is_set()
    assert (await read(sm, parent, a)).worker_exposure_usd == 1


class _VirtualClock:
    """A clock only the test advances, so the busy-window limit never races the wall clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _VirtualTimeLoop:
    """The running loop with ``time()`` read from a ``_VirtualClock``; all else is delegated."""

    def __init__(self, loop, clock: _VirtualClock) -> None:
        self._loop, self._clock = loop, clock

    def time(self) -> float:
        return self._clock.now

    def __getattr__(self, name):
        return getattr(self._loop, name)


class _VirtualTimeAsyncio:
    """Stands in for ``asyncio`` inside ``channel_budget`` so the watch reads virtual time.

    Only ``get_running_loop().time()`` is virtual. Timeouts, sleeps, events and the
    database's own ``lock_timeout`` stay real, so a poll still waits for the lock.
    """

    def __init__(self, clock: _VirtualClock) -> None:
        self._clock = clock

    def get_running_loop(self) -> _VirtualTimeLoop:
        return _VirtualTimeLoop(asyncio.get_running_loop(), self._clock)

    def __getattr__(self, name):
        return getattr(asyncio, name)


class _WatchProbe:
    """Counts the watch's polls so a test waits for events instead of sleeping.

    ``polls`` rises when a poll opens its transaction, so ``polls >= n + 1`` means poll
    ``n`` has finished. ``reads`` rises when a poll got the lock and read the budget.
    """

    def __init__(self, sm, monkeypatch, clock: _VirtualClock) -> None:
        from daimon.core import channel_budget

        self.polls = 0
        self.reads = 0
        self.watch: asyncio.Task | None = None
        original = channel_budget.produce_fleet_budget

        async def counted(*args, **kwargs):
            self.reads += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(channel_budget, "produce_fleet_budget", counted)
        monkeypatch.setattr(channel_budget, "asyncio", _VirtualTimeAsyncio(clock))
        self.sessionmaker = _CountingSessions(sm, self)

    def watching(self, watch: asyncio.Task) -> asyncio.Task:
        """Register the watch task so a wait ends the moment the watch does."""
        self.watch = watch
        return watch

    async def until(self, condition, what: str, *, within: float = 30.0) -> None:
        """Wait for ``condition``; fail naming ``what`` if the watch ends first or time runs out."""
        async with asyncio.timeout(within):
            while not condition():
                if self.watch is not None and self.watch.done():
                    outcome = (
                        repr(self.watch.exception())
                        if not self.watch.cancelled() and self.watch.exception()
                        else f"result {self.watch.result()!r}"
                        if not self.watch.cancelled()
                        else "cancelled"
                    )
                    raise AssertionError(
                        f"the watch ended before {what} (polls={self.polls}, "
                        f"reads={self.reads}): {outcome}"
                    )
                await asyncio.sleep(0.005)

    async def polls_since(self, start: int, count: int) -> None:
        """Wait until ``count`` polls have finished after the poll counter read ``start``."""
        await self.until(
            lambda: self.polls >= start + count + 1,
            f"{count} busy poll(s) finished after poll {start}",
        )


class _CountingSessions:
    """The sessionmaker surface the watch uses, counting each poll's transaction."""

    def __init__(self, sm, probe: _WatchProbe) -> None:
        self._sm, self._probe = sm, probe

    def begin(self):
        self._probe.polls += 1
        return self._sm.begin()


async def _hold_tenant_lock(sm, parent, holding, release):
    from sqlalchemy import text

    async with sm.begin() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"fleet:{parent.fleet.scope.tenant_id}"},
        )
        holding.set()
        await release.wait()


@asynccontextmanager
async def _tenant_lock_held(sm, parent):
    """Hold the tenant fleet lock, like admissions mid-dispatch, and always release it.

    A holder left running would keep one of the shared engine's two pooled connections
    and starve the next test.
    """
    holding, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(_hold_tenant_lock(sm, parent, holding, release))
    try:
        await asyncio.wait_for(holding.wait(), 10)
        yield
    finally:
        release.set()
        await holder


async def test_run_watch_skips_while_admissions_hold_the_tenant_lock(sm, monkeypatch):
    # Staging r19 (2026-10-18): budget reads queued behind admissions holding the
    # tenant fleet lock, timed out, and every in-flight dispatch was cancelled.
    # The old blocking read would fail closed after BUDGET_READ_TIMEOUT_LIMIT consecutive
    # slow reads, so the first hold outlasts more polls than that. The busy limit is
    # measured on a virtual clock, so each hold stays under it exactly.
    from daimon.core import channel_budget

    parent, _ = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    cancelled = {a.id: asyncio.Event()}
    finished = asyncio.Event()
    clock = _VirtualClock()
    probe = _WatchProbe(sm, monkeypatch, clock)
    limit = 1.0
    monkeypatch.setattr(channel_budget, "BUDGET_LOCK_BUSY_LIMIT_S", limit)
    busy_polls = channel_budget.BUDGET_READ_TIMEOUT_LIMIT + 1
    watch = probe.watching(
        asyncio.create_task(
            watch_fleet_budget(
                sessionmaker=probe.sessionmaker,
                parent=parent,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled=cancelled,
                finished=finished,
                poll_interval=0.01,
                read_timeout=READ_TIMEOUT_S,
                # Every poll takes the lock, so each busy poll is a contended one.
                locked_refresh_s=0,
            )
        )
    )
    try:
        # Hold, release, hold again: the busy window resets on success. Each hold lasts 0.9 of
        # the limit, so the second one only stays under it if the first window was reset.
        for polls_before_advance in (busy_polls, 1):
            async with _tenant_lock_held(sm, parent):
                await probe.polls_since(probe.polls, polls_before_advance)
                assert not cancelled[a.id].is_set() and not watch.done()
                clock.advance(0.9 * limit)
                await probe.polls_since(probe.polls, 1)
                assert not cancelled[a.id].is_set() and not watch.done()
            reads = probe.reads
            await probe.until(
                lambda reads=reads: probe.reads > reads, "a successful read after release"
            )
        finished.set()
        assert await asyncio.wait_for(watch, 5) == "finished"
        assert not cancelled[a.id].is_set()
    finally:
        finished.set()
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
    assert (await read(sm, parent, a)).worker_exposure_usd == 1


async def test_an_overrun_between_locked_refreshes_is_cancelled_from_the_snapshot(sm, monkeypatch):
    """With the locked refresh every BUDGET_LOCKED_REFRESH_S, an overrun in between is
    still cancelled at poll speed, from the lock-free snapshot."""
    parent, run = await setup(sm)
    worker = await add(sm, parent, budget=".01")
    assert (await reserve(sm, parent, worker)).admitted
    cancelled = {worker.id: asyncio.Event()}
    finished = asyncio.Event()
    probe = _WatchProbe(sm, monkeypatch, _VirtualClock())
    watch = probe.watching(
        asyncio.create_task(
            watch_fleet_budget(
                sessionmaker=probe.sessionmaker,
                parent=parent,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled=cancelled,
                finished=finished,
                poll_interval=0.01,
                read_timeout=READ_TIMEOUT_S,
            )
        )
    )
    try:
        await probe.until(lambda: probe.reads >= 1, "the first locked refresh")
        reads = probe.reads
        await usage(sm, parent, run, worker, inputs=10_000, outputs=None)
        await asyncio.wait_for(cancelled[worker.id].wait(), 5)
        # The virtual clock never reached the next locked refresh: the snapshot decided.
        assert probe.reads == reads
    finally:
        finished.set()
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)


async def test_run_watch_takes_the_tenant_lock_only_every_locked_refresh(sm, monkeypatch):
    """SMOKE J4: each run's watch took the tenant fleet lock four times a second and held it
    for its whole producer read, so the tenant's admissions kept waiting on it. Between
    locked refreshes the watch decides from the lock-free snapshot."""
    from daimon.core import channel_budget

    parent, _ = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    cancelled = {a.id: asyncio.Event()}
    finished = asyncio.Event()
    clock = _VirtualClock()
    probe = _WatchProbe(sm, monkeypatch, clock)
    watch = probe.watching(
        asyncio.create_task(
            watch_fleet_budget(
                sessionmaker=probe.sessionmaker,
                parent=parent,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled=cancelled,
                finished=finished,
                poll_interval=0.01,
                read_timeout=READ_TIMEOUT_S,
            )
        )
    )
    try:
        await probe.until(lambda: probe.reads >= 1, "the first locked refresh")
        reads = probe.reads
        await probe.polls_since(probe.polls, 5)
        assert probe.reads == reads
        clock.advance(channel_budget.BUDGET_LOCKED_REFRESH_S)
        await probe.until(lambda: probe.reads > reads, "the next locked refresh")
        finished.set()
        assert await asyncio.wait_for(watch, 5) == "finished"
        assert not cancelled[a.id].is_set()
    finally:
        finished.set()
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)


async def test_run_watch_fails_closed_when_the_tenant_lock_stays_busy(sm, monkeypatch):
    from daimon.core import channel_budget as _cb

    # The fail-closed limits themselves, without the 30 s read-failure window.
    monkeypatch.setattr(_cb, "BUDGET_READ_FAILURE_WINDOW_S", 0.0)
    from daimon.core import channel_budget

    parent, _ = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    cancelled = {a.id: asyncio.Event()}
    clock = _VirtualClock()
    probe = _WatchProbe(sm, monkeypatch, clock)
    limit = 1.0
    monkeypatch.setattr(channel_budget, "BUDGET_LOCK_BUSY_LIMIT_S", limit)
    async with _tenant_lock_held(sm, parent):
        watch = probe.watching(
            asyncio.create_task(
                watch_fleet_budget(
                    sessionmaker=probe.sessionmaker,
                    parent=parent,
                    prices=tariffs(parent),
                    markup=Decimal(1),
                    cancelled=cancelled,
                    finished=asyncio.Event(),
                    poll_interval=0.01,
                    read_timeout=READ_TIMEOUT_S,
                )
            )
        )
        try:
            # Busy is judged by how long, not how often: no virtual time has passed.
            await probe.polls_since(0, 2)
            assert not cancelled[a.id].is_set() and not watch.done()
            clock.advance(limit + 0.1)
            result = await asyncio.wait_for(watch, 10)
            assert result == "usage_unavailable" and cancelled[a.id].is_set()
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
    assert (await read(sm, parent, a)).worker_exposure_usd == 1


async def test_run_watch_is_not_starved_by_back_to_back_short_lock_holds(sm, monkeypatch):
    # Staging r21 (2026-10-18): a try-lock never queued, so admissions' repeated short
    # holds kept the watch busy for 10s and it cancelled every worker.
    from daimon.core import channel_budget
    from sqlalchemy import text

    parent, _ = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    cancelled = {a.id: asyncio.Event()}
    finished = asyncio.Event()
    monkeypatch.setattr(channel_budget, "BUDGET_LOCK_BUSY_LIMIT_S", 1.0)
    stop = asyncio.Event()
    reads = 0
    original = channel_budget.produce_fleet_budget

    async def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await original(*args, **kwargs)

    async def hammer():
        while not stop.is_set():
            async with sm.begin() as db:
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"fleet:{parent.fleet.scope.tenant_id}"},
                )
                await asyncio.sleep(0.03)

    monkeypatch.setattr(channel_budget, "produce_fleet_budget", counted)
    hammers = [asyncio.create_task(hammer()) for _ in range(3)]
    watch = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled=cancelled,
            finished=finished,
            poll_interval=0.05,
            read_timeout=READ_TIMEOUT_S,
        )
    )
    try:
        await asyncio.sleep(2.0)
        assert not cancelled[a.id].is_set() and not watch.done() and reads > 0
    finally:
        stop.set()
        await asyncio.gather(*hammers)
        finished.set()
        assert await asyncio.wait_for(watch, 3) == "finished"


async def test_fleet_r8_slow_admissions_holding_the_lock_in_turn_do_not_cancel_the_run(
    sm, db_nullpool_engine, monkeypatch
):
    """Fleet r8: admissions each held the tenant lock longer than the watch waits.

    Their holds changed hands, so the run was making progress; the watch still
    cancelled every worker after 10 s of busy polls. Each hold here outlasts the
    watch's lock wait but not the per-holder limit, so only a stuck holder may fail it.
    """
    from daimon.core import channel_budget
    from sqlalchemy import text

    parent, _ = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    cancelled = {a.id: asyncio.Event()}
    finished = asyncio.Event()
    monkeypatch.setattr(channel_budget, "BUDGET_LOCK_BUSY_LIMIT_S", 1.5)
    stop = asyncio.Event()
    # Admissions use their own connections, as other requests in the host do.
    admissions_sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)

    async def admission():
        while not stop.is_set():
            async with admissions_sm.begin() as db:
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"fleet:{parent.fleet.scope.tenant_id}"},
                )
                # Longer than the watch's lock wait (0.75 * READ_TIMEOUT_S).
                await asyncio.sleep(0.8)

    admissions = [asyncio.create_task(admission()) for _ in range(3)]
    watch = asyncio.create_task(
        watch_fleet_budget(
            sessionmaker=sm,
            parent=parent,
            prices=tariffs(parent),
            markup=Decimal(1),
            cancelled=cancelled,
            finished=finished,
            poll_interval=0.05,
            read_timeout=READ_TIMEOUT_S,
        )
    )
    try:
        await asyncio.sleep(4.0)
        assert not cancelled[a.id].is_set() and not watch.done()
    finally:
        stop.set()
        await asyncio.gather(*admissions)
        finished.set()
        assert await asyncio.wait_for(watch, 5) == "finished"
    assert not cancelled[a.id].is_set()


async def test_run_watch_contended_by_changing_holders_still_fails_closed_at_its_limit(
    sm, monkeypatch
):
    from daimon.core import channel_budget as _cb

    # The fail-closed limits themselves, without the 30 s read-failure window.
    monkeypatch.setattr(_cb, "BUDGET_READ_FAILURE_WINDOW_S", 0.0)
    from daimon.core import channel_budget

    parent, _ = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    cancelled = {a.id: asyncio.Event()}
    clock = _VirtualClock()
    probe = _WatchProbe(sm, monkeypatch, clock)
    monkeypatch.setattr(channel_budget, "BUDGET_LOCK_BUSY_LIMIT_S", 1.0)
    monkeypatch.setattr(channel_budget, "BUDGET_LOCK_CONTENDED_LIMIT_S", 3.0)
    holders = iter(range(10_000))

    async def changing(session, key):
        n = next(holders)
        return {"pid": n, "xact_start": str(n), "state": "active", "wait": "", "query": ""}

    monkeypatch.setattr(channel_budget, "_lock_holder", changing)
    async with _tenant_lock_held(sm, parent):
        watch = probe.watching(
            asyncio.create_task(
                watch_fleet_budget(
                    sessionmaker=probe.sessionmaker,
                    parent=parent,
                    prices=tariffs(parent),
                    markup=Decimal(1),
                    cancelled=cancelled,
                    finished=asyncio.Event(),
                    poll_interval=0.01,
                    read_timeout=READ_TIMEOUT_S,
                )
            )
        )
        try:
            # Busy for 2.7 s with a new holder each poll: past the stuck limit, not stuck.
            for _ in range(3):
                await probe.polls_since(probe.polls, 1)
                clock.advance(0.9)
            await probe.polls_since(probe.polls, 1)
            assert not cancelled[a.id].is_set() and not watch.done()
            clock.advance(0.5)
            result = await asyncio.wait_for(watch, 10)
            assert result == "usage_unavailable" and cancelled[a.id].is_set()
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)


async def test_a_busy_lock_never_blinds_the_watch_to_an_overrun(sm, monkeypatch):
    """#1302 review: while admissions hold the lock, the watch is the only overrun guard.

    Busy polls decide from a lock-free snapshot, so an over-budget run is cancelled
    without waiting for the lock or any busy limit.
    """
    from daimon.core import channel_budget
    from daimon.core._models import FleetRun as RunRow
    from sqlalchemy import update

    parent, run = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    cancelled = {a.id: asyncio.Event()}
    finished = asyncio.Event()
    monkeypatch.setattr(channel_budget, "BUDGET_LOCK_BUSY_LIMIT_S", 1000.0)
    monkeypatch.setattr(channel_budget, "BUDGET_LOCK_CONTENDED_LIMIT_S", 1000.0)
    async with _tenant_lock_held(sm, parent):
        watch = asyncio.create_task(
            watch_fleet_budget(
                sessionmaker=sm,
                parent=parent,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled=cancelled,
                finished=finished,
                poll_interval=0.05,
                read_timeout=READ_TIMEOUT_S,
            )
        )
        try:
            await asyncio.sleep(0.5)
            assert not cancelled[a.id].is_set()
            # The run's cap drops below what it holds: an overrun the watch must catch.
            async with sm.begin() as db:
                await db.execute(
                    update(RunRow)
                    .where(RunRow.id == run.id)
                    .values(total_budget_usd=Decimal("0.5"))
                )
            await asyncio.wait_for(cancelled[a.id].wait(), 5)
            assert not watch.done()
        finally:
            finished.set()
            await asyncio.wait_for(watch, 5)


async def test_snapshot_read_takes_no_lock_and_writes_nothing(sm):
    from daimon.core._models import FleetWorker as WorkerRow
    from daimon.core.channel_budget import read_fleet_budget_snapshot
    from sqlalchemy import select, text

    parent, run = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    async with sm() as db:
        before = (await db.execute(select(WorkerRow.version).where(WorkerRow.id == a.id))).scalar()
    holder = await sm().__aenter__()
    try:
        await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"fleet:{parent.fleet.scope.tenant_id}"},
        )
        await holder.execute(select(WorkerRow).where(WorkerRow.id == a.id).with_for_update())
        usages = await asyncio.wait_for(
            read_fleet_budget_snapshot(
                sm, parent=parent, prices=tariffs(parent), markup=Decimal(1)
            ),
            5,
        )
        assert usages[a.id].admitted and usages[a.id].worker.reserved_usd == 1
    finally:
        await holder.rollback()
        await holder.close()
    async with sm() as db:
        after = (await db.execute(select(WorkerRow.version).where(WorkerRow.id == a.id))).scalar()
    assert after == before


async def test_busy_lock_names_its_actual_holder(sm):
    from daimon.core.channel_budget import _lock_holder  # pyright: ignore[reportPrivateUsage]
    from sqlalchemy import text

    parent, _ = await setup(sm)
    key = f"fleet:{parent.fleet.scope.tenant_id}"
    async with sm.begin() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
        )
        pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        async with sm.begin() as watcher:
            seen = await _lock_holder(watcher, key)
            assert seen is not None and seen["pid"] == pid
            assert seen["state"] == "idle in transaction" and seen["xact_start"]
            assert await _lock_holder(watcher, "fleet:" + str(uuid4())) is None


async def _parallel_default_spawns(sm, parent, run, hints):
    """Each spawn reads the same empty snapshot, as parallel fleet_spawn calls do."""
    from daimon.core.neutral_resources import default_worker_limits

    shares = [
        default_worker_limits(
            parent.limits, run, (), provider=parent.spec.model.provider, planned_workers=hint
        ).budget_usd
        for hint in hints
    ]
    workers = [
        await add(sm, parent, ordinal=ordinal, budget=str(share.normalize()))
        for ordinal, share in enumerate(shares)
    ]
    return await asyncio.gather(*(reserve(sm, parent, worker) for worker in workers))


@pytest.mark.parametrize("hints", [(2, 2, 2, 2), (2, 4, 2, 4)])
async def test_under_planned_parallel_spawns_never_burn_slots(sm, hints):
    """More spawns at once than planned are refused at admission, and free their slot."""
    parent, run = await setup(sm, cap="1", max_workers=4)
    results = await _parallel_default_spawns(sm, parent, run, hints)
    admitted = [result for result in results if result.admitted]
    refused = [result for result in results if not result.admitted]
    assert refused and all(result.worker.status == "cancelled" for result in refused)
    assert sum(result.worker.budget_usd for result in admitted) <= run.total_budget_usd
    # Every refused spawn left its slot: the run can still fill all 4 with live workers.
    for ordinal in range(len(hints), len(hints) + len(refused)):
        await add(sm, parent, ordinal=ordinal, budget="0.01")
    with pytest.raises(FleetConflict, match="allows 4 workers"):
        await add(sm, parent, ordinal=len(hints) + len(refused), budget="0.01")
    async with sm() as db, db.begin():
        _, workers = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
    assert sum(1 for worker in workers if holds_slot(worker)) == run.max_workers


async def test_admission_refusals_stay_bounded_by_twice_max_workers(sm):
    parent, run = await setup(sm, cap="1", max_workers=2)
    for ordinal in range(4):
        worker = await add(sm, parent, ordinal=ordinal, budget="2")
        assert not (await reserve(sm, parent, worker)).admitted
    with pytest.raises(FleetConflict, match="allows 2 workers"):
        await add(sm, parent, ordinal=4, budget="0.01")


def test_worker_cancels_say_why_once():
    """Fleet r11: an out-of-memory restart interrupted every worker with no cause logged."""
    from daimon.core.channel_budget import cancel_workers
    from structlog.testing import capture_logs

    run_id, a, b = uuid4(), uuid4(), uuid4()
    events = {a: asyncio.Event(), b: asyncio.Event()}
    events[b].set()
    with capture_logs() as logs:
        cancel_workers(events, "shutdown", run_id=run_id)
        cancel_workers(events, "watch_ended", run_id=run_id)
    assert all(event.is_set() for event in events.values())
    (entry,) = [log for log in logs if log["event"] == "fleet.worker_cancel"]
    assert entry["reason"] == "shutdown" and entry["run_id"] == str(run_id)
    # Only the worker this call actually cancelled is named.
    assert entry["worker_ids"] == [str(a)]


async def test_host_shutdown_names_itself_as_the_cancel_reason(sm):
    """The r11 repro: the host's lifespan ends mid-dispatch; workers must not just vanish."""
    from daimon.core.fleet_orchestration import HostFleetPorts
    from structlog.testing import capture_logs

    ports = HostFleetPorts(sm, prices={}, markup=Decimal(1))
    run_id, worker_id = uuid4(), uuid4()
    cancel, finished = asyncio.Event(), asyncio.Event()

    async def watching():
        await finished.wait()

    task = asyncio.create_task(watching())
    ports._watches[run_id] = ({worker_id: cancel}, finished, task)  # pyright: ignore[reportPrivateUsage]
    with capture_logs() as logs:
        await ports.close()
    assert cancel.is_set() and finished.is_set() and task.done()
    (entry,) = [log for log in logs if log["event"] == "fleet.worker_cancel"]
    assert (entry["reason"], entry["run_id"], entry["worker_ids"]) == (
        "shutdown",
        str(run_id),
        [str(worker_id)],
    )


async def test_watch_logs_why_it_releases_a_terminal_worker(sm):
    from daimon.core import channel_budget
    from structlog.testing import capture_logs

    parent, run = await setup(sm)
    a = await add(sm, parent)
    assert (await reserve(sm, parent, a)).admitted
    async with sm.begin() as db:
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=a.id,
            expected_version=(await read(sm, parent, a)).worker.version,
            status="cancelled",
            error_code="stopped",
        )
    cancelled = {a.id: asyncio.Event()}
    finished = asyncio.Event()
    with capture_logs() as logs:
        watch = asyncio.create_task(
            channel_budget.watch_fleet_budget(
                sessionmaker=sm,
                parent=parent,
                prices=tariffs(parent),
                markup=Decimal(1),
                cancelled=cancelled,
                finished=finished,
                poll_interval=0.01,
                read_timeout=READ_TIMEOUT_S,
            )
        )
        await asyncio.wait_for(cancelled[a.id].wait(), 5)
        finished.set()
        assert await asyncio.wait_for(watch, 5) == "finished"
    assert any(
        log["event"] == "fleet.worker_cancel" and log["reason"] == "terminal" for log in logs
    )


async def test_a_runs_cost_is_its_own_workers_observed_usage_before_any_debit(sm):
    """J3's cost line: read fresh from this run's rows, priced usage counts at once."""
    from daimon.core.channel_budget import read_fleet_run_cost

    parent, run = await setup(sm)
    worker = await add(sm, parent, budget="2")
    assert (await reserve(sm, parent, worker)).admitted
    assert await read_fleet_run_cost(
        sm, parent=parent, prices=tariffs(parent), markup=Decimal(1)
    ) == Decimal(0)
    await usage(sm, parent, run, worker, inputs=600_000, outputs=0)
    cost = await read_fleet_run_cost(sm, parent=parent, prices=tariffs(parent), markup=Decimal(1))
    assert cost == Decimal("1.2"), "the measured usage, not the stored run figure"
    # Another run's spend is never this run's.
    other_parent, other = await next_run(sm, parent, run)
    stranger = await add(sm, other_parent, budget="2")
    assert (await reserve(sm, other_parent, stranger)).admitted
    await usage(sm, other_parent, other, stranger, inputs=600_000, outputs=0)
    assert await read_fleet_run_cost(
        sm, parent=parent, prices=tariffs(parent), markup=Decimal(1)
    ) == Decimal("1.2")


async def test_a_slow_or_failed_cost_read_says_not_available_never_zero(monkeypatch):
    import asyncio

    from daimon.core import channel_budget, fleet_orchestration
    from daimon.core.fleet_orchestration import (
        FleetOrchestrator,
        HostFleetPorts,
        fleet_cost_sentence,
    )

    async def stuck(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(channel_budget, "read_fleet_run_cost", stuck)
    monkeypatch.setattr(fleet_orchestration, "RUN_COST_TIMEOUT_S", 0.05)
    ports = object.__new__(HostFleetPorts)
    ports.sessions, ports.prices, ports.markup = None, {}, Decimal(1)
    parent = root()
    assert await ports.run_cost(parent, parent.fleet.run_id) is None, "bounded, then None"

    class Failing:
        async def run_cost(self, *_a):
            raise RuntimeError("pool exhausted")

    run = SimpleNamespace(id=parent.fleet.run_id, observed_usd=Decimal(0))
    orchestrator = FleetOrchestrator(Failing())  # pyright: ignore[reportArgumentType]
    assert await orchestrator.run_cost(parent, run) is None  # pyright: ignore[reportArgumentType]
    assert fleet_cost_sentence(None) == "This cost isn't available yet."
    assert fleet_cost_sentence(Decimal("0.001911")) == "This cost less than $0.01."
    assert fleet_cost_sentence(Decimal("0.425")) == "This cost $0.43."
