"""J10: a finished fleet hands back everything it did not spend, on every provider.

Staging 7aba3054: a tenant with $207 of credit was refused "this server's spending
limit was reached" because 125 finished fleet runs still held $203. #1481/#1482 were
meant to settle them; on real rows none of the three providers' workers could:

- OpenAI conversation-only: its tariff bills cache writes, its turn meter has no
  cache-write count, so `provider_cost_of` priced no measured row (since #1019) and
  every row stayed pending. ROOT D2=B: such a turn is priced as writing no cache, an
  estimate marked on its debit. A turn first read before OpenAI metered it
  (`usage: null`) was never read again.
- Gemini: its sends stay `accepted`, which #1482's settle read as still in flight.
- Anthropic Managed Agents: no outbox; its debits are keyed by its own session, and
  the settle read none of them ($0, "no usage", so the hold was kept).

The journey uses each provider's real row shapes (the staging rows), not the test
helper's: available after the fleet == available before - what was actually debited,
and the run's observed spend == the sum of its workers' debits.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from daimon.core._models import MuxOperation, TenantLedger
from daimon.core.accounting_outbox import record_observation_usage
from daimon.core.channel_backend import AgentSettings, agent_selection
from daimon.core.channel_budget import admit_fleet_worker, produce_fleet_budget
from daimon.core.fleet import spawn_operation_key, worker_slot
from daimon.core.neutral_resources import resolve_worker_spec
from daimon.core.pricing import MODEL_PRICING, provider_prices
from daimon.core.stores import fleet, mux_state
from daimon.core.usage_outbox_drain import drain_pending_usage
from daimon.core.usage_recording import record_turn_usage
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.profiles import backend_selection
from sqlalchemy import func, insert, select

from .test_fleet_budget import POLICY, add, root, setup
from .test_fleet_budget import sm as sm
from .test_fleet_budget_batching import _claim, _run

LUNA = "gpt-6-luna"
CONVERSATION = agent_selection("openai", None, AgentSettings(profile="openai.conversation_only"))
GEMINI = backend_selection("gemini")
ANTHROPIC = backend_selection("anthropic")


def _parent():
    """A root granted all three providers: one worker of each in one run."""
    parent = root("gemini")
    models = (
        parent.spec.model,
        ModelRef(provider="openai", id=LUNA),
        ModelRef(provider="anthropic", id=ANTHROPIC.model or "claude-haiku-5-5"),
    )
    return replace(parent, backend_grants=(GEMINI, CONVERSATION, ANTHROPIC), models=models)


def _prices(parent):
    prices: dict = {}
    for grant, model in zip(parent.backend_grants, parent.models, strict=True):
        if grant.backend == "anthropic":
            prices[(model.provider, model.id)] = MODEL_PRICING[model.id]
        else:
            prices[(model.provider, model.id)] = provider_prices(grant.profile)[
                (model.provider, model.id)
            ]
    return prices


def _spec(parent, model: ModelRef):
    return parent.spec.model_copy(update={"model": model})


async def _available(sm, parent, prices) -> Decimal:
    """What a later fleet in this tenant may still spend (its own worker holds nothing)."""
    async with sm() as db, db.begin():
        usage = await produce_fleet_budget(db, parent=parent, prices=prices, markup=Decimal(1))
    (probe,) = usage.values()
    return probe.prepaid_available_usd


async def _worker(sm, parent, run, ordinal, model: ModelRef, prices):
    """Reserved, claimed and running on its own native session."""
    limits = parent.limits.model_copy(update={"budget_usd": Decimal(1)})
    resolved = resolve_worker_spec(
        parent, _spec(parent, model), policy=POLICY, now=datetime.now(UTC), limits=limits
    )
    async with sm() as db, db.begin():
        worker = await fleet.add_worker(
            db,
            tenant_id=run.tenant_id,
            run_id=run.id,
            ordinal=ordinal,
            role="worker",
            spec=resolved.spec.model_dump(mode="json"),
            spec_digest=resolved.digest,
            backend=resolved.backend.backend,
            profile=resolved.backend.profile,
            model=resolved.spec.model.id,
            budget_usd=Decimal(1),
            deadline_at=limits.deadline,
            max_turns=limits.max_turns,
        )
        admitted = await admit_fleet_worker(
            db,
            parent=parent,
            policy=POLICY,
            resolved=resolved,
            ordinal=ordinal,
            role="worker",
            prices=prices,
            markup=Decimal(1),
        )
    assert admitted.admitted, admitted.reason
    claimed = await _claim(sm, run, worker)
    session_id = f"native-{worker.backend}-{worker.id}"
    binding = ProviderBinding(
        id=f"worker-{worker.id}",
        thread=worker_slot(run, worker).thread,
        legacy_account_id=str(parent.fleet.account_id),
        provider=worker.backend,
        profile=worker.profile,
        native_refs={"session": session_id},
        generation=1,
        config_revision=0,
    )
    async with sm() as db, db.begin():
        await mux_state.put_binding(db, binding, expected_generation=0)
    running = await fleet.mark_running(
        sm,
        tenant_id=run.tenant_id,
        worker_id=worker.id,
        expected_version=claimed.version,
        binding_id=binding.id,
    )
    native = ResourceRef(
        id=session_id,
        kind="session",
        provider=worker.backend,
        account_scope_id="daimon-deployment",
        tenant_id=str(run.tenant_id),
        account_id=str(run.orchestrator_account_id),
    )
    return running, binding, native


async def _send(sm, run, worker, *, status: str, turn_id: str | None = None):
    """The worker's input send, with the receipt the provider path stores."""
    result = {} if turn_id is None else {"status": "queued", "turn_id": turn_id}
    async with sm() as db, db.begin():
        await db.execute(
            insert(MuxOperation).values(
                tenant_id=run.tenant_id,
                account_id=str(run.orchestrator_account_id),
                key=spawn_operation_key(run.id, worker.ordinal) + ":send:0",
                principal_id=str(run.orchestrator_account_id),
                status=status,
                record={"result": result},
                updated_at=func.now(),
            )
        )


