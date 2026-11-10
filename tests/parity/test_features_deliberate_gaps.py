"""Executable record of where a feature level does not decide everything.

Watches keep an operator safety gate beside the `watches` feature: the
`DAIMON_WATCHES__ENABLED` variable and the `global` row of the `watch_controls`
table. A watch fires only when both are on, whatever a workspace, agent or
channel sets, because a firing reads outside pages and starts paid turns without
a person present. So the feature has no `requires` entry for it (the tools work,
and list or remove what exists, without the gate) and the gate is not a feature
level. Each reader of the feature states this where a person looks.
"""

from __future__ import annotations

from pathlib import Path

from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import _SETUP

REPO_ROOT = Path(__file__).resolve().parents[2]

DELIBERATE_GATES = {
    "watches": "the operator's `DAIMON_WATCHES__ENABLED` and the `global` watch_controls row",
}
"""Feature key -> the operator gate that stays outside the feature levels, and why above."""


def test_watches_keep_their_operator_gate_outside_the_feature() -> None:
    spec = FEATURES["watches"]
    assert spec.legacy_env == ("DAIMON_WATCHES__ENABLED",), "the variable is the old switch"
    assert not any(need in _SETUP for need in spec.requires), "no setup entry hides the tools"
    assert "only once the operator enables them" in spec.doc


def test_the_watches_doc_says_they_fire_only_once_the_operator_enables_them() -> None:
    text = " ".join((REPO_ROOT / "docs" / "watches.md").read_text().split())
    assert "Watches fire only once the operator has done both" in text
    assert "DAIMON_WATCHES__ENABLED" in text and "`global` gate" in text


def test_every_deliberate_gate_names_a_registered_feature() -> None:
    assert set(DELIBERATE_GATES) <= set(FEATURES)
