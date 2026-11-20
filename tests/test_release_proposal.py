"""Release proposal tiering is a pure function; these pin the rules that make a span high."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release_proposal", Path(__file__).resolve().parents[1] / "scripts" / "release" / "proposal.py"
)
assert _SPEC and _SPEC.loader
proposal = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = proposal
_SPEC.loader.exec_module(proposal)

EXPAND = proposal.Migration("0230_a.py", "expand", "expand", "safe")


def span(**kw: object) -> object:
    base: dict[str, object] = {
        "prs": 3,
        "migrations": (),
        "paths": ("README.md",),
        "bad_signals": (),
    }
    return proposal.Span(**{**base, **kw})


def test_tier_when_span_is_ordinary_is_low() -> None:
    level, reasons = proposal.tier(span(migrations=(EXPAND,)))
    assert (level, reasons) == ("low", []), "expand migration and docs are low tier"


@pytest.mark.parametrize(
    "kw",
    [
        {"migrations": (proposal.Migration("0231_b.py", "expand", "contract", "safe"),)},
        # Declared contract: the classifier cannot see a function body or a policy change.
        {"migrations": (proposal.Migration("0231_b.py", "contract", "expand", "safe"),)},
        {"migrations": (proposal.Migration("0231_b.py", "expand", "expand", "unsupported"),)},
        {"prs": 21},
        {"bad_signals": ("canary failed",)},
        {"paths": (".github/workflows/ci.yml",)},
        {"paths": ("deploy/compose.yml",)},
        {"paths": ("packages/core/daimon/core/billing/ledger.py",)},
        {"paths": ("packages/core/daimon/core/stores/tenant_store.py",)},
        {"paths": ("packages/core/daimon/core/usage_recording.py",)},
        {"paths": ("packages/core/daimon/core/privacy/cascade.py",)},
    ],
)
def test_tier_when_high_trigger_present_is_high(kw: dict[str, object]) -> None:
    level, reasons = proposal.tier(span(**kw))
    assert level == "high" and reasons, f"{kw} should be high with a stated reason"


@pytest.mark.parametrize(
    "path",
    [
        "packages/core/tests/test_billing.py",
        "packages/adapters/discord/daimon/adapters/discord/billing_panel/panel.py",
        "docs/auth.md",
    ],
)
def test_tier_when_path_is_test_or_outside_core_is_low(path: str) -> None:
    assert proposal.tier(span(paths=(path,)))[0] == "low", f"{path} is not a sensitive core path"


def test_tier_when_exactly_twenty_prs_is_low() -> None:
    assert proposal.tier(span(prs=20))[0] == "low", "the limit is more than 20"


def _jobs(
    conclusion: str, name: str = "Deploy to GCP (staging) / Deploy to GCP (staging)"
) -> list[dict]:
    step = {"name": "Mark tested staging images promotable", "conclusion": conclusion}
    return [{"name": name, "steps": [step]}]


def test_promotable_only_when_staging_marked_its_images() -> None:
    assert proposal.promotable(_jobs("success")), "promotable images propose"
    assert not proposal.promotable(_jobs("skipped")), "a superseded run proposes nothing"
    assert not proposal.promotable(_jobs("failure")), "a deploy failing late proposes nothing"
    assert not proposal.promotable(_jobs("success", "Deploy to GCP (production)")), "staging only"


def _deploy(conclusion: str, completed_at: str) -> list[dict]:
    job = "Promote to production / Deploy to GCP (production)"
    return [{"name": job, "conclusion": conclusion, "completed_at": completed_at}]


def test_production_sha_is_the_last_successful_production_deploy() -> None:
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    runs = [
        ("Promote", _deploy("success", "2026-11-20T09:00:00Z")),
        # Created last but approved first.
        (f"Promote {a}", _deploy("success", "2026-11-19T10:00:00Z")),
        # Deployed, then its canary failed: the run is red, production still runs b.
        (f"Promote {b}", _deploy("success", "2026-11-19T12:00:00Z")),
        (f"Promote {c}", _deploy("failure", "2026-11-19T13:00:00Z")),
    ]
    assert proposal.production_sha(runs) == b, "the latest successful deploy job wins"
    assert proposal.production_sha([]) is None, "no promote run means unknown"
