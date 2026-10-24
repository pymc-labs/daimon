"""Ordered public admin calls on owned DBs; missing edges never confer cell credit."""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from daimon.testing.admin_bindings import AdminBindings, AdminReceipt, provision_admin_run
from daimon.testing.anthropic_fixtures import load_anthropic_fixtures
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.outcome_oracle import TurnEvidence
from daimon.testing.provider_fixtures import FixtureGap, load_provider_fixtures
from mux.contracts.ids import Provider
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from .catalog_runner import Invocation, load_catalog, plan_scenario
from .catalog_steps import OPERATION_GAP, AdminStepBinding, StepReceipt, run_steps
from .headless_executor import (
    execute_catalog,
    execute_cell,
    execute_gemini_cell,
    execute_openai_cell,
)
from .test_anthropic_catalog import INDEX, REPO, replay, selected
from .test_catalog_runner import planned, source

pytest_plugins = ["daimon.testing.db"]
CATALOG = Path(__file__).resolve().parents[2] / "packages/testing/fixtures/target53/catalog"
CHANNEL = "223456789012345678"
VALUES = {"guild_id": "123456789012345678", "channel_id": CHANNEL}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@pytest.fixture(params=["anthropic", "openai", "gemini"])
async def binding(
    db_preflight: object,
    request: pytest.FixtureRequest,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AdminBindings]:
    try:
        require_owned_sessionmaker(db_session_factory)
    except ValueError:
        raw = db_session_factory.kw.get("bind")
        engine = raw.engine if isinstance(raw, AsyncConnection) else raw
        if (
            isinstance(engine, AsyncEngine)
            and engine.url.get_backend_name() == "postgresql"
            and engine.url.host in ("localhost", "127.0.0.1")
            and engine.url.database == "daimon_test"
            and not engine.url.query
            and not db_session_factory.kw.get("binds")
            and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
        ):
            pytest.skip("ordered admin proofs require an owned DB or registered isolated CI schema")
        raise
    bound = await provision_admin_run(
        db_session_factory,
        workspace_id=VALUES["guild_id"],
        backend=cast(Provider, request.param),
        channels=frozenset({CHANNEL}),
        trace=ScriptedTransport(),
    )
    try:
        yield bound
    finally:
        await bound.close()


async def no_turn(_i: Invocation, _timeout: float) -> TurnEvidence:
    raise AssertionError("unbound admin context reached a provider turn")


async def no_channel(_i: Invocation) -> None:
    raise AssertionError("admin-only scenario invented channel creation")


def admin_source(command: str) -> dict[str, Any]:
    return {"do": "admin", "tool": "cli", "args": command}


def admin_row(operations: list[dict[str, Any]]) -> dict[str, Any]:
    row = source()
    row["steps"] = operations
    row["assert"] = []
    return row


async def test_real_common_admin_pair_is_ordered_with_readback_and_no_turn_credit(
    tmp_path: Path, binding: AdminBindings
) -> None:
    operations = [
        admin_source("daimon channels budget set discord {guild_id} {channel_id} 0.02"),
        admin_source("daimon channels budget clear discord {guild_id} {channel_id}"),
    ]
    plan = planned(tmp_path, admin_row(operations), binding.backend)
    steps = await run_steps(
        plan,
        invoke=no_turn,
        prepare_channel=no_channel,
        admin=binding,
        admin_values=VALUES,
        native_gaps=tuple(
            FixtureGap(code="RUNNER_HOOK", location=i.location, reason=OPERATION_GAP)
            for i in plan.invocations
        ),
    )
    require(
        [r.status for r in steps.receipts] == ["executed", "executed"],
        "common admin did not execute in order",
    )
    actual = [r.admin for r in steps.receipts]
    require(
        all(r is not None and r.backend == binding.backend for r in actual),
        "lost selected provider readback",
    )
    require(
        actual[0] is not None and actual[0].observations["budget"] is not None,
        "write readback missing",
    )
    require(
        actual[1] is not None and actual[1].observations["budget"] is None, "clear readback missing"
    )
    require(
        not any(g.code == "PROVIDER_RUNNER_HOOK" for g in steps.gaps),
        "executed admin hook still unbound",
    )
    require(
        any(g.code == "NO_SCRIPTABLE_HOST_TURN" for g in steps.gaps),
        "admin evidence acquired turn credit",
    )
    require(not binding.trace.requests, "common admin called a provider")


