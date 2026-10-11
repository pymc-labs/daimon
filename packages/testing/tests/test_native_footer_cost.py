"""Opt-in native cost from real posts and independent owned accounting reads.

Controlled offline arithmetic only; source ingress and live spend are not
certified. Default-provider outputs and frozen assertions remain unchanged.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from daimon.core.config import TurnSettings
from daimon.core.native_footer_cost import NativeFooterCostContext, native_footer_text
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.state import TurnState
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.d11_binding import D11Result, D11TurnBinding
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
from .test_native_platform_footer import (
    REPO,
    Capture,
    NativeResult,
    Surface,
    actual_posts,
    joined,
    routed,
)

account = controls.account
footer_engine = controls.footer_engine


def context(value: Account) -> NativeFooterCostContext:
    return NativeFooterCostContext(
        value.hook.sessions, value.hook.scope, value.hook.binding, value.hook.native_session, MARKUP
    )


def footer_strings(capture: Capture) -> tuple[str, ...]:
    if isinstance(capture, DiscordSurfaceCapture):
        return tuple(
            json.loads(m.payload_json).get("embeds", [{}])[0].get("footer", {}).get("text", "")
            for m in capture.messages
            if json.loads(m.payload_json).get("embeds")
        )
    if capture.platform == "slack":
        return tuple(
            e["text"]
            for m in capture.messages
            for b in json.loads(m.payload_json).get("blocks", [])
            if b.get("type") == "context"
            for e in b.get("elements", [])
        )
    return tuple(t for m in capture.messages for t in m.card_texts if "s" in t)


@pytest.mark.parametrize("surface", ["discord", "slack", "teams"])
async def test_opt_in_component_posts_complete_cost_and_independent_ledger(
    account: Account, surface: Surface, tmp_path: Path
):
    value = await routed(account, surface)
    await value.record()
    enabled = TurnSettings(
        native_footer_cost=OPT_IN_PRODUCT_PROFILE.flags["core.turn.native_footer_cost"]
    )
    before = await actual_posts(value, surface)
    native = value.observation.session.provider != "anthropic"
    cost = context(value) if enabled.native_footer_cost and native else None
    if cost is not None:
        cost.note(value.observation, price=value.price, infrastructure_usd=Decimal(0), settled=True)
        cost.reconciled((value.observation,))
        await cost.finish(authoritative_terminal=True)
        assert cost.result.status == "complete", cost.result
    with cost.activate() if cost is not None else nullcontext():
        capture = await actual_posts(value, surface)
    report = await joined(value, capture)
    require_checks(
        report,
        ("PASS", "CONTROLLED_POSTED_COST_MATCH"),
        ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"),
    )
    if not native:
        assert footer_strings(capture) == footer_strings(before)
    else:
        assert "used" not in " ".join(footer_strings(before))
        assert "$0.012 used" in " ".join(footer_strings(capture))
    save_proof(tmp_path, value.observation.session.provider, capture, report, native_sdk=False)


@pytest.mark.parametrize(
    "problem",
    [
        "price",
        "unverified",
        "future_rate",
        "tokens",
        "infra",
        "container_time",
        "coverage",
        "terminal",
        "settlement",
        "aggregate",
    ],
)
async def test_incomplete_native_cost_posts_explicit_text_not_a_subtotal(
    account: Account, problem: str
):
    if account.observation.session.provider == "anthropic":
        pytest.skip("native-only cost completeness; Anthropic preservation above")
    await account.record()
    cost = context(account)
    usage = account.observation
    price = account.price
    if problem == "tokens":
        usage = usage.model_copy(update={"input_tokens": None})
    elif problem == "aggregate":
        usage = usage.model_copy(update={"covers": ("unreturned-child",)})
    if problem == "unverified":
        price = replace(price, verified=False)
    elif problem == "future_rate":
        price = replace(price, checked_on=usage.observed_at.date() + timedelta(days=1))
    cost.note(
        usage,
        price=None if problem == "price" else price,
        infrastructure_usd=None
        if problem == "infra"
        else Decimal(1)
        if problem == "container_time"
        else Decimal(0),
        settled=problem != "settlement",
    )
    if problem != "coverage":
        cost.reconciled((usage,))
    await cost.finish(authoritative_terminal=problem != "terminal")
    assert cost.result.amount is None and cost.result.status != "complete"
    with cost.activate():
        capture = await actual_posts(account, "discord")
    footer = " ".join(footer_strings(capture))
    assert "Cost partial" in footer or "Cost unknown" in footer
    assert "$" not in footer and "partial used" not in footer and "unknown used" not in footer


async def test_replacement_attempt_does_not_inherit_a_numeric_cost(account: Account):
    if account.observation.session.provider == "anthropic":
        pytest.skip("native root reset control")
    await account.record()
    old = context(account)
    old.note(account.observation, price=account.price, infrastructure_usd=Decimal(0), settled=True)
    old.reconciled((account.observation,))
    await old.finish(authoritative_terminal=True)
    totals = ProviderUsageTotals().add_observation(account.observation)
    with old.activate():
        assert native_footer_text(totals) == "$0.012"
        with context(account).activate():
            assert native_footer_text(totals) == "Cost unknown"
        assert native_footer_text(totals) == "$0.012"
        newer = account.observation.model_copy(update={"revision": 2})
        assert native_footer_text(ProviderUsageTotals().add_observation(newer)) == "Cost partial"
    assert native_footer_text(totals) is None


def test_anthropic_cannot_construct_native_cost_context(account: Account):
    if account.observation.session.provider != "anthropic":
        pytest.skip("Anthropic admission refusal control")
    with pytest.raises(ValueError, match="authorized native tenant binding"):
        context(account)


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

    The current reviewed Gemini codec is pinned explicitly in this private case;
    no frozen fixture or pin is rewritten and no live prices are claimed.
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
    tenant = uuid.UUID(int=506)
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
    if backend == "gemini":
        # Reviewed merged #838 (e8dcf6bda4ea3553c59b8095fc8b416fe3808226); private case only.
        fixture = fixture.model_copy(
            update={
                "codec_commit": "e8dcf6bda4ea3553c59b8095fc8b416fe3808226",
                "codec_source": SourcePin(
                    path="packages/core/daimon/core/turn/gemini.py",
                    sha256="f1f2c74600aa819a181cb1c33c54e0386cfa990c4f52de4a6361f9632abd9546",
                ),
            }
        )
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


def save_proof(
    directory: Path,
    provider: str,
    capture: Capture,
    report,
    *,
    native_sdk: bool,
    request_count: int = 0,
    opt_in: bool = True,
    binding_result: D11Result | None = None,
):
    source = yaml.safe_load(
        (
            REPO
            / "packages/testing/fixtures/target53/catalog/scenarios/QA-D11-FOOTER-COST-MATCHES-LEDGER.yaml"
        ).read_text()
    )
    texts = "\n".join(
        part
        for m in capture.messages
        for part in (
            (m.content, *m.embed_texts)
            if isinstance(capture, DiscordSurfaceCapture)
            else (*m.body_texts, *m.card_texts)
        )
    )
    checks = [
        {
            "kind": requirement["kind"],
            "status": "PASS" if re.search(requirement["pattern"], texts) else "FAIL",
        }
        for requirement in source["assert"][:2]
    ] + [check.model_dump(mode="json") for check in report.checks]
    if binding_result is not None:
        checks = [check.model_dump(mode="json") for check in binding_result.outcome.checks]
    expected = (
        ["PASS"] * 4 if provider == "anthropic" or opt_in else ["PASS", "FAIL", "FAIL", "PASS"]
    )
    assert [v["status"] for v in checks] == expected
    (
        directory
        / f"n5-{'opt-in' if opt_in else 'default'}-d11-{provider}-{getattr(capture, 'platform', 'discord')}.json"
    ).write_text(
        json.dumps(
            {
                "provider": provider,
                "profile": "opt-in-product" if opt_in else "default",
                "product_flags": {"turn.native_footer_cost": opt_in},
                "status": "PASS" if provider == "anthropic" else OPT_IN_PASS if opt_in else "FAIL",
                "boundary": "controlled_offline_native_sdk_host"
                if native_sdk
                else "controlled_offline_adapter_component",
                "frozen_assertions": source["assert"],
                "checks": checks,
                "public_binding_outcome": binding_result.outcome.model_dump(mode="json")
                if binding_result is not None
                else None,
                "public_binding_evidence": binding_result.evidence.model_dump(mode="json")
                if binding_result is not None
                else None,
                "actual_accepted_posts": capture.model_dump(mode="json"),
                "ledger_usd": str(report.ledger_usd),
                "controlled_expected_usd": str(report.controlled_expected_usd),
                "request_count": request_count,
                "source_ingress_certified": False,
                "live_spend_certified": False,
                "actual_live_usd": "0",
                "held_live_usd": "0",
            },
            indent=2,
        )
    )


@pytest.mark.parametrize("zero", [False, True])
async def test_complete_zero_or_corrected_current_revision_uses_actual_settled_amount(
    account: Account, zero: bool
):
    if account.observation.session.provider == "anthropic":
        pytest.skip("native exact Decimal revision arithmetic control")
    cost = context(account)
    original = account.observation
    if zero:
        original = original.model_copy(
            update={"input_tokens": 0, "input_cached_tokens": 0, "output_tokens": 0}
        )
    await account.record(original)
    cost.note(original, price=account.price, infrastructure_usd=Decimal(0), settled=True)
    current = original
    if not zero:
        current = original.model_copy(update={"revision": 2, "output_tokens": 2000})
        await account.record(current)
        cost.note(current, price=account.price, infrastructure_usd=Decimal(0), settled=True)
    # Native snapshot walk may include revisions. Selection must replace them,
    # never sum both the original debit and corrected total.
    cost.reconciled((original, current))
    await cost.finish(authoritative_terminal=True)
    assert cost.result.status == "complete", cost.result
    assert cost.result.amount == (Decimal(0) if zero else Decimal("0.022000"))
    with cost.activate():
        totals = ProviderUsageTotals().add_observation(current)
        assert native_footer_text(totals) == ("<$0.001" if zero else "$0.022")
        changed = current.model_copy(update={"output_tokens": 999})
        assert native_footer_text(ProviderUsageTotals().add_observation(changed)) == "Cost partial"


async def test_complete_native_footer_ignores_low_caller_decimal_precision(account: Account):
    if account.observation.session.provider == "anthropic":
        pytest.skip("native exact Decimal ledger sum control")
    assert account.observation.model is not None
    value = replace(
        account,
        observation=controls.normalize(
            account.observation.session.provider,
            account.observation.session,
            account.observation.model,
            incoming=617283,
            output=100,
        ),
    )
    await value.record()
    cost = context(value)
    cost.note(value.observation, price=value.price, infrastructure_usd=Decimal(0), settled=True)
    cost.reconciled((value.observation,))
    with localcontext() as caller:
        caller.prec = 6
        await cost.finish(authoritative_terminal=True)
        assert cost.result.status == "complete", cost.result
        assert cost.result.amount == Decimal("1.235566")
        with cost.activate():
            capture = await actual_posts(value, "discord")
        assert "$1.236 used" in " ".join(footer_strings(capture))


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


@pytest.mark.parametrize("missing", ["rates", "inventory"])
async def test_public_d11_binding_incomplete_accounting_stays_typed(account: Account, missing: str):
    from daimon.testing.outcome_oracle import RunEvidence, TurnEvidence
    from daimon.testing.turn_accounting import TurnUsageInventory

    if account.observation.session.provider == "anthropic":
        pytest.skip("D11 binding is native-only; existing Anthropic controls remain unchanged")
    capture = await account.capture()
    model = account.observation.model
    assert model is not None
    binding = D11TurnBinding(
        sessions=account.hook.sessions,
        scope=account.hook.scope,
        binding=account.hook.binding,
        native_session=account.hook.native_session,
        root_turn_id=account.hook.root_turn_id,
        capture_id=capture.evidence_id,
        profile=DEFAULT_PRODUCT_PROFILE,
        admitted_model=model,
        price=account.price,
        markup=MARKUP,
        platform_user_id="user",
        rate_evidence_id=None if missing == "rates" else "controlled:rate",
        model_evidence_id="controlled:admitted-model",
        infrastructure_policy="not_applicable",
        infrastructure_usd=Decimal(0),
        infrastructure_evidence_id="controlled:no-container",
    )
    billing = binding.billing()
    settled = await billing.record(observation=account.observation)
    assert settled is (missing != "rates")
    before = await account.hook.snapshot()
    evidence = RunEvidence(
        scenario_id="QA-D11-FOOTER-COST-MATCHES-LEDGER",
        backend=account.observation.session.provider,
        turns=(
            TurnEvidence(
                turn=1,
                slot_id="slot",
                session_id=capture.session_id,
                root_turn_id=capture.root_turn_id,
                started_s=capture.started_s,
                terminals=(controls.terminal(observed_s=capture.observed_s),),
                terminal_capture_complete=True,
            ),
        ),
    )
    source = yaml.safe_load(
        (
            REPO
            / "packages/testing/fixtures/target53/catalog/scenarios/QA-D11-FOOTER-COST-MATCHES-LEDGER.yaml"
        ).read_text()
    )
    result = await binding.evaluate(
        evidence=evidence,
        capture=capture,
        receipts=(account.observation,),
        assertions=source["assert"],
        inventory=None
        if missing == "inventory"
        else TurnUsageInventory(
            binding_id=account.hook.binding.id,
            session=account.hook.native_session,
            root_turn_id=account.hook.root_turn_id,
            revisions=((account.observation.id, account.observation.revision),),
            evidence_id="native:closed-turn-inventory",
            kind="terminal_native_inventory",
        ),
    )
    assert all(c.status == "PENDING" for c in result.accounting.checks)
    assert all(c.code for c in result.accounting.checks)
    # The frozen text requirement still executes against the actual cost-free
    # post. Incomplete accounting never turns that observed failure into PASS.
    assert result.status == "FAIL"
    assert result.outcome.checks[1].status == "FAIL"
    assert await account.hook.snapshot() == before
