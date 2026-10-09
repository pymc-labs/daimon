"""Admission: required gaps refuse, optional gaps fall back visibly."""

from __future__ import annotations

import pytest
from mux.contracts.admission import admit
from mux.contracts.config import (
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    resolve_default,
)
from mux.contracts.ids import ChannelRef
from mux.contracts.profile import Capability
from mux.errors import InvalidConfig, UnsupportedCapability
from mux.profiles import (
    CONVERSATION_ONLY,
    INLINE_REUSE,
    MANAGED_AGENTS,
    PERSISTENT_WORKSPACE,
)

CHANNEL = ChannelRef(tenant_id="t1", platform="discord", channel_id="c1")
REQUIRED = CapabilityRequirement(level="required")


def _optional(fallback: str) -> CapabilityRequirement:
    return CapabilityRequirement(level="optional", fallback=fallback)


def _revision(**config: object) -> ConfigRevision:
    return ConfigRevision.create(CHANNEL, 1, resolve_default(BackendConfig.model_validate(config)))


def test_default_config_is_admitted_on_managed_agents() -> None:
    admission = admit(_revision(), MANAGED_AGENTS)
    assert admission.profile_id == "anthropic.managed_agents"
    assert admission.thread_mode == "per_caller"
    assert admission.fallbacks == ()
    assert admission.waived_core == ()
    assert admission.emulated == ()


def test_required_unsupported_lists_every_gap() -> None:
    config = _revision(
        backend="openai",
        model="gpt-6",
        requires={"memory_stores": REQUIRED, "native_event_replay": REQUIRED, "steer": REQUIRED},
    )
    with pytest.raises(UnsupportedCapability) as caught:
        admit(config, PERSISTENT_WORKSPACE)
    assert caught.value.missing == ("memory_stores", "native_event_replay")
    assert caught.value.profile == "openai.persistent_workspace"


def test_unknown_counts_as_unsupported() -> None:
    assert PERSISTENT_WORKSPACE.support_for("memory_stores") == "unknown"
    config = _revision(backend="openai", model="gpt-6", requires={"memory_stores": REQUIRED})
    with pytest.raises(UnsupportedCapability):
        admit(config, PERSISTENT_WORKSPACE)


def test_optional_unsupported_is_admitted_with_its_fallback_surfaced() -> None:
    config = _revision(
        backend="openai",
        model="gpt-6",
        requires={"memory_stores": _optional("repo_memory"), "steer": _optional("queue")},
    )
    admission = admit(config, PERSISTENT_WORKSPACE)
    assert [(f.capability, f.support, f.fallback) for f in admission.fallbacks] == [
        ("memory_stores", "unknown", "repo_memory")
    ]
    assert "steer" in admission.satisfied
    assert "memory_stores" not in admission.satisfied


def test_emulated_support_is_admitted_and_surfaced() -> None:
    admission = admit(_revision(backend="openai", model="gpt-6"), PERSISTENT_WORKSPACE)
    assert admission.emulated == ("reconcile",)


def test_core_capabilities_are_mandatory_on_a_core_profile() -> None:
    assert set(MANAGED_AGENTS.missing_core()) == set()
    assert MANAGED_AGENTS.core and PERSISTENT_WORKSPACE.core


def test_named_non_core_profile_is_admitted_with_waived_core_surfaced() -> None:
    config = _revision(backend="openai", profile="openai.conversation_only", model="gpt-6")
    admission = admit(config, CONVERSATION_ONLY)
    assert "thread_workspace_persistence" in admission.waived_core
    assert "skills_bundle" in admission.waived_core


def test_non_core_profile_still_refuses_an_explicitly_required_core_capability() -> None:
    requires: dict[Capability, CapabilityRequirement] = {"thread_workspace_persistence": REQUIRED}
    config = _revision(
        backend="gemini", profile="gemini.inline_reuse", model="gemini-3", requires=requires
    )
    with pytest.raises(UnsupportedCapability) as caught:
        admit(config, INLINE_REUSE)
    assert caught.value.missing == ("thread_workspace_persistence",)


def test_profile_must_match_the_config() -> None:
    with pytest.raises(InvalidConfig, match="selects"):
        admit(_revision(), PERSISTENT_WORKSPACE)


def test_non_default_backend_without_a_model_is_refused_at_admission() -> None:
    resolved = resolve_default(BackendConfig(backend="openai", model="gpt-6"))
    config = ConfigRevision.create(CHANNEL, 1, resolved.model_copy(update={"model": None}))
    with pytest.raises(InvalidConfig, match="explicit model"):
        admit(config, PERSISTENT_WORKSPACE)
