"""An upgrade that still sets a removed variable starts with a warning; anything else unknown fails."""

from __future__ import annotations

import pytest
from daimon.core.config import FeatureEnvSettings, load_settings
from daimon.core.features.registry import FEATURES
from daimon.core.features.removed import REMOVED_FEATURE_KEYS
from pydantic import ValidationError
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")


def _variables_warned(logs: list[dict[str, object]]) -> list[object]:
    return [
        entry["variable"] for entry in logs if entry["event"] == "settings.removed_variable_ignored"
    ]


def test_removed_keys_are_not_registered_features() -> None:
    """The removed list never overlaps the registry, or a live feature would be ignored."""
    assert not REMOVED_FEATURE_KEYS & set(FEATURES), "a removed key is still a feature"


@pytest.mark.parametrize("key", sorted(REMOVED_FEATURE_KEYS))
def test_removed_feature_variable_starts_with_a_warning_naming_it(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"DAIMON_FEATURES__{key.upper()}", "on")
    with capture_logs() as logs:
        settings = load_settings(_env_file=None)
    assert key not in settings.features, "a removed key must not reach the deployment tier"
    assert _variables_warned(logs) == [f"DAIMON_FEATURES__{key.upper()}"], logs


def test_removed_feature_variable_is_tolerated_by_the_feature_writer_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_FEATURES__STATUS_LOG", "on:locked")
    monkeypatch.setenv("DAIMON_FEATURES__PROGRESS_PLANS", "on")
    with capture_logs() as logs:
        found = FeatureEnvSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert found.features == {"progress_plans": "on"}, "only the registered key stays"
    assert _variables_warned(logs) == ["DAIMON_FEATURES__STATUS_LOG"], logs


def test_removed_approvals_switch_starts_with_a_warning_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_TOOL_SAFETY__ALLOW_GATE_OVERRIDES", "true")
    monkeypatch.setenv("DAIMON_TOOL_SAFETY__TRUSTED_ANNOTATIONS", '["linear"]')
    with capture_logs() as logs:
        settings = load_settings(_env_file=None)
    assert settings.tool_safety.trusted_annotations == ("linear",), "other fields still apply"
    assert _variables_warned(logs) == ["DAIMON_TOOL_SAFETY__ALLOW_GATE_OVERRIDES"], logs


def test_unknown_feature_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_FEATURES__NOT_A_FEATURE", "on")
    with pytest.raises(ValidationError, match="unknown feature"):
        load_settings(_env_file=None)


def test_unknown_tool_safety_field_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_TOOL_SAFETY__NOT_A_FIELD", "true")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_settings(_env_file=None)