@pytest.mark.parametrize(
    "scenario_id", ["QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS", "QA-D7-BIG-WORKSPACE-CHECKPOINT"]
)
async def test_exact_frozen_skill_pair_preserves_missing_resource_edge(
    binding: AdminBindings, scenario_id: str
) -> None:
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id)
    plan = plan_scenario(entry, CATALOG, binding.backend, run_id="admin")
    originals = [i.source for i in plan.invocations if i.source.get("do") == "admin"]
    require(len(originals) == 2, "frozen skill pair changed")
    # Each source operation is checked independently, so cleanup has no fake predecessor.
    for original in originals:
        receipt = await binding.execute(original, {**VALUES, "env.qa_library_skill": "qa-library"})
        require(
            receipt.status == "pending" and receipt.code == "ADMIN_RESOURCE_EDGE_UNBOUND",
            "missing skill edge acquired success",
        )
    require(not binding.trace.requests, "missing resource edge called a provider")


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
@pytest.mark.parametrize(
    "scenario_id",
    ["QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS", "QA-D7-BIG-WORKSPACE-CHECKPOINT"],
)
async def test_native_skill_pair_reaches_public_admin_and_stops_at_missing_edge(
    binding: AdminBindings, scenario_id: str
) -> None:
    values = {**VALUES, "env.qa_library_skill": "qa-library"}
    if binding.backend == "anthropic":
        pack = load_anthropic_fixtures(INDEX, source_root=REPO)
        plan, recipe = selected(pack, scenario_id)
        result = await execute_cell(
            plan, replay(recipe), native_recipe=recipe, admin=binding, admin_values=values
        )
    else:
        providers = load_provider_fixtures(CATALOG.parent / "index.json", catalog_root=CATALOG)
        scenario, fixture = providers.select(scenario_id, binding.backend)
        entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id).model_copy(
            update={"sha256": scenario.source.sha256}
        )
        plan = plan_scenario(entry, CATALOG, binding.backend, run_id="admin-native")
        result = await (
            execute_openai_cell if binding.backend == "openai" else execute_gemini_cell
        )(
            plan,
            scenario,
            fixture,
            source_root=CATALOG,
            repository_root=REPO,
            admin=binding,
            admin_values=values,
        )
    admin_receipts = [r for r in result.step_receipts if r.kind == "admin"]
    require(len(admin_receipts) == 2, "original add/remove steps were dropped")
    first = admin_receipts[0]
    require(first.code == "ADMIN_RESOURCE_EDGE_UNBOUND", "public edge was not consulted")
    require(
        first.admin is not None and first.admin.backend == binding.backend, "public receipt lost"
    )
    require(admin_receipts[1].code == "DEPENDENCY_BLOCKED", "cleanup invented a predecessor")
    require(result.reported_status == "PENDING", "missing skill edge conferred cell credit")
    require(
        result.request_count == (6 if binding.backend == "openai" else 3),
        "later turn escaped admin barrier",
    )
    require([t.turn for t in result.evidence.turns] == [1], "post-admin turn executed")
    require(not binding.trace.requests, "unbound skill edge called a provider")


async def test_common_admin_mutation_cannot_authorize_a_synthetic_host_context(
    tmp_path: Path, binding: AdminBindings
) -> None:
    row = source()
    row["setup"] = [admin_source("daimon tenants turn-cap discord {guild_id} 2")]
    plan = planned(tmp_path, row, binding.backend)

    async def prepare(_i: Invocation) -> None:
        await binding.configure_channel(CHANNEL)

    steps = await run_steps(
        plan, invoke=no_turn, prepare_channel=prepare, admin=binding, admin_values=VALUES
    )
    require(steps.receipts[0].status == "executed", "real cap was not applied")
    require(
        any(g.code == "ADMIN_HOST_CONTEXT_UNBOUND" for g in steps.gaps),
        "unbound product context was accepted",
    )
    require(
        all(r.status == "pending" for r in steps.receipts[2:]),
        "later turn/teardown escaped the barrier",
    )


