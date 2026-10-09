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
    if config.get("backend") == "openai":
        config.setdefault("profile", "openai.persistent_workspace")
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
    admission = admit(
        _revision(backend="openai", model="gpt-6", requires={"reconcile": REQUIRED}),
        PERSISTENT_WORKSPACE,
    )
    assert admission.emulated == ("reconcile",)


def test_core_capabilities_are_mandatory_on_a_core_profile() -> None:
    assert set(MANAGED_AGENTS.missing_core()) == set()
    assert MANAGED_AGENTS.core
    assert PERSISTENT_WORKSPACE.core
    assert PERSISTENT_WORKSPACE.missing_core() == ()


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


def test_gemini_inline_reuse_requires_an_explicit_profile_and_model() -> None:
    admission = admit(
        _revision(backend="gemini", profile="gemini.inline_reuse", model="gemini-3"), INLINE_REUSE
    )
    assert admission.profile_id == "gemini.inline_reuse"
    assert "usage_observations" in admission.emulated
    assert "thread_workspace_persistence" in admission.waived_core
    assert not INLINE_REUSE.core
    with pytest.raises(InvalidConfig, match="profile"):
        _revision(backend="gemini", model="gemini-3")
    with pytest.raises(InvalidConfig, match="model"):
        _revision(backend="gemini", profile="gemini.inline_reuse")


def test_usage_observations_cannot_be_waived_even_by_a_named_non_core_profile() -> None:
    assert not INLINE_REUSE.core
    config = _revision(backend="gemini", profile="gemini.inline_reuse", model="gemini-3")
    unmetered_gemini = INLINE_REUSE.model_copy(
        update={"support": {**INLINE_REUSE.support, "usage_observations": "unknown"}}
    )
    with pytest.raises(UnsupportedCapability) as caught:
        admit(config, unmetered_gemini)
    assert caught.value.missing == ("usage_observations",)
    unmetered = CONVERSATION_ONLY.model_copy(
        update={"support": {**CONVERSATION_ONLY.support, "usage_observations": "unknown"}}
    )
    named = _revision(backend="openai", profile="openai.conversation_only", model="gpt-6")
    with pytest.raises(UnsupportedCapability):
        admit(named, unmetered)


def test_admission_rejects_a_backend_that_disagrees_with_the_profile() -> None:
    legacy = _revision()
    with pytest.raises(InvalidConfig):
        admit(legacy, MANAGED_AGENTS.model_copy(update={"provider": "openai"}))


def test_admission_rejects_a_stale_digest() -> None:
    config = _revision(backend="openai", model="gpt-6", requires={"memory_stores": REQUIRED})
    forged = ConfigRevision.model_construct(
        **{name: getattr(config, name) for name in ConfigRevision.model_fields} | {"requires": {}}
    )
    with pytest.raises(InvalidConfig, match="digest"):
        admit(forged, PERSISTENT_WORKSPACE)


def test_requirements_cannot_be_mutated_after_admission_was_refused() -> None:
    config = _revision(backend="openai", model="gpt-6", requires={"memory_stores": REQUIRED})
    digest = config.digest
    with pytest.raises((TypeError, AttributeError)):
        getattr(config.requires, "clear")()  # noqa: B009 -- deliberate mutation probe
    with pytest.raises(TypeError):
        config.requires["memory_stores"] = CapabilityRequirement(level="optional", fallback="x")  # pyright: ignore[reportIndexIssue]
    assert config.digest == digest == config.content_digest()
    with pytest.raises(UnsupportedCapability):
        admit(config, PERSISTENT_WORKSPACE)


def test_profile_must_match_the_config() -> None:
    with pytest.raises(InvalidConfig, match="selects"):
        admit(_revision(), PERSISTENT_WORKSPACE)


def test_non_default_backend_without_a_model_is_refused_at_admission() -> None:
    resolved = resolve_default(
        BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-6")
    )
    config = ConfigRevision.create(CHANNEL, 1, resolved)
    for model in (None, "", "  "):
        forged = config.model_copy(update={"model": model})
        # Re-digest, so the selection check, not the digest check, is what refuses it.
        forged = forged.model_copy(update={"digest": forged.content_digest()})
        with pytest.raises(InvalidConfig, match="model"):
            admit(forged, PERSISTENT_WORKSPACE)


def test_admission_refuses_a_re_digested_backend_profile_mismatch() -> None:
    config = _revision()
    forged = config.model_copy(update={"backend": "openai", "model": "gpt-6"})
    forged = forged.model_copy(update={"digest": forged.content_digest()})
    with pytest.raises(InvalidConfig, match="selects"):
        admit(forged, MANAGED_AGENTS)
