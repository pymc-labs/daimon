"""Model policy refuses even priced alternatives, before callback/key access."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from mux.conformance.budget import (
    LIVE_MODEL_ALLOWLIST,
    BudgetRefused,
    ModelPrice,
    ProbePlan,
    TokenLimits,
    TokenUsage,
)
from mux.conformance.live_probe import ProbeOutcome, run_probe
from mux.conformance.recording import Recorder
from mux.conformance.test_budget import receipts, setup_guard

APPROVED = (
    ("openai", "gpt-6-luna"),
    ("anthropic", "claude-haiku-5-5"),
    ("gemini", "gemini-3.8-flash"),
    ("gemini", "gemini-flash-latest"),
    ("gemini", "gemini-3.5-flash-lite"),
)


def test_live_policy_contains_only_the_explicit_approved_models() -> None:
    assert dict(LIVE_MODEL_ALLOWLIST) == {
        "openai": frozenset({"gpt-6-luna"}),
        "anthropic": frozenset({"claude-haiku-5-5"}),
        "gemini": frozenset({"gemini-3.8-flash", "gemini-flash-latest", "gemini-3.5-flash-lite"}),
    }


def test_reviewed_haiku_price_bounds_both_prompt_tiers_and_cache_durations() -> None:
    # Official pricing table, USD/MTok: use the >100k tier and 1h cache write.
    # https://platform.claude.com/docs/en/about-claude/pricing#model-pricing
    price = ModelPrice(
        input=Decimal("0.5"),
        cached_input=Decimal("0.05"),
        cache_write_input=Decimal("1"),
        output=Decimal("2.5"),
    )
    limits = TokenLimits(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price.reserve(limits) == Decimal("3.5")
    for cached, writes, expected in (
        (0, 0, "3"),
        (1_000_000, 0, "2.55"),
        (0, 1_000_000, "3.5"),
    ):
        usage = TokenUsage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            input_cached_tokens=cached,
            input_cache_write_tokens=writes,
        )
        actual = price.estimate(usage)
        assert actual is not None and actual == Decimal(expected)
        assert price.reserve(limits) >= actual


@pytest.mark.parametrize(("provider", "model"), APPROVED)
@pytest.mark.parametrize("priced", [True, False])
def test_approved_model_requires_reviewed_price(
    tmp_path: Path, provider: str, model: str, priced: bool
) -> None:
    guard = setup_guard(tmp_path)
    config = json.loads(guard.config_path.read_text())
    rates = config["providers"]["openai"]["models"]["gpt-6-luna"]
    config["providers"] = {provider: {"cap_usd": "10", "models": {model: rates} if priced else {}}}
    guard.config_path.write_text(json.dumps(config))
    probe = ProbePlan(
        provider=provider,
        model=model,
        fixture_id="C16",
        limits=TokenLimits(input_tokens=1_000_000, output_tokens=0),
    )
    if priced:
        assert guard.reserve(probe).receipt.reserved_usd == 1
    else:
        with pytest.raises(BudgetRefused):
            guard.reserve(probe)
        assert receipts(guard)[-1].status == "blocked"
        assert receipts(guard)[-1].reserved_usd == 0
        assert receipts(guard)[-1].cost_estimate_usd == 0


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-6-sol"),
        ("openai", "gpt-6-luna-latest"),
        ("openai", "claude-haiku-4-5-20251001"),
        ("anthropic", "claude-sonnet-5-5"),
        ("anthropic", "claude-haiku-4-5-latest"),
        ("anthropic", "claude-haiku-4-5"),
        ("anthropic", "claude-haiku-4-5-20251001"),
        ("anthropic", "claude-haiku-5-5-latest"),
        ("anthropic", "CLAUDE-HAIKU-5-5"),
        ("gemini", "gemini-3.5-pro"),
        ("gemini", "gemini-3.5-flash"),
        ("gemini", "gemini-3.5-flash-latest"),
        ("gemini", "gemini-3.8-flash-latest"),
        ("gemini", "models/gemini-3.5-flash-lite"),
        ("unknown", "gpt-6-luna"),
    ],
)
async def test_priced_disallowed_model_cannot_reach_key_access(
    tmp_path: Path, provider: str, model: str
) -> None:
    guard = setup_guard(tmp_path)
    config = json.loads(guard.config_path.read_text())
    rates = config["providers"]["openai"]["models"]["gpt-6-luna"]
    config["providers"] = {provider: {"cap_usd": "10", "models": {model: rates}}}
    guard.config_path.write_text(json.dumps(config))
    probe = ProbePlan(
        provider=provider,
        model=model,
        fixture_id="C16",
        limits=TokenLimits(input_tokens=1_000_000, output_tokens=0),
    )
    key_accesses = 0

    async def access_key(recorder: Recorder) -> ProbeOutcome:
        nonlocal key_accesses
        key_accesses += 1
        raise RuntimeError("offline key-access sentinel")

    with pytest.raises(BudgetRefused):
        await run_probe(guard, probe, tmp_path / "refused.json", access_key)
    assert key_accesses == 0
    assert not (tmp_path / "refused.json").exists()
    receipt = receipts(guard)[-1]
    assert receipt.status == "blocked" and receipt.reason == "unconfigured"
    assert receipt.reserved_usd == receipt.cost_estimate_usd == 0
    checkpoint = json.loads(guard.checkpoint_path.read_text())
    assert all(value == "0" for value in checkpoint["provider_totals"].values())


@pytest.mark.parametrize(("provider", "model"), APPROVED)
async def test_unpriced_approved_model_cannot_reach_key_access(
    tmp_path: Path, provider: str, model: str
) -> None:
    guard = setup_guard(tmp_path)
    config = json.loads(guard.config_path.read_text())
    config["providers"] = {provider: {"cap_usd": "10", "models": {}}}
    guard.config_path.write_text(json.dumps(config))
    key_accesses = 0

    async def access_key(recorder: Recorder) -> ProbeOutcome:
        nonlocal key_accesses
        key_accesses += 1
        raise RuntimeError("offline key-access sentinel")

    plan = ProbePlan(
        provider=provider,
        model=model,
        fixture_id="C16",
        limits=TokenLimits(input_tokens=1_000_000, output_tokens=0),
    )
    with pytest.raises(BudgetRefused):
        await run_probe(guard, plan, tmp_path / "unpriced.json", access_key)
    assert key_accesses == 0
    assert not (tmp_path / "unpriced.json").exists()
    receipt = receipts(guard)[-1]
    assert receipt.status == "blocked" and receipt.reason == "unconfigured"
    assert receipt.reserved_usd == receipt.cost_estimate_usd == 0


def test_optimized_admission_still_refuses_a_priced_larger_model(tmp_path: Path) -> None:
    guard = setup_guard(tmp_path)
    config = json.loads(guard.config_path.read_text())
    config["providers"]["openai"]["models"]["gpt-6-sol"] = config["providers"]["openai"]["models"][
        "gpt-6-luna"
    ]
    guard.config_path.write_text(json.dumps(config))
    script = """
import sys
from pathlib import Path
from mux.conformance.budget import BudgetGuard, BudgetRefused, ProbePlan, TokenLimits
guard = BudgetGuard(Path(sys.argv[1]), Path(sys.argv[2]))
plan = ProbePlan(provider='openai', model='gpt-6-sol', fixture_id='C16',
    limits=TokenLimits(input_tokens=1_000_000, output_tokens=0))
try:
    guard.reserve(plan)
except BudgetRefused:
    pass
else:
    raise SystemExit('optimized model policy vanished')
"""
    subprocess.run(
        [sys.executable, "-O", "-c", script, str(guard.config_path), str(guard.spend_path)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert receipts(guard)[-1].cost_estimate_usd == 0
