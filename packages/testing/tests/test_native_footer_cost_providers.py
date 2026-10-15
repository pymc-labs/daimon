"""Provider-host cases of test_native_footer_cost: they need the OpenAI/Gemini host modules,
so they land with the provider runtimes.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from daimon.core.config import TurnSettings
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.state import TurnState
from daimon.testing.d11_binding import D11TurnBinding
from daimon.testing.discord_surface import DiscordSurfaceCapture
from daimon.testing.footer_accounting import FooterAccountingHook
from daimon.testing.product_profiles import (
    DEFAULT_PRODUCT_PROFILE,
    OPT_IN_PASS,
    OPT_IN_PRODUCT_PROFILE,
)
from mux.contracts.ids import ModelRef

from . import test_footer_accounting as controls
from .test_footer_accounting import (
    MARKUP,
    Account,
    require_checks,
)
from .test_native_footer_cost import account as account
from .test_native_footer_cost import footer_engine as footer_engine
from .test_native_footer_cost import save_proof as save_proof
from .test_native_platform_footer import (
    REPO,
    Capture,
    NativeResult,
    Surface,
)


@pytest.mark.parametrize("surface", ["discord", "slack", "teams"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_opt_in_actual_native_sdk_host_d11_matches_posted_cost(
    account: Account,
    surface: Surface,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """Authored caller case: unchanged frozen predicates, real native host/SDK.

    Recorded native payloads and actual ledger/footer joins prove behavior;
    implementation provenance is not an admission gate. No live prices are claimed.
    """
    import uuid

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
    # Bind real host accounting before SDK dispatch; do not seed usage or costs
    # from a headless return. The driver calls this recorder for native snapshots.
    host = importlib.import_module("judge." + backend + "_catalog_host")
    original_run = host.run_turn
    sessions = account.hook.sessions
    captured_binding: list[ProviderBinding] = []
    d11_bindings: list[D11TurnBinding] = []

    async def billed_run(**kwargs: Any) -> TurnState:
        request = kwargs["backend_request"]
        native = request.session
        scope = request.scope
        assert native is not None
        lifecycle = kwargs["lifecycle"].delegate
        harness = lifecycle.harness if surface == "discord" else lifecycle.platform_harness
        assert harness is not None
        destination = "101" if surface == "discord" else harness.recorder.destination_id
        thread_id = (
            str(harness.thread.id)
            if surface == "discord"
            else (harness.recorder.thread_id or destination)
        )
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
        async with sessions() as db, db.begin():
            await make_tenant(db, id=tenant)
        await PostgresStateStore(sessions).put_binding(binding, expected_generation=0)
        captured_binding.append(binding)
        price = ProviderPrice(
            provider=backend,
            model=fixture.model,
            checked_on=controls.NOW.date(),
            input=Decimal(1),
            output=Decimal(5),
            cache_read=Decimal("0.1"),
        )
        profile = OPT_IN_PRODUCT_PROFILE if enabled else DEFAULT_PRODUCT_PROFILE
        opted = TurnSettings(native_footer_cost=profile.flags["core.turn.native_footer_cost"])
        assert opted.native_footer_cost is enabled
        output_id = (
            f"{plan.scenario.id}:{backend}:{native.id}:{fixture.turns[0].root_id}:headless_return"
        )
        # Capture ID is determined by the actual host lifecycle, not an output preview.
        capture_id = output_id.replace(":headless_return", f":{surface}_adapter_posts")
        d11 = D11TurnBinding(
            sessions=sessions,
            scope=scope,
            binding=binding,
            native_session=native,
            root_turn_id=fixture.turns[0].root_id,
            capture_id=capture_id,
            profile=profile,
            admitted_model=ModelRef(provider=backend, id=fixture.model),
            price=price,
            markup=MARKUP,
            platform_user_id="qa-caller",
            rate_evidence_id="controlled:dated-native-model-tariff",
            model_evidence_id="authored:admitted-request-model",
            infrastructure_policy="not_applicable",
            infrastructure_usd=Decimal(0),
            infrastructure_evidence_id="controlled:offline-no-container-charge",
        )
        d11_bindings.append(d11)
        billing = d11.billing()
        cost = billing.native_footer_cost
        result = await original_run(**{**kwargs, "billing": billing})
        if cost is not None:
            assert cost.result.status == "complete", cost.result
        return result

    monkeypatch.setattr(host, "run_turn", billed_run)
    # The existing unbilled catalog tapes have no final billed reconciliation.
    # Add exactly its native GET to this authored control, serving N3's saved
    # snapshot bytes unchanged. Every reply is consumed once and shape-checked.
    replay_module = importlib.import_module("judge." + backend + "_catalog_replay")
    original_tape = replay_module.host_tape
    from daimon.testing.provider_replay import WireReply

    def billed_tape(scenario, fixture, native_turn, **kwargs):
        tape = original_tape(scenario, fixture, native_turn, **kwargs)
        extra = WireReply(
            id="final-billed-usage-walk",
            method="GET",
            path=f"/v1/agents/sessions/{native_turn.session_id}/turns"
            if backend == "openai"
            else f"/v1beta/interactions/{native_turn.root_id}",
            query=(("order", "desc"), ("limit", "100")) if backend == "openai" else (),
            headers=(("openai-beta", "agents=v1"),)
            if backend == "openai"
            else (("api-revision", "2026-05-20"),),
            response_json={"data": [native_turn.snapshot], "has_more": False}
            if backend == "openai"
            else native_turn.snapshot,
            after=(tape.replies[-1].id,),
        )
        return tape.model_copy(update={"replies": (*tape.replies, extra)})

    monkeypatch.setattr(replay_module, "host_tape", billed_tape)
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
    assert captured_binding == [binding]
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
    (d11,) = d11_bindings
    evaluated = await d11.evaluate(
        evidence=result.evidence,
        capture=capture,
        receipts=(observation,),
        assertions=frozen["assert"],
        inventory=TurnUsageInventory(
            binding_id=binding.id,
            session=native,
            root_turn_id=native_turn.root_id,
            revisions=((observation.id, observation.revision),),
            evidence_id="authored:native-single-terminal-inventory",
            kind="terminal_native_inventory",
        ),
    )
    report = evaluated.accounting
    assert evaluated.status == (OPT_IN_PASS if enabled else "FAIL")
    assert [check.status for check in evaluated.outcome.checks] == (
        ["PASS"] * 4 if enabled else ["PASS", "FAIL", "FAIL", "PASS"]
    )
    require_checks(
        report,
        ("PASS", "CONTROLLED_POSTED_COST_MATCH") if enabled else ("FAIL", "POSTED_COST_MISSING"),
        ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"),
    )
    assert result.request_count == (7 if backend == "openai" else 4)
    if not capture.post_capture_complete or not capture.text_capture_complete:
        raise RuntimeError("D11 native failure does not have complete actual posts")
    if report.ledger_usd is None or report.ledger_usd != report.controlled_expected_usd:
        raise RuntimeError("controlled native usage inventory remained incomplete")
    if await hook.snapshot() != before:
        raise RuntimeError("native footer read manufactured accounting facts")
    save_proof(
        tmp_path,
        backend,
        capture,
        report,
        native_sdk=True,
        request_count=result.request_count,
        opt_in=enabled,
        binding_result=evaluated,
    )


@pytest.mark.parametrize(
    ("url", "skipped"),
    [
        ("postgresql+asyncpg://localhost/daimon_test", True),
        ("postgresql+asyncpg://remote.invalid/daimon_test_nc_N5", False),
    ],
)
def test_native_preparation_admission_precedes_any_engine_fixture(
    tmp_path: Path, url: str, skipped: bool
) -> None:
    trap = tmp_path / "forbidden-fixture-step"
    plugin = tmp_path / "n5_footer_connection_trap.py"
    plugin.write_text(
        "from pathlib import Path\n"
        "from sqlalchemy import event\n"
        "from daimon.testing import db\n"
        f"trap = Path({str(trap)!r})\n"
        "def forbid(*args, **kwargs):\n"
        "    trap.write_text('connection or destructive fixture reached')\n"
        "    raise RuntimeError('N5_CONNECTION_OR_CLEANUP_TRAP')\n"
        "async def forbid_async(*args, **kwargs):\n"
        "    forbid()\n"
        "def pytest_sessionstart(session):\n"
        "    original = db.build_test_engine\n"
        "    def build(*args, **kwargs):\n"
        "        engine = original(*args, **kwargs)\n"
        "        event.listen(engine.sync_engine, 'do_connect', forbid)\n"
        "        return engine\n"
        "    db.build_test_engine = build\n"
        "    db.sweep_orphan_schemas = forbid_async\n"
        "    db._create_schema_with_tables = forbid_async\n"
        "    db.truncate_all = forbid_async\n"
    )
    environment = {"DAIMON_DATABASE__TEST_URL": url, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            plugin.stem,
            "-p",
            "no:cacheprovider",
            "-q",
            "-rs",
            str(REPO / "packages/core/tests/turn/test_native_footer_preparation.py"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    if trap.exists() or "N5_CONNECTION_OR_CLEANUP_TRAP" in output:
        raise RuntimeError("footer fixture connected or swept/truncated before admission")
    if skipped:
        if result.returncode != 0 or "6 skipped" not in output or "owned lane" not in output:
            raise RuntimeError(f"contributor database was not skipped before setup: {output}")
    elif result.returncode != 1 or "require owned local lane engines" not in output:
        raise RuntimeError(f"foreign database was not refused before setup: {output}")