@pytest.mark.parametrize("field", ["guild_id", "channel_id"])
async def test_foreign_admin_destination_refuses_before_the_ordered_turn(
    tmp_path: Path, binding: AdminBindings, field: str
) -> None:
    row = admin_row([admin_source("daimon channels budget clear discord {guild_id} {channel_id}")])
    plan = planned(tmp_path, row, binding.backend)
    with pytest.raises(ScopeViolation):
        await run_steps(
            plan,
            invoke=no_turn,
            prepare_channel=no_channel,
            admin=binding,
            admin_values={**VALUES, field: "foreign"},
        )
    require(not binding.trace.requests, "foreign admin called a provider")


async def test_selected_provider_mismatch_refuses_before_any_ordered_hook(
    tmp_path: Path, binding: AdminBindings
) -> None:
    provider: Provider = "openai" if binding.backend == "anthropic" else "anthropic"
    plan = planned(tmp_path, admin_row([admin_source("never execute")]), provider)
    with pytest.raises(ValueError, match="selected scenario provider"):
        await run_steps(
            plan, invoke=no_turn, prepare_channel=no_channel, admin=binding, admin_values=VALUES
        )


@pytest.mark.parametrize("mutation", ["backend", "operation", "status", "empty_readback"])
async def test_forged_admin_receipt_never_authorizes_a_later_turn(
    tmp_path: Path, binding: AdminBindings, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    receipt = AdminReceipt(
        status="executed",
        backend=binding.backend,
        operation="tenant_turn-cap",
        observations={"turn_cap": 2},
    )
    if mutation == "backend":
        receipt = receipt.model_copy(
            update={"backend": "openai" if binding.backend == "anthropic" else "anthropic"}
        )
    elif mutation == "operation":
        receipt = receipt.model_copy(update={"operation": "channel_skills_add"})
    elif mutation == "status":
        receipt = receipt.model_copy(update={"status": "invented"})
    else:
        receipt = receipt.model_copy(update={"observations": {}})

    async def forged(*_args: object, **_kwargs: object) -> AdminReceipt:
        return receipt

    monkeypatch.setattr(binding, "execute", forged)
    row = admin_row([admin_source("daimon tenants turn-cap discord {guild_id} 2")])
    plan = planned(tmp_path, row, binding.backend)
    with pytest.raises(ValueError):
        await run_steps(
            plan, invoke=no_turn, prepare_channel=no_channel, admin=binding, admin_values=VALUES
        )


def test_admin_readback_cannot_be_attached_to_a_turn_or_pending_success() -> None:
    receipt = AdminReceipt(
        status="executed",
        backend="anthropic",
        operation="tenant_turn-cap",
        observations={"turn_cap": 2},
    )
    for kind, status in (("admin", "pending"), ("mention", "executed")):
        with pytest.raises(ValueError):
            StepReceipt(
                location="steps[0]",
                kind=kind,
                status=cast(Literal["pending", "executed"], status),
                code="BOUND",
                started_s=1 if status == "executed" else None,
                finished_s=2 if status == "executed" else None,
                root_turn_id="root" if status == "executed" else None,
                turn=1,
                admin=receipt,
            )


@pytest.mark.parametrize("mutation", ["extra", "provider"])
async def test_catalog_admin_keys_cannot_reassign_the_frozen_cells(
    tmp_path: Path, binding: AdminBindings, mutation: str
) -> None:
    shutil.copytree(CATALOG, tmp_path, dirs_exist_ok=True)
    (tmp_path / "SCHEMA.md").write_text("offline cell-key admission proof")
    (tmp_path / "PROPOSED-KINDS.md").write_text("offline cell-key admission proof")
    scenario = "QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS"
    provider = binding.backend
    if mutation == "extra":
        scenario = "unscored-extra"
    else:
        provider = "openai" if binding.backend == "anthropic" else "anthropic"
    with pytest.raises(ValueError, match="frozen scenario/provider"):
        await execute_catalog(
            tmp_path,
            integration_sha="a" * 40,
            run_id="admin-keys",
            replays=(),
            admin_runs={(scenario, provider): AdminStepBinding(binding, VALUES)},
        )
    require(not binding.trace.requests, "foreign catalog key reached a provider")
