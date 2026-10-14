"""Opt-in native cost from real posts and independent owned accounting reads.

Controlled offline arithmetic only; source ingress and live spend are not
certified. Default-provider outputs and frozen assertions remain unchanged.
"""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
import yaml
from daimon.core.config import TurnSettings
from daimon.core.native_footer_cost import NativeFooterCostContext, native_footer_text
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.d11_binding import D11Result, D11TurnBinding
from daimon.testing.discord_surface import DiscordSurfaceCapture
from daimon.testing.product_profiles import (
    DEFAULT_PRODUCT_PROFILE,
    OPT_IN_PASS,
    OPT_IN_PRODUCT_PROFILE,
)

from . import test_footer_accounting as controls
from .test_footer_accounting import (
    MARKUP,
    Account,
    require_checks,
)
from .test_native_platform_footer import (
    REPO,
    Capture,
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
