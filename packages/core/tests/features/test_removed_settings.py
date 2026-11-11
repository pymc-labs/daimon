"""An upgrade that still sets a removed variable starts with a warning; anything else unknown fails."""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.core.config import FeatureEnvSettings, load_settings
from daimon.core.defaults.loader import load_agent_specs
from daimon.core.features.registry import FEATURES
from daimon.core.features.removed import REMOVED_FEATURE_KEYS
from daimon.core.specs import AgentSpec
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


_BLOCK_REQUIRED = {
    "DAIMON_DISCORD": {"DAIMON_DISCORD__BOT_TOKEN": "t"},
    "DAIMON_ARTIFACTS": {
        "DAIMON_ARTIFACTS__ENDPOINT_URL": "https://s3.example.com",
        "DAIMON_ARTIFACTS__BUCKET": "b",
        "DAIMON_ARTIFACTS__ACCESS_KEY_ID": "k",
        "DAIMON_ARTIFACTS__SECRET_ACCESS_KEY": "s",
    },
}


@pytest.mark.parametrize(
    "variable",
    [
        "DAIMON_TURN__STATUS_LOG",
        "DAIMON_TURN__NATIVE_FOOTER_COST",
        "DAIMON_DISCORD__WRITERS_NONE_NOTICE",
        "DAIMON_DISCORD__UNMENTIONED_REPLY_HINT",
        "DAIMON_DISCORD__TENANT_CAP_WAITING_CARD",
        "DAIMON_ARTIFACTS__EMBED_IMAGES",
    ],
)
def test_removed_field_of_a_lenient_block_starts_with_a_warning_naming_it(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These blocks ignore unknown fields, so without the list the variable would vanish silently."""
    for name, value in _BLOCK_REQUIRED.get(variable.split("__")[0], {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(variable, "true")
    with capture_logs() as logs:
        load_settings(_env_file=None)
    assert variable in _variables_warned(logs), logs


def test_agent_spec_drops_a_removed_feature_with_a_warning() -> None:
    with capture_logs() as logs:
        spec = AgentSpec.model_validate(
            {
                "name": "a",
                "model": "claude-sonnet-4-6",
                "features": {"status_log": "off", "progress_plans": "on"},
            }
        )
    assert spec.features == {"progress_plans": "on"}, "only the registered key stays"
    assert _variables_warned(logs) == ["features.status_log"], logs


def test_seeded_agent_yaml_with_a_removed_feature_still_loads(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        "name: a\nmodel: claude-sonnet-4-6\nfeatures:\n  daimon_tools: off\n"
    )
    with capture_logs() as logs:
        (spec,) = load_agent_specs(tmp_path)
    assert spec.features == {}, "the removed key is dropped"
    assert _variables_warned(logs) == ["features.daimon_tools"], logs


def test_agent_spec_with_an_unknown_feature_still_fails() -> None:
    with pytest.raises(ValidationError, match="cannot be set on an agent"):
        AgentSpec.model_validate(
            {"name": "a", "model": "claude-sonnet-4-6", "features": {"not_a_feature": "on"}}
        )


def test_unknown_feature_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_FEATURES__NOT_A_FEATURE", "on")
    with pytest.raises(ValidationError, match="unknown feature"):
        load_settings(_env_file=None)


def test_unknown_tool_safety_field_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_TOOL_SAFETY__NOT_A_FIELD", "true")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_settings(_env_file=None)
