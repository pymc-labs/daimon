"""Provider/profile product literals may only decrease outside the seam."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("provider_literal_baseline.json")
# Lower this ceiling along with the reviewed inventory when extracting provider policy.
MAX_OUTSIDE_SEAM = 209


def _is_provider_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (
            node.value in {"anthropic", "openai", "gemini"}
            or node.value.startswith(("anthropic.", "openai.", "gemini."))
        )
    )


def _provider_literals(source: str) -> int:
    return sum(_is_provider_literal(node) for node in ast.walk(ast.parse(source)))


def _outside_seam(path: Path) -> bool:
    parts = path.parts
    if "tests" in parts or "testing" in parts:
        return False
    if path.is_relative_to(Path("packages/mux/mux")):
        if path.is_relative_to(Path("packages/mux/mux/contracts/profile.py")):
            return False
        return not any(part in {"profiles", "drivers", "conformance"} for part in parts)
    return True


def _scan(root: Path) -> Counter[str]:
    directories = [
        root / "packages/core/daimon/core",
        root / "packages/mux/mux",
        root / "apps/notebook-host/src",
        root / "apps/report-host/src",
        *sorted((root / "packages/adapters").glob("*/daimon/adapters")),
    ]
    counts: Counter[str] = Counter()
    for directory in directories:
        for file in sorted(directory.rglob("*.py")):
            relative = file.relative_to(root)
            if _outside_seam(relative):
                count = _provider_literals(file.read_text())
                if count:
                    counts[str(relative)] = count
    return counts


def _assert_no_growth(current: Counter[str], ceiling: Counter[str]) -> None:
    growth = current - ceiling
    assert not growth, f"provider literals grew outside the profile/driver seam: {dict(growth)}"


def test_provider_literal_ratchet() -> None:
    ceiling = Counter(json.loads(BASELINE.read_text()))
    assert all(type(v) is int and v >= 0 for v in ceiling.values())
    assert ceiling.total() <= MAX_OUTSIDE_SEAM, (
        "regenerating the inventory must not raise its ceiling"
    )
    _assert_no_growth(_scan(ROOT), ceiling)


def test_inventory_counts_aliases_data_comparisons_membership_and_profile_matches() -> None:
    source = """
provider = "anthropic"
model = {"backend": "openai"}
if provider != "anthropic": pass
if provider in ("openai", "gemini"): pass
match profile:
    case "openai.persistent_workspace": pass
    case "gemini.inline_reuse" if provider == "gemini": pass
"""
    assert _provider_literals(source) == 8


def test_inventory_excludes_only_driver_profile_and_test_harness_boundaries(tmp_path: Path) -> None:
    names = [
        "packages/core/daimon/core/new_usage.py",
        "packages/adapters/slack/daimon/adapters/slack/new_usage.py",
        "packages/mux/mux/state/new_usage.py",
        "packages/mux/mux/contracts/usage.py",
        "packages/mux/mux/contracts/profile.py",
        "packages/mux/mux/profiles/custom.py",
        "packages/mux/mux/drivers/custom/usage.py",
        "packages/mux/mux/conformance/fake.py",
        "packages/testing/daimon/testing/fake.py",
        "packages/core/tests/test_fake.py",
    ]
    for name in names:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('if provider != "anthropic": pass\n')
    counts = _scan(tmp_path)
    assert set(counts) == set(names[:4])
    assert counts.total() == 4
    with pytest.raises(AssertionError, match="literals grew"):
        _assert_no_growth(counts, Counter())


def test_one_removed_literal_cannot_hide_a_new_literal_in_another_file() -> None:
    with pytest.raises(AssertionError, match="literals grew"):
        _assert_no_growth(Counter({"new.py": 1}), Counter({"old.py": 2}))
