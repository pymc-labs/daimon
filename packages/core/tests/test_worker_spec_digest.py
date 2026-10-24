"""A worker's spec digest names its intent, not the spelling of an equal amount.

Spawn and run_worker each re-resolve the worker and compare digests; an equal
Decimal written two ways ("0.67" / "0.6700") must never read as changed intent.
Both sides log which part of the digest body they hashed, ids only.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from daimon.core.neutral_resources import _canonical_money  # pyright: ignore[reportPrivateUsage]
from structlog.testing import capture_logs

from .test_fleet_permissions import parent, resolve


@pytest.mark.parametrize(
    ("given", "canonical"),
    # 0.670000: the staging Argon rows (Numeric(…, 6)) whose digest broke run_worker.
    [
        ("0.670000", "0.67"),
        ("0.6700", "0.67"),
        ("0.67", "0.67"),
        ("2.00", "2"),
        ("0", "0"),
        ("0.00", "0"),
        ("1E+1", "10"),
    ],
)
def test_an_amount_has_one_spelling(given: str, canonical: str) -> None:
    from decimal import localcontext

    with localcontext() as low:
        # Never rounded by an ambient low precision (1.2345 stays 1.2345).
        low.prec = 2
        assert _canonical_money({"budget_usd": "1.2345"})["budget_usd"] == "1.2345"
    limits = {"budget_usd": given, "provider_limits_usd": {"gemini": given}, "max_turns": 4}
    assert _canonical_money(limits) == {
        "budget_usd": canonical,
        "provider_limits_usd": {"gemini": canonical},
        "max_turns": 4,
    }


@pytest.mark.parametrize("provider", ("anthropic", "openai", "gemini"))
def test_equal_budgets_written_two_ways_resolve_to_one_digest(provider: str) -> None:
    root = parent(provider)
    short = root.limits.model_copy(update={"budget_usd": Decimal("0.67")})
    # As the row stores it (six places) vs as spawn computed it.
    padded = root.limits.model_copy(update={"budget_usd": Decimal("0.670000")})
    assert (
        resolve(replace(root, limits=short), limits=short).digest
        == resolve(replace(root, limits=padded), limits=padded).digest
    )
    # A different amount is still a different intent.
    other = root.limits.model_copy(update={"budget_usd": Decimal("0.68")})
    assert (
        resolve(replace(root, limits=other), limits=other).digest
        != resolve(replace(root, limits=short), limits=short).digest
    )


def test_each_side_logs_its_digest_parts_without_content() -> None:
    root = parent("gemini")
    with capture_logs() as logs:
        spawned = resolve(root, digest_phase="spawn")
        ran = resolve(root, digest_phase="run_worker")
        resolve(root)  # unlabelled callers stay silent
    entries = [e for e in logs if e["event"] == "fleet.worker_spec_digest"]
    assert [e["phase"] for e in entries] == ["spawn", "run_worker"]
    assert entries[0]["digest"] == spawned.digest[:12] == ran.digest[:12]
    assert entries[0]["parts"] == entries[1]["parts"]
    assert {"spec", "limits", "backend", "scope"} <= set(entries[0]["parts"])
    assert all(len(value) == 8 for value in entries[0]["parts"].values())
    assert entries[0]["system_chars"] == len(spawned.spec.system or "")
    # Hashes and ids only: the instructions never reach the log.
    assert (spawned.spec.system or "x") not in str(entries)
