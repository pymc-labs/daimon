"""Provider-host cases of test_native_platform_footer: they need the OpenAI/Gemini host modules,
so they land with the provider runtimes.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.testing.discord_surface import DiscordSurfaceCapture
from daimon.testing.footer_accounting import FooterAccountingHook
from daimon.testing.turn_accounting import RevisionRateAttribution
from mux.contracts.ids import ModelRef

from . import test_footer_accounting as controls
from .test_footer_accounting import (
    MARKUP,
    Account,
    require_checks,
)
from .test_native_platform_footer import REPO as REPO
from .test_native_platform_footer import Capture as Capture
from .test_native_platform_footer import NativeResult as NativeResult
from .test_native_platform_footer import Surface as Surface
from .test_native_platform_footer import account as account
from .test_native_platform_footer import footer_engine as footer_engine
from .test_native_platform_footer import save_d11_proof as save_d11_proof


@pytest.mark.parametrize("surface", ["discord", "slack", "teams"])
async def test_actual_native_sdk_host_footer_joins_complete_controlled_inventory(
    account: Account, surface: Surface, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authored caller case: unchanged frozen predicates, real native host/SDK.

    Recorded native payloads and actual ledger/footer joins prove behavior;
    implementation provenance is not an admission gate. No live prices are claimed.
    """
    import uuid

    from daimon.core import accounting_outbox
    from daimon.core.pricing import ProviderPrice
    from daimon.testing.factories import make_tenant
    from daimon.testing.provider_fixtures import load_provider_fixtures
    from daimon.testing.provider_replay import SourcePin
    from daimon.testing.turn_accounting import TurnUsageInventory
    from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
    from mux.contracts.resources import ProviderBinding
    from mux.drivers.gemini.usage import observation_from_interaction
    from mux.drivers.openai.usage import observation as openai_observation

    # The judge package is a source-only optional consumer, not an installed
    # testing dependency. Load its public callable through an explicit local path.
    monkeypatch.syspath_prepend(str(REPO / "tests"))  # pyright: ignore[reportUnknownMemberType]
    executor = importlib.import_module("judge.headless_executor")
    planner = importlib.import_module("judge.test_catalog_runner")

    backend = account.observation.session.provider
    if backend == "anthropic":
        pytest.skip(
            "Real native SDK caller proof is OpenAI/Gemini; Anthropic adapter control above"
        )
    source_path = (
        REPO
        / "packages/testing/fixtures/target53/catalog/scenarios/QA-D11-FOOTER-COST-MATCHES-LEDGER.yaml"
    )
    frozen = cast(dict[str, Any], yaml.safe_load(source_path.read_text()))
    row = {**frozen, "surface": surface}
    plan = planner.planned(tmp_path, row, backend)
    # The source-only planner uses a readable qa-* tenant. Admit a real owned
    # fixture UUID before native dispatch so the same identity can join SQL.
    # Its own 8-hex prefix: tenant ids must differ in their first 8 hex chars.
    tenant = uuid.UUID(int=506 << 96)
    plan = plan.model_copy(
        update={
            "bindings": tuple(
                binding.model_copy(
                    update={
                        "revision": binding.revision.model_copy(
                            update={
                                "channel": binding.revision.channel.model_copy(
                                    update={"tenant_id": str(tenant)}
                                )
                            }
                        )
                    }
                )
                for binding in cast(tuple[Any, ...], plan.bindings)
            )
        }
    )
    index = REPO / "packages/testing/fixtures/target53/index.json"
    pack = load_provider_fixtures(index, catalog_root=index.parent / "catalog")
    original = next(s for s in pack.scenarios if s.scenario_id == frozen["id"])
    fixture = next(p for p in original.providers if p.backend == backend)
    pin = SourcePin(path=f"scenarios/{plan.scenario.filename}", sha256=plan.scenario.sha256)
    scenario = original.model_copy(
        update={
            "source": pin,
            "projection": pin,
            "scenario": row,
            "providers": tuple(fixture if p.backend == backend else p for p in original.providers),
        }
    )
    execute = cast(
        Callable[..., Awaitable[NativeResult]],
        getattr(executor, "execute_openai_cell" if backend == "openai" else "execute_gemini_cell"),
    )
    result = await execute(plan, scenario, fixture, source_root=tmp_path, repository_root=REPO)
    capture: Capture
    if surface == "discord":
        (capture,) = result.discord_surfaces
    else:
        (capture,) = result.platform_surfaces
    (native_turn,) = fixture.turns
    (turn,) = result.evidence.turns
    (ended,) = turn.terminals
    if ended.authority != "record" or ended.outcome != "completed":
        raise RuntimeError("native host lacks a recorded terminal")
    if (capture.session_id, capture.root_turn_id) != (native_turn.session_id, native_turn.root_id):
        raise RuntimeError("actual post capture differs from the native accepted root")
    native = ResourceRef(
        id=native_turn.session_id,
        kind="session",
        provider=backend,
        account_scope_id="qa-offline-project",
        tenant_id=str(tenant),
        account_id="qa-caller",
    )
    model = ModelRef(provider=backend, id=fixture.model)
    # Match host record_provider_usage: bind the separately admitted session
    # model when the native snapshot omits it; preserve every native meter/version.
    observation = (
        openai_observation(dict(native_turn.snapshot), native, revision=1).model_copy(
            update={"model": model}
        )
        if backend == "openai"
        else observation_from_interaction(
            dict(native_turn.snapshot),
            native,
            revision=1,
            observed_at=controls.NOW,
            root_turn=native_turn.root_id,
            model=model,
        )
    )
    price = ProviderPrice(
        provider=backend,
        model=model.id,
        checked_on=controls.NOW.date(),
        input=Decimal(1),
        output=Decimal(5),
        cache_read=Decimal("0.1"),
    )
    if isinstance(capture, DiscordSurfaceCapture):
        destination, thread_id = "101", capture.messages[0].channel_id
    else:
        destination = capture.messages[0].destination_id
        thread_id = capture.messages[0].thread_id or destination
    binding = ProviderBinding(
        id="native-sdk-binding",
        provider=backend,
        profile=fixture.profile,
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=str(tenant), platform=surface, channel_id=destination),
            thread_id=thread_id,
        ),
        native_refs={"session": native.id},
        generation=1,
        config_revision=1,
    )
    sessions = account.hook.sessions
    async with sessions() as db, db.begin():
        await make_tenant(db, id=tenant)
    await PostgresStateStore(sessions).put_binding(binding, expected_generation=0)
    # Controlled offline settlement of the actual served native revision, never
    # an aggregate manufactured from request spans or a live spend assertion.
    async with sessions() as db, db.begin():
        await accounting_outbox.record_observation_usage(
            db,
            binding_id=binding.id,
            observation=observation,
            tenant_id=tenant,
            platform_user_id="qa-caller",
            channel_id=destination,
            markup=MARKUP,
            pricing=None,
            provider_price=price,
            infrastructure_usd=Decimal(0),
            billing_grain=observation.grain,
        )
    hook = FooterAccountingHook(
        sessions=sessions,
        scope=Scope(
            tenant_id=str(tenant),
            account_id="qa-caller",
            principal_id="offline-catalog-host",
            authorization_id="scripted-fixture",
        ),
        binding=binding,
        native_session=native,
        root_turn_id=native_turn.root_id,
    )
    before = await hook.snapshot()
    report = await hook.evaluate_turn(
        capture=capture,
        terminal=ended,
        receipts=(observation,),
        markup=MARKUP,
        attributions=(
            RevisionRateAttribution(
                observation_id=observation.id,
                revision=observation.revision,
                price=price,
                rate_evidence_id="controlled:dated-native-model-tariff",
                infrastructure_policy="not_applicable",
                infrastructure_usd=Decimal(0),
                infrastructure_evidence_id="controlled:offline-no-container-charge",
            ),
        ),
        inventory=TurnUsageInventory(
            binding_id=binding.id,
            session=native,
            root_turn_id=native_turn.root_id,
            revisions=((observation.id, observation.revision),),
            evidence_id="authored:native-single-terminal-inventory",
            kind="terminal_native_inventory",
        ),
    )
    require_checks(
        report, ("FAIL", "POSTED_COST_MISSING"), ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH")
    )
    if not capture.post_capture_complete or not capture.text_capture_complete:
        raise RuntimeError("D11 native failure does not have complete actual posts")
    if report.ledger_usd is None or report.ledger_usd != report.controlled_expected_usd:
        raise RuntimeError("controlled native usage inventory remained incomplete")
    if await hook.snapshot() != before:
        raise RuntimeError("native footer read manufactured accounting facts")
    save_d11_proof(
        tmp_path, backend, capture, report, native_sdk=True, request_count=result.request_count
    )
