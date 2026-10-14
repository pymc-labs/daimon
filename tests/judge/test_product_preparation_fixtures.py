"""Fresh preparation tapes cross the real source/DB/SDK/consumer boundaries."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.testing.admin_bindings import AdminBindings
from daimon.testing.anthropic_fixtures import load_anthropic_fixtures
from daimon.testing.provider_replay import SourcePin
from mux.errors import ScopeViolation

from .anthropic_catalog_replay import AnthropicRecipe
from .catalog_product_context import ProductContext, ProductContextPending, ProductTurnRecipe
from .catalog_runner import ScenarioPlan, load_catalog, plan_scenario, sha
from .headless_executor import execute_cell
from .product_preparation_fixtures import (
    availability,
    build_preparation_index,
    load_preparation_index,
    provision_preparation_caller,
    publish_preparation,
)
from .test_anthropic_catalog import INDEX, REPO, replay
from .test_catalog_admin import CHANNEL, VALUES, require
from .test_catalog_admin import binding as binding

pytest_plugins = ["daimon.testing.db"]
CATALOG = INDEX.parent.parent / "catalog"
IDS = (
    "QA-D11-FOOTER-COST-MATCHES-LEDGER",
    "QA-NEW10-SPLIT-ANSWER-AND-CHUNK-REPLY",
    "QA-NEW37-MARKDOWN-TABLE-READABLE",
    "QA-NEW8-LINK-PAGE-THREAD-TITLE",
    "QA-NEW9-NO-INTERNAL-PLUMBING",
)


def selected(id_: str) -> tuple[ScenarioPlan, AnthropicRecipe]:
    pack = load_anthropic_fixtures(INDEX, source_root=REPO)
    entry, row = pack.select(id_)
    catalog = next(s for s in load_catalog(CATALOG) if s.id == id_)
    # The publisher independently verifies the public projection and original
    # authored pin against the immutable native input, not a claimed hash.
    catalog = catalog.model_copy(update={"sha256": row.scenario_sha256})
    return (
        plan_scenario(catalog, CATALOG, "anthropic", run_id="product-preparation"),
        AnthropicRecipe(entry, row, pack.index.codec, INDEX, REPO, sha(INDEX.read_bytes())),
    )


def owned_context(bound: AdminBindings, root: Path, plan: ScenarioPlan) -> ProductContext:
    return ProductContext(
        bound,
        {b.ref: CHANNEL for b in plan.bindings},
        {(i.channel or "", i.thread or ""): "323456789012345678" for i in plan.invocations},
        "423456789012345678",
        {},
        root,
        REPO,
    )


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
@pytest.mark.parametrize("id_", IDS)
async def test_published_fresh_recipe_executes_actual_product_turn_without_prewarming(
    binding: AdminBindings,
    tmp_path: Path,
    id_: str,
    record_property: Callable[[str, object], None],
) -> None:
    import uuid

    plan, native = selected(id_)
    context = owned_context(binding, tmp_path, plan)
    first = next(i for i in plan.invocations if i.operation == "host_turn")
    binding.default = DeploymentDefault(agent_name="daimon", environment_name="default")
    await context.configure(first)
    await provision_tenant(
        binding.sessions,
        platform="discord",
        workspace_id=binding.workspace_id,
        signup_credit=Decimal("10"),
    )
    caller_account = await provision_preparation_caller(context, plan, native, catalog_root=CATALOG)
    pin = await publish_preparation(context, plan, native, catalog_root=CATALOG)
    pin.verify(tmp_path)
    recipe = ProductTurnRecipe.model_validate_json((tmp_path / pin.path).read_bytes())
    # Main #824 (0733181847dc775bda444d51c2467016780b8d5d): one exact
    # post-turn GET; the global strict replay still rejects any extra call.
    catchups = [
        r
        for r in recipe.tape.replies
        if r.method == "GET" and r.path == f"/v1/sessions/{recipe.session_id}/events"
    ]
    require(len(catchups) == 1, "usage catch-up missing or broadened")
    require(
        catchups[0].query
        == (
            ("beta", "true"),
            ("limit", "100"),
            ("order", "desc"),
            ("types[]", "span.model_request_end"),
        )
        and catchups[0].request_json is None,
        "usage catch-up request changed",
    )
    require(
        catchups[0].response_json
        == {
            "data": [
                e for e in native.turn(1).turn.events if e["type"] == "span.model_request_end"
            ],
            "next_page": None,
        },
        "usage catch-up lost recorded model-request spans",
    )
    # Authoring must not leave a prepared mapping hidden behind the replay.
    async with binding.sessions() as db:
        row = await get_live_thread_session(
            db,
            tenant_id=uuid.UUID(binding.scope.tenant_id),
            platform="discord",
            thread_id="323456789012345678",
            account_id=uuid.UUID(caller_account),
        )
    require(row is None, "preparation recorder left a product mapping behind")
    context.sources = {1: SourcePin.model_validate_json(pin.model_dump_json())}
    result = await execute_cell(
        plan,
        replay(native),
        native_recipe=native,
        admin=binding,
        admin_values=VALUES,
        product_context=context,
    )
    require(len(result.evidence.turns) == 1, "consumer omitted the actual first prepared turn")
    observed = result.evidence.turns[0]
    context.verify_execution(first, observed)
    require(
        observed.root_turn_id == recipe.root_turn_id == native.turn(1).turn.events[0]["id"],
        "native root changed",
    )
    require(
        observed.session_id == recipe.session_id == native.session_id(1), "native session changed"
    )
    require(observed.terminal_capture_complete, "durable terminal missing")
    require(
        result.request_count == len(recipe.tape.replies), "not all preparation/turn wire consumed"
    )
    require(result.reported_status != "PASS", "unbound catalog observations gained false credit")
    record_property("scenario_id", id_)
    record_property("catalog_status", result.reported_status)
    record_property("actual_turns", len(result.evidence.turns))
    record_property("actual_request_count", result.request_count)
    with pytest.raises(ProductContextPending, match="empty owned thread"):
        await publish_preparation(context, plan, native, catalog_root=CATALOG)


@pytest.mark.parametrize("backend", ["openai", "gemini"])
def test_native_provider_preparation_is_typed_pending(backend: str) -> None:
    from typing import cast

    from mux.contracts.ids import Provider

    plan, native = selected(IDS[0])
    plan = plan_scenario(plan.scenario, CATALOG, cast(Provider, backend), run_id="prep")
    status = availability(plan, native, catalog_root=CATALOG)
    require(
        status.status == "PENDING" and "provider-native" in status.reason, "native false coverage"
    )


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
@pytest.mark.parametrize(
    "mutation", ["source", "text", "setup", "provider", "repository", "channel", "thread", "scope"]
)
async def test_refused_inputs_publish_no_tape_and_send_no_provider_requests(
    binding: AdminBindings,
    tmp_path: Path,
    mutation: str,
) -> None:
    plan, native = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")
    context = owned_context(binding, tmp_path, plan)
    if mutation == "source":
        plan = plan.model_copy(
            update={"scenario": plan.scenario.model_copy(update={"sha256": "0" * 64})}
        )
    elif mutation in {"text", "setup"}:
        if mutation == "text":
            invocations = tuple(
                i.model_copy(update={"text": "foreign"}) if i.operation == "host_turn" else i
                for i in plan.invocations
            )
            plan = plan.model_copy(update={"invocations": invocations})
        else:
            scenario = {
                **plan.scenario.scenario,
                "setup": [{"do": "admin", "args": "unrepresented"}],
            }
            plan = plan.model_copy(
                update={"scenario": plan.scenario.model_copy(update={"scenario": scenario})}
            )
    elif mutation == "provider":
        context.admin.backend = "gemini"
    elif mutation == "repository":
        context.repository_root = tmp_path
    elif mutation == "channel":
        context.channels = {}
    elif mutation == "thread":
        context.threads = {}
    else:
        context.admin.scope = context.admin.scope.model_copy(
            update={"account_id": "00000000-0000-0000-0000-000000000001"}
        )
    with pytest.raises((ValueError, ProductContextPending, ScopeViolation)):
        await publish_preparation(context, plan, native, catalog_root=CATALOG)
    require(not tuple(tmp_path.glob("*.json")), "refused input published a tape")
    require(not binding.trace.requests, "refused input reached the admin provider")


def test_all_159_cells_keep_source_pins_and_typed_pending(tmp_path: Path) -> None:
    index = build_preparation_index(catalog_root=CATALOG, repository_root=REPO)
    available = {c.scenario_id for c in index.cells if c.status == "AVAILABLE"}
    require(available == set(IDS), "availability slice changed silently")
    require(len(index.cells) == 159, "denominator changed")
    require(
        all(c.status == "PENDING" for c in index.cells if c.backend != "anthropic"),
        "native public boundary was fabricated",
    )
    for cell in index.cells:
        cell.source.verify(CATALOG)
    path = tmp_path / "index.json"
    path.write_text(index.model_dump_json())
    require(
        load_preparation_index(path, catalog_root=CATALOG, repository_root=REPO) == index,
        "JSON roundtrip lost source decisions",
    )
    forged = index.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"status": "AVAILABLE"}) if c.backend == "gemini" else c
                for c in index.cells
            )
        }
    )
    path.write_text(forged.model_dump_json())
    with pytest.raises(ValueError, match="differs from current"):
        load_preparation_index(path, catalog_root=CATALOG, repository_root=REPO)


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
async def test_missing_caller_identity_is_pending_without_provider_io(
    binding: AdminBindings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from . import product_preparation_fixtures as publisher

    plan, native = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")
    context = owned_context(binding, tmp_path, plan)
    await context.configure(next(i for i in plan.invocations if i.operation == "host_turn"))

    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing caller identity reached the provider")

    def handlers(*_handlers: object) -> Callable[[httpx.Request], httpx.Response]:
        return forbidden

    monkeypatch.setattr(publisher, "combine_handlers", handlers)
    with pytest.raises(ProductContextPending, match="caller identity"):
        await publish_preparation(context, plan, native, catalog_root=CATALOG)
    require(not tuple(tmp_path.glob("*.json")), "missing caller identity published a tape")


@pytest.mark.parametrize("binding", ["anthropic"], indirect=True)
@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "ANTHROPIC_WEBHOOK_SECRET", "OPENAI_CUSTOM_HEADERS", "GOOGLE_API_KEY"],
)
async def test_hostile_environment_refuses_before_recording(
    binding: AdminBindings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    from . import product_preparation_fixtures as publisher

    plan, native = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")
    context = owned_context(binding, tmp_path, plan)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ambient SDK refusal crossed the DB recording boundary")

    monkeypatch.setattr(publisher, "_rollback_sessions", forbidden)
    monkeypatch.setenv(name, "offline-sentinel-never-a-key")
    with pytest.raises(ValueError, match="ambient"):
        await publish_preparation(context, plan, native, catalog_root=CATALOG)
    require(not tuple(tmp_path.glob("*.json")), "ambient env published a tape")
    require(not binding.trace.requests, "ambient env reached provider")
