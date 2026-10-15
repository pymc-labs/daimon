"""Actual A3 admission exposes an unavailable NEW27 prerequisite, not FAIL."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from daimon.testing import db as database_fixtures
from mux.errors import UnsupportedCapability
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.turn import test_openai_credential_requests as a3

from .catalog_runner import ScenarioPlan, load_catalog, plan_scenario
from .openai_rejected_token_catalog import (
    SCENARIO,
    OpenAIRejectedTokenGap,
    observe_openai_new27_prerequisite,
    openai_new27_pending_outcome,
    validate_openai_new27,
)

db_preflight = a3.db_preflight
owned_database_preflight = a3.owned_database_preflight
db_clean = database_fixtures.db_clean
db_engine = database_fixtures.db_engine
db_schema = database_fixtures.db_schema
case = a3.case
isolated = a3.isolated
type Case = a3.Case
CATALOG = Path(__file__).resolve().parents[2] / "packages/testing/fixtures/target53/catalog"


@pytest.fixture
def plan() -> ScenarioPlan:
    entry = next(entry for entry in load_catalog(CATALOG) if entry.id == SCENARIO)
    return plan_scenario(entry, CATALOG, "openai", run_id="new27-prerequisite")


async def test_real_native_offer_admission_keeps_new27_pending(
    case: Case, plan: ScenarioPlan, record_property: Callable[[str, object], None]
) -> None:
    before = len(case.wire.requests)
    gap = await observe_openai_new27_prerequisite(
        plan, case.adapter, case.origin, product_profile="opt-in-product"
    )
    outcome = openai_new27_pending_outcome(gap)
    assert outcome.status == "PENDING"
    assert outcome.checks[0].code == "OPENAI_NATIVE_MCP_FORM_UNAVAILABLE"
    assert gap.product_profile == "opt-in-product"
    assert gap.source_sha256 == plan.scenario.sha256
    assert not gap.submit_observed and not gap.rejecting_probe_observed and not gap.used_at_observed
    assert len(case.wire.requests) == before
    assert case.delivery.registered == []
    assert case.delivery.surface.snapshot().messages == ()
    # The actual owned SDK setup created a native copy. No legacy form/agent
    # substitution is needed to observe that A3 cannot offer an MCP token.
    assert any(request.method == "POST" for request in case.wire.requests)
    assert case.origin.agent.provider == "openai"
    record_property("openai_new27_prerequisite", gap.model_dump_json())
    record_property("openai_new27_status", outcome.status)


async def test_default_profile_refuses_before_native_effects(
    case: Case, plan: ScenarioPlan
) -> None:
    before = len(case.wire.requests)
    with pytest.raises(ValueError, match="opt-in profile"):
        await observe_openai_new27_prerequisite(plan, case.adapter, case.origin)
    assert len(case.wire.requests) == before and case.delivery.registered == []


@pytest.mark.parametrize("missing", ["flag", "delivery"])
async def test_missing_binding_is_typed_refusal(
    case: Case, plan: ScenarioPlan, missing: str
) -> None:
    if missing == "flag":
        case.adapter.operator_enabled = False
    else:
        case.adapter.delivery = None
    before = len(case.wire.requests)
    with pytest.raises(UnsupportedCapability):
        await observe_openai_new27_prerequisite(
            plan, case.adapter, case.origin, product_profile="opt-in-product"
        )
    assert len(case.wire.requests) == before and case.delivery.registered == []


async def test_callback_rebound_foreign_database_is_refused(case: Case, plan: ScenarioPlan) -> None:
    foreign = create_async_engine(
        "postgresql+asyncpg://test:test@foreign.invalid/daimon_test_nc_n11"
    )
    before = len(case.wire.requests)
    case.sessions.configure(bind=foreign)
    try:
        with pytest.raises(ValueError, match="owned"):
            await observe_openai_new27_prerequisite(
                plan, case.adapter, case.origin, product_profile="opt-in-product"
            )
        assert len(case.wire.requests) == before and case.delivery.registered == []
    finally:
        await foreign.dispose()


async def test_supported_mcp_mutation_cannot_fabricate_pending_or_readback(
    case: Case, plan: ScenarioPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mutation control: skip the unsupported-kind gate. A new real capability
    # must receive a real submit/probe/readback adapter before classification.
    receipt = await case.adapter.offer(case.origin, kind="env", target="NEW27_TOKEN")

    async def accepted(*args: object, **kwargs: object) -> a3.NativeOfferReceipt:
        return receipt

    monkeypatch.setattr(case.adapter, "offer", accepted)
    with pytest.raises(ValueError, match="bind real submit/probe/readback"):
        await observe_openai_new27_prerequisite(
            plan, case.adapter, case.origin, product_profile="opt-in-product"
        )


@pytest.mark.parametrize(
    "mutation",
    ["backend", "source_hash", "document", "expect", "sql", "token", "step", "turn", "global"],
)
def test_source_mutations_refuse_before_hook(plan: ScenarioPlan, mutation: str) -> None:
    if mutation == "backend":
        changed = plan.model_copy(update={"backend": "anthropic"})
    elif mutation == "source_hash":
        changed = plan.model_copy(
            update={"scenario": plan.scenario.model_copy(update={"sha256": "0" * 64})}
        )
    elif mutation == "document":
        changed = plan.model_copy(
            update={
                "scenario": plan.scenario.model_copy(
                    update={"scenario": plan.scenario.scenario | {"title": "changed"}}
                )
            }
        )
    elif mutation in {"expect", "sql"}:
        assertion = plan.assertions[0] | (
            {"expect": False} if mutation == "expect" else {"sql": "SELECT true"}
        )
        changed = plan.model_copy(update={"assertions": (assertion,)})
    elif mutation == "global":
        changed = plan.model_copy(update={"global_assertions": ({"kind": "db_check"},)})
    else:
        invocation = plan.invocations[0]
        if mutation == "token":
            args = invocation.source["args"] | {"token": "another-token"}
            invocation = invocation.model_copy(
                update={"source": invocation.source | {"args": args}}
            )
        elif mutation == "step":
            invocation = invocation.model_copy(update={"location": "steps[1]"})
        else:
            invocation = invocation.model_copy(update={"turn": 1})
        changed = plan.model_copy(update={"invocations": (invocation,)})
    with pytest.raises(ValueError, match="pinned administrative source"):
        validate_openai_new27(changed)


def test_gap_is_not_submit_or_used_at_evidence(plan: ScenarioPlan) -> None:
    gap = OpenAIRejectedTokenGap(source_sha256=plan.scenario.sha256)  # pyright: ignore[reportArgumentType]
    for field in ("submit_observed", "rejecting_probe_observed", "used_at_observed"):
        data = gap.model_dump() | {field: True}
        with pytest.raises(ValidationError):
            OpenAIRejectedTokenGap.model_validate(data)
        with pytest.raises(ValidationError):
            openai_new27_pending_outcome(gap.model_copy(update={field: True}))
    with pytest.raises(ValidationError):
        OpenAIRejectedTokenGap.model_validate(gap.model_dump() | {"used_at": None})
    assert openai_new27_pending_outcome(gap).checks[0].evidence_ids == ()
