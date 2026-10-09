"""Default resolution: an unconfigured channel behaves exactly as before."""

from __future__ import annotations

import pytest
from mux.contracts.config import (
    DEFAULT_PROFILES,
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    ResolvedBackend,
    resolve_default,
)
from mux.contracts.ids import ChannelRef
from mux.errors import InvalidConfig
from mux.profiles import PROFILES
from pydantic import ValidationError

LEGACY = ResolvedBackend(
    backend="anthropic", profile="anthropic.managed_agents", model=None, thread_mode="per_caller"
)


@pytest.mark.parametrize("config", [None, BackendConfig(), BackendConfig.model_validate({})])
def test_empty_or_legacy_config_resolves_to_anthropic_per_caller(
    config: BackendConfig | None,
) -> None:
    assert resolve_default(config) == LEGACY


def test_anthropic_keeps_the_agent_model_when_none_is_named() -> None:
    resolved = resolve_default(BackendConfig(backend="anthropic"))
    assert resolved.model is None
    assert resolved.profile == "anthropic.managed_agents"


def test_shared_threads_are_opt_in() -> None:
    assert BackendConfig().thread_mode == "per_caller"
    assert resolve_default(BackendConfig(thread_mode="shared")).thread_mode == "shared"


def test_openai_gets_its_core_profile_but_needs_a_model() -> None:
    with pytest.raises(InvalidConfig, match="explicit model"):
        resolve_default(BackendConfig(backend="openai"))
    resolved = resolve_default(BackendConfig(backend="openai", model="gpt-6"))
    assert resolved.profile == "openai.persistent_workspace"


def test_gemini_must_be_named_explicitly() -> None:
    with pytest.raises(InvalidConfig, match="no default profile"):
        resolve_default(BackendConfig(backend="gemini", model="gemini-3"))
    resolved = resolve_default(
        BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3")
    )
    assert resolved.profile == "gemini.inline_reuse"


@pytest.mark.parametrize(
    "config",
    [
        BackendConfig(profile="openai.conversation_only"),
        BackendConfig(backend="openai", profile="anthropic.managed_agents", model="m"),
    ],
)
def test_profile_must_belong_to_its_backend(config: BackendConfig) -> None:
    with pytest.raises(InvalidConfig):
        resolve_default(config)


def test_default_profiles_exist_and_are_core() -> None:
    for backend, profile_id in DEFAULT_PROFILES.items():
        assert PROFILES[profile_id].provider == backend
        assert PROFILES[profile_id].core


def test_optional_requirement_needs_a_fallback() -> None:
    with pytest.raises(ValidationError, match="fallback"):
        CapabilityRequirement(level="optional")
    with pytest.raises(ValidationError, match="no fallback"):
        CapabilityRequirement(level="required", fallback="x")


def test_config_revision_digest_tracks_content(
    channel: ChannelRef, resolved: ResolvedBackend
) -> None:
    revision = ConfigRevision.create(channel, 1, resolved)
    assert revision.digest == resolved.content_digest()
    assert ConfigRevision.create(channel, 2, resolved).digest == revision.digest
    assert revision.digest != LEGACY.content_digest()
    tampered = revision.model_dump() | {"model": "other"}
    with pytest.raises(ValidationError, match="digest"):
        ConfigRevision.model_validate(tampered)
