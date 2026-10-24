"""A self-hosted channel backend runs end to end on the mux leg and is refused on legacy.

Reuses the real host runtime, state stores and HTTP fake of `test_selfhosted_host`
(no live harness, model or credentials) and swaps only the turn leg:

* mux_selfhosted: admission binds the channel backend, the turn is dispatched on
  the mux path with the self-hosted profile, the host answers, and the usage path
  records the harness-reported tokens;
* legacy_selfhosted: the same request is refused as `backend_unsupported` before
  any harness I/O or usage.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from daimon.core.turn import run as turn_run
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.testing.turn_fakes import RecordingLifecycle
from daimon.testing.turn_leg import LEGACY_LEG, MUX_LEG

from .conformance_support import ACCOUNT, TENANT
from .test_selfhosted_host import (
    PROFILE,
    composed,  # noqa: F401  (fixture)
)


@pytest.fixture
def pinned_leg(request):
    """The leg under test, chosen per test: `test_selfhosted_host.composed` builds on it."""
    return request.param


async def _bind(request):
    return await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id="thread",
        session_account_id=ACCOUNT,
        reuse_existing=True,
    )


@pytest.mark.parametrize("pinned_leg", [MUX_LEG], ids=["mux"], indirect=True)
@pytest.mark.parametrize("composed", ["memory"], indirect=True)
async def test_mux_selfhosted_turn_is_admitted_dispatched_and_billed(composed, monkeypatch):  # noqa: F811
    request, wire, _, observations = composed
    dispatched = []
    real = turn_run._turn_port_kwargs
    monkeypatch.setattr(
        turn_run,
        "_turn_port_kwargs",
        lambda *a, **k: dispatched.append(real(*a, **k)) or dispatched[-1],
    )

    prepared = await _bind(request)

    async def reseed():
        return "question"

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="caller",
        user_message="question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        render_interval_s=0.01,
        operation_key="leg-selfhosted",
    )
    await drain_outcomes()

    assert request.deps.turn_path == "mux" and request.deps.channel_backends
    assert [(k["path"], k["profile"]) for k in dispatched] == [("mux", PROFILE)]
    assert outcome.state.error is None
    assert "answer 1" in str(outcome.state.content)
    assert wire.posts == 1
    (recorded,) = observations
    usage = recorded["observation"]
    assert recorded["model_id"] == "gpt-6-luna"
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)


@pytest.mark.parametrize("pinned_leg", [MUX_LEG], ids=["mux"], indirect=True)
@pytest.mark.parametrize("composed", ["postgres"], indirect=True)
async def test_mux_selfhosted_turn_persists_usage_through_the_real_host_path(composed):  # noqa: F811
    """Admit the tariff, run the turn, and read the committed usage and ledger rows back.

    The host records through `record_selfhosted_usage` exactly as
    `selfhosted_deployment` wires it; only the harness HTTP and the price table
    are fakes.
    """
    from datetime import date
    from decimal import Decimal

    from daimon.core.config import SelfhostedBillingSettings, SelfhostedModelPrice
    from daimon.core.selfhosted_usage import admit_selfhosted_usage, record_selfhosted_usage
    from daimon.core.stores import tenant_ledger, usage_events
    from daimon.core.stores.accounting_outbox import list_pending_usage
    from daimon.core.stores.mux_state import PostgresStateStore
    from mux.contracts.ids import ThreadRef
    from mux.state.lease import Slot

    from .test_provider_dispatch import SCOPE
    from .test_selfhosted_host import REVISION

    request, wire, _, _ = composed
    admission_key = "leg-selfhosted-usage"
    billing = SelfhostedBillingSettings(
        deployments={"gpt-6-luna": "test-model"},
        models={
            "test-model": [
                SelfhostedModelPrice(
                    effective_from=date(2026, 1, 1), input="2", output="10", cache_read="0.5"
                )
            ]
        },
    )
    sessionmaker = request.deps.sessionmaker

    async def record(observation, binding_id):
        return await record_selfhosted_usage(
            sessionmaker=sessionmaker,
            observation=observation,
            binding_id=binding_id,
            tenant_id=TENANT,
            platform_user_id="caller",
            admission_key=admission_key,
            infrastructure_usd=Decimal(0),
            markup=request.deps.markup,
        )

    runtime = replace(request.deps.turn_runtimes[PROFILE], record_usage=record)
    request = replace(request, deps=replace(request.deps, turn_runtimes={PROFILE: runtime}))
    prepared = await _bind(request)
    binding = await PostgresStateStore(sessionmaker).get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id=request.thread_id),
            account_id=SCOPE.account_id,
        )
    )
    assert binding is not None
    admitted = await admit_selfhosted_usage(
        sessionmaker=sessionmaker,
        tenant_id=TENANT,
        binding_id=binding.id,
        session_ref=prepared.session_ref,
        admission_key=admission_key,
        deployment="gpt-6-luna",
        billing=billing,
    )

    async def reseed():
        return "question"

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="caller",
        user_message="question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        render_interval_s=0.01,
        operation_key=admission_key,
    )
    await drain_outcomes()

    assert outcome.state.error is None and wire.posts == 1
    async with sessionmaker() as db:
        rows = await usage_events.list_for_tenant(db, tenant_id=TENANT)
        ledger = await tenant_ledger.list_for_tenant(db, tenant_id=TENANT)
        pending = await list_pending_usage(db, tenant_id=TENANT)
    # The admitted underlying model is the accounting identity, not the deployment alias.
    assert [(r.model, r.input_tokens, r.output_tokens) for r in rows] == [("test-model", 10, 5)]
    assert rows[0].managed_session_id == prepared.session_ref.id
    # (10 * 2 + 5 * 10) / 1M at the admitted tariff, no infrastructure cost, markup 1.
    debits = [row.delta_usd for row in ledger if row.delta_usd < 0]
    assert debits == [Decimal("-0.000070")]
    assert pending.rows == []
    # The tariff snapshot the receipt froze is the one admitted before the turn.
    assert (admitted.deployment, admitted.underlying_model) == ("gpt-6-luna", "test-model")
    assert (admitted.tariff.input, admitted.tariff.output, admitted.tariff.cache_read) == (
        Decimal(2),
        Decimal(10),
        Decimal("0.5"),
    )


@pytest.mark.parametrize("pinned_leg", [LEGACY_LEG], ids=["legacy"], indirect=True)
@pytest.mark.parametrize("composed", ["memory"], indirect=True)
async def test_legacy_selfhosted_channel_backend_is_refused(composed):  # noqa: F811
    request, wire, _, observations = composed

    assert request.deps.turn_path == "legacy" and not request.deps.channel_backends
    with pytest.raises(AdmissionDenied) as refused:
        await _bind(request)

    assert refused.value.reason == "backend_unsupported"
    assert wire.posts == 0
    assert not observations
