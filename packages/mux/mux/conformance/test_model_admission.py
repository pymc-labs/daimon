"""Model policy refuses even priced alternatives, before callback/key access."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mux.conformance.budget import BudgetRefused, ProbePlan, TokenLimits
from mux.conformance.live_probe import ProbeOutcome, run_probe
from mux.conformance.recording import Recorder
from mux.conformance.test_budget import receipts, setup_guard

APPROVED = (
    ("openai", "gpt-6-luna"),
    ("anthropic", "claude-haiku-5-5"),
    ("gemini", "gemini-3.5-flash-lite"),
)


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
        ("openai", "claude-haiku-5-5"),
        ("anthropic", "claude-sonnet-5-5"),
        ("anthropic", "claude-haiku-4-5"),
        ("anthropic", "CLAUDE-HAIKU-5-5"),
        ("gemini", "gemini-3.5-pro"),
        ("gemini", "gemini-3.5-flash"),
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
