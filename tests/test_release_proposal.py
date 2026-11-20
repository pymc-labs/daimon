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

EXPAND = proposal.Migration("0230_a.py", "expand", "safe")


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
        {"migrations": (proposal.Migration("0231_b.py", "contract", "safe"),)},
        {"migrations": (proposal.Migration("0231_b.py", "expand", "unsupported"),)},
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
    return [{"name": name, "steps": [{"name": "Post-deploy smoke turn", "conclusion": conclusion}]}]


def test_smoke_succeeded_only_when_staging_smoke_step_succeeded() -> None:
    assert proposal.smoke_succeeded(_jobs("success")), "green smoke proposes"
    assert not proposal.smoke_succeeded(_jobs("skipped")), "a superseded run proposes nothing"
    assert not proposal.smoke_succeeded(_jobs("failure")), "a failed smoke proposes nothing"
    assert not proposal.smoke_succeeded(_jobs("success", "Deploy to GCP (production)")), (
        "staging only"
    )


def test_production_sha_takes_newest_titled_run() -> None:
    sha = "a" * 40
    runs = [
        {"displayTitle": "Promote"},
        {"displayTitle": f"Promote {sha}"},
        {"displayTitle": f"Promote {'b' * 40}"},
    ]
    assert proposal.production_sha(runs) == sha, "untitled runs are skipped, newest titled wins"
    assert proposal.production_sha([]) is None, "no promote run means unknown"