def _openai_turn(native, turn: str, *, revision=1, metered=True) -> UsageObservation:
    """`mux.drivers.openai.usage.observation`'s shape: no cache-write count, ever."""
    meter = (
        {
            "input_tokens": 13351,
            "total_tokens": 13412,
            "output_tokens": 61,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 37},
        }
        if metered
        else {}
    )
    return UsageObservation(
        id=f"openai:turn:{turn}",
        revision=revision,
        session=native,
        turn_id=turn,
        model=ModelRef(provider="openai", id=LUNA),
        grain="turn",
        basis="cumulative",
        input_tokens=13351 if metered else None,
        input_cached_tokens=0 if metered else None,
        output_tokens=61 if metered else None,
        output_reasoning_tokens=37 if metered else None,
        native_meter=meter,
        completeness="measured" if metered else "unknown",
        observed_at=datetime.now(UTC),
    )


def _gemini_interaction(native, model, turn: str, *, revision: int) -> UsageObservation:
    """Staging's Gemini rows: rev1 unknown at the interaction's start, rev2 measured."""
    metered = revision > 1
    return UsageObservation(
        id=f"gemini:{turn}:usage",
        revision=revision,
        session=native,
        turn_id=turn,
        model=model,
        grain="turn",
        basis="cumulative",
        input_tokens=4000 if metered else None,
        input_cached_tokens=1000 if metered else None,
        output_tokens=300 if metered else None,
        native_meter={"total_token_count": 4300} if metered else {},
        completeness="measured" if metered else "unknown",
        observed_at=datetime.now(UTC),
    )


async def _record(sm, run, binding, observation, prices):
    async with sm() as db, db.begin():
        return await record_observation_usage(
            db,
            binding_id=binding.id,
            observation=observation,
            tenant_id=run.tenant_id,
            platform_user_id="tester",
            pricing=None,
            provider_price=prices[(observation.session.provider, observation.model.id)],
            infrastructure_usd=Decimal(0),
            billing_grain="turn",
            channel_id=run.channel_id,
        )


async def _ma_call(sm, run, native, model_id: str, event: str):
    """One Managed Agents model call, as the live recorder or the usage sweep bills it."""
    observation = UsageObservation(
        id=event,
        revision=1,
        session=native,
        model=ModelRef(provider="anthropic", id=model_id),
        grain="model_request",
        basis="increment",
        input_tokens=2000,
        input_cached_tokens=500,
        input_cache_write_tokens=100,
        output_tokens=200,
        native_meter={"offline": True},
        completeness="measured",
        observed_at=datetime.now(UTC),
    )
    return await record_turn_usage(
        sessionmaker=sm,
        tenant_id=run.tenant_id,
        platform_user_id="tester",
        observation=observation,
        pricing=MODEL_PRICING[model_id],
        channel_id=run.channel_id,
    )


