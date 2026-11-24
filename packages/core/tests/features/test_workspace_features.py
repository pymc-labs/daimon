"""A feature limited to some workspaces (`in_workspace`) does not exist anywhere else."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from daimon.core.config import (
    AnthropicSettings,
    ArtifactPagesSettings,
    DatabaseSettings,
    Settings,
)
from daimon.core.defaults._reconcile import (
    _workspace_skills,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.features import deployment
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import FeatureView, TierSetting, feature_refusal
from daimon.core.features.snapshot import FeaturePlace, resolve_one, unavailable_reason
from daimon.core.features.surface import listed, present
from daimon.core.features.tool_gate import hides_tools
from daimon.core.specs import AgentSpec, SkillRef
from daimon.core.stores.domain import FeatureValueRow
from pydantic import PostgresDsn, SecretStr

LISTED, OTHER = uuid.UUID(int=1), uuid.UUID(int=2)
SETTINGS = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        "artifact_pages": ArtifactPagesSettings(workspaces=(LISTED,)),
    }
)
SPEC = FEATURES["artifacts"]


def _saved(tenant_id: uuid.UUID) -> FeatureView:
    """`artifacts` at an agent with its own On saved, as a listing reads it."""
    place = FeaturePlace(tenant_id, "slack", agent_id="agent_a")
    row = FeatureValueRow(
        tenant_id=tenant_id,
        tier="agent",
        agent_id="agent_a",
        channel_id="",
        thread_key="",
        key="artifacts",
        value="on",
        locked=False,
        set_by=None,
        surface="web",
        set_at=datetime.now(UTC),
        revision=1,
    )
    resolved = resolve_one(SPEC, SETTINGS, place, [row])
    return FeatureView(SPEC, (TierSetting("agent", "on"),), resolved)


def test_outside_the_list_artifacts_do_not_exist_even_with_a_saved_value() -> None:
    """No listing shows the row, no change takes the key and its tools are hidden."""
    inside, outside = _saved(LISTED), _saved(OTHER)
    assert outside.resolved.unavailable == "workspace", "the workspace lacks the feature"
    assert not outside.resolved.on and hides_tools(outside.resolved), "its tools are gone"
    place = FeaturePlace(OTHER, "slack")
    assert listed([inside, outside], SETTINGS, place) == (inside,), "the chat listing drops it"
    assert present([inside, outside]) == (inside,), "the web and CLI listings drop it"
    refusal = feature_refusal(
        "artifacts", "agent", "off", locked=False, place=place, settings=SETTINGS
    )
    assert refusal is not None and refusal.reason == "unknown_key", "a change names no setting"
    assert inside.resolved.unavailable != "workspace", "a listed workspace keeps it"


def test_without_settings_the_process_env_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader handed no `Settings` (an agent edit, the defaults pipeline) reads the env."""
    monkeypatch.setenv("DAIMON_ARTIFACT_PAGES__WORKSPACES", str(LISTED))
    deployment._env_artifact_pages.cache_clear()  # pyright: ignore[reportPrivateUsage]
    try:
        assert unavailable_reason(SPEC, None, FeaturePlace(OTHER, "web")) == "workspace"
        assert unavailable_reason(SPEC, None, FeaturePlace(LISTED, "web")) is None
    finally:
        deployment._env_artifact_pages.cache_clear()  # pyright: ignore[reportPrivateUsage]


def test_an_unlisted_workspace_is_seeded_without_the_artifacts_skill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither the skill nor an agent's reference to it reaches a workspace without artifacts."""
    monkeypatch.setenv("DAIMON_ARTIFACT_PAGES__WORKSPACES", str(LISTED))
    deployment._env_artifact_pages.cache_clear()  # pyright: ignore[reportPrivateUsage]
    dirs = [tmp_path / "artifacts", tmp_path / "file-handling"]
    refs = [SkillRef(type="custom", skill_id=d.name) for d in dirs]
    spec = AgentSpec(name="default", model="claude-sonnet-4-5", skills=refs)
    try:
        kept_dirs, (kept,) = _workspace_skills(dirs, [spec], OTHER)
        whole_dirs, (whole,) = _workspace_skills(dirs, [spec], LISTED)
    finally:
        deployment._env_artifact_pages.cache_clear()  # pyright: ignore[reportPrivateUsage]
    assert [d.name for d in kept_dirs] == ["file-handling"], "the skill is not seeded"
    assert [r.skill_id for r in kept.skills] == ["file-handling"], "nor named on the agent"
    assert whole_dirs == dirs and whole.skills == refs, "a listed workspace gets both"
