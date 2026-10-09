"""Declared profiles and extension addressing."""

from __future__ import annotations

import inspect

import pytest
from mux.contracts import ports
from mux.contracts.extensions import ExtensionConfig, ExtensionRef
from mux.contracts.profile import CORE_CAPABILITIES, Profile
from mux.errors import ExtensionVersionError, InvalidConfig, UnsupportedCapability
from mux.profiles import MANAGED_AGENTS, PERSISTENT_WORKSPACE, PROFILES, get_profile
from pydantic import ValidationError


def test_declared_profiles_and_their_core_status() -> None:
    assert {pid: p.core for pid, p in PROFILES.items()} == {
        "anthropic.managed_agents": True,
        "openai.persistent_workspace": False,
        "openai.conversation_only": False,
        "gemini.inline_reuse": False,
    }


def test_core_flag_is_checked_against_support() -> None:
    support = dict.fromkeys(CORE_CAPABILITIES, "native")
    support["cancel"] = "unknown"
    with pytest.raises(ValidationError, match="lacks cancel"):
        Profile(
            provider="openai",
            profile_id="openai.x",
            schema_version="1",
            sdk_pin="openai",
            core=True,
            support=support,  # pyright: ignore[reportArgumentType]
        )


def test_undeclared_capability_is_unknown() -> None:
    assert PERSISTENT_WORKSPACE.support_for("tool_confirmation") == "unknown"


def test_unknown_profile_id() -> None:
    with pytest.raises(InvalidConfig):
        get_profile("anthropic.nope")
    assert get_profile("anthropic.managed_agents") is MANAGED_AGENTS


def test_extension_lookup_distinguishes_never_from_wrong_version() -> None:
    assert MANAGED_AGENTS.offered_extension("anthropic.memory_stores", 1) == ExtensionRef(
        namespace="anthropic.memory_stores", version=1
    )
    with pytest.raises(ExtensionVersionError) as caught:
        MANAGED_AGENTS.offered_extension("anthropic.memory_stores", 2)
    assert caught.value.offered == (1,)
    with pytest.raises(UnsupportedCapability):
        PERSISTENT_WORKSPACE.offered_extension("anthropic.memory_stores", 1)


def test_extensions_are_namespaced_by_provider() -> None:
    with pytest.raises(ValidationError):
        ExtensionConfig(namespace="memory_stores", version=1)
    with pytest.raises(ValidationError, match="not a openai namespace"):
        Profile.model_validate(
            PERSISTENT_WORKSPACE.model_dump()
            | {"extensions": [{"namespace": "anthropic.vaults", "version": 1}]}
        )


def test_no_port_exposes_a_raw_client() -> None:
    for _, port in inspect.getmembers(ports, inspect.isclass):
        if port.__module__ != ports.__name__:
            continue
        names = {n for n in dir(port) if not n.startswith("__")}
        assert not {n for n in names if "client" in n or n.startswith("raw")}, port.__name__