async def _debits(sm, tenant_id: UUID) -> Decimal:
    async with sm() as db:
        return -(
            await db.scalar(
                select(func.coalesce(func.sum(TenantLedger.delta_usd), 0)).where(
                    TenantLedger.tenant_id == tenant_id, TenantLedger.delta_usd < 0
                )
            )
        )


async def test_a_finished_fleet_on_every_provider_holds_exactly_what_it_spent(sm):
    parent, run = await setup(sm, parent=_parent(), funds="10", cap="4", max_workers=4)
    prices = _prices(parent)
    p2, _later = await _run(sm, parent, run, cap="1")
    await add(sm, p2, ordinal=0)
    before = await _available(sm, p2, prices)
    assert before == Decimal(10)

    gemini_model, luna, claude = parent.models
    gemini, gemini_binding, gemini_native = await _worker(sm, parent, run, 0, gemini_model, prices)
    openai, openai_binding, openai_native = await _worker(sm, parent, run, 1, luna, prices)
    anthropic, _, ma_native = await _worker(sm, parent, run, 2, claude, prices)

    # Gemini: the interaction's two revisions, both applied; its send stays accepted.
    for revision in (1, 2):
        await _record(
            sm,
            run,
            gemini_binding,
            _gemini_interaction(gemini_native, gemini_model, "v1_interaction", revision=revision),
            prices,
        )
    await _send(sm, run, gemini, status="accepted", turn_id="v1_interaction")
    # OpenAI: one turn metered at its end; one read while OpenAI still reported null.
    assert await _record(sm, run, openai_binding, _openai_turn(openai_native, "turn_a"), prices)
    assert not await _record(
        sm, run, openai_binding, _openai_turn(openai_native, "turn_b", metered=False), prices
    )
    await _send(sm, run, openai, status="processed")
    # Managed Agents: two model calls on the worker's own session.
    for event in ("sevt_1", "sevt_2"):
        assert await _ma_call(sm, run, ma_native, claude.id, event)
    await _send(sm, run, anthropic, status="processed")
    await drain_pending_usage(sm, markup=Decimal(1))  # closes Gemini's superseded rev1

    # Ended after spending (any terminal status settles alike; `succeeded` also needs
    # a journal result, beside the point here).
    for worker in (gemini, openai, anthropic):
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

    # Gemini and Managed Agents settle now; OpenAI's unmetered turn still holds.
    held = await _available(sm, p2, prices)
    _, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    by_id = {w.id: w for w in workers}
    assert by_id[gemini.id].reserved_usd < Decimal("0.01"), by_id[gemini.id].reserved_usd
    assert by_id[anthropic.id].reserved_usd < Decimal("0.05"), by_id[anthropic.id].reserved_usd
    assert by_id[anthropic.id].reserved_usd > 0  # its debits, not $0
    assert by_id[openai.id].reserved_usd == Decimal(1)  # unknown spend keeps the hold
    # Luna at (B): 13,351 input at $0.10 + 61 output at $0.50 per million, on the ledger
    # to the micro-dollar.
    luna_turn = Decimal("0.0013656").quantize(Decimal("0.000001"))
    # Held: the OpenAI worker's reservation less what it has already been debited.
    assert held == Decimal(10) - await _debits(sm, run.tenant_id) - (Decimal(1) - luna_turn)

    # OpenAI meters turn_b later; the re-read records it and the hold settles.
    from daimon.core.usage_reread import reread_unmeasured_usage

    async def list_usage(native, revision):
        assert native == openai_native
        return [
            _openai_turn(native, "turn_a", revision=revision("openai:turn:turn_a")),
            _openai_turn(native, "turn_b", revision=revision("openai:turn:turn_b")),
        ]

    assert (
        await reread_unmeasured_usage(
            sm,
            markup=Decimal(1),
            list_usage=list_usage,
            now=datetime.now(UTC) + timedelta(minutes=6),
        )
        == 1
    )

    after = await _available(sm, p2, prices)
    spent = await _debits(sm, run.tenant_id)
    ended, workers = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    assert spent > 0
    assert after == before - spent, (before, after, spent)
    assert ended.observed_usd == ended.reserved_usd == spent
    assert sum((w.reserved_usd for w in workers), Decimal(0)) == spent
    # Each worker holds exactly what it was debited; OpenAI's two turns at (B).
    assert {w.id: w.reserved_usd for w in workers}[openai.id] == 2 * luna_turn
