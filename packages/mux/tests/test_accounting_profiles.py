"""One declared accounting mode drives all settlement policy projections."""

from __future__ import annotations

from typing import cast

import pytest
from mux.contracts.ids import Provider
from mux.contracts.profile import AccountingMode, Profile
from mux.errors import InvalidConfig
from mux.profiles import PROFILES, accounting_mode_for, get_profile
from pydantic import ValidationError


@pytest.mark.parametrize(
    "id_,mode,native,root",
    [
        ("anthropic.managed_agents", AccountingMode.REQUEST_SPANS, False, False),
        ("openai.persistent_workspace", AccountingMode.NATIVE_ROOT_TOTALS, True, True),
        ("openai.conversation_only", AccountingMode.NATIVE_ROOT_TOTALS, True, True),
        ("gemini.inline_reuse", AccountingMode.NATIVE_TOTALS, True, False),
        ("selfhosted.loop", AccountingMode.NATIVE_REQUESTS, True, False),
    ],
)
def test_declared_profile_accounting_preserves_existing_policy(id_, mode, native, root):
    profile = get_profile(id_)
    assert profile.accounting_mode is mode
    assert accounting_mode_for(profile.provider) is mode
    assert mode.settles_natively is native
    assert mode.supports_root_usage is root


@pytest.mark.parametrize(
    "mode,grain",
    [
        (AccountingMode.REQUEST_SPANS, "model_request"),
        (AccountingMode.NATIVE_REQUESTS, "model_request"),
        (AccountingMode.NATIVE_TOTALS, "turn"),
        (AccountingMode.NATIVE_ROOT_TOTALS, "turn"),
    ],
)
def test_each_accounting_mode_names_the_grain_it_settles(mode, grain):
    assert mode.usage_grain == grain


def test_unknown_provider_has_no_accounting_fallback():
    with pytest.raises(InvalidConfig, match="unambiguous"):
        accounting_mode_for(cast(Provider, "missing"))


def test_conflicting_profile_declarations_refuse_instead_of_selecting_first(monkeypatch):
    declaration = get_profile("openai.conversation_only").model_copy(
        update={"accounting_mode": AccountingMode.REQUEST_SPANS}
    )
    monkeypatch.setattr("mux.profiles.PROFILES", {**PROFILES, declaration.profile_id: declaration})
    with pytest.raises(InvalidConfig, match="unambiguous"):
        accounting_mode_for("openai")


def test_registry_policy_is_read_from_metadata_not_a_second_provider_table(monkeypatch):
    original = get_profile("anthropic.managed_agents")
    revised = original.model_copy(update={"accounting_mode": AccountingMode.NATIVE_TOTALS})
    monkeypatch.setattr("mux.profiles.PROFILES", {**PROFILES, original.profile_id: revised})
    assert accounting_mode_for("anthropic").settles_natively
    assert not accounting_mode_for("anthropic").supports_root_usage


def test_accounting_metadata_does_not_enable_a_delegation_configuration_port():
    assert get_profile("openai.persistent_workspace").support_for("multiagent") == "unsupported"
    assert get_profile("gemini.inline_reuse").support_for("multiagent") == "unsupported"
    assert not accounting_mode_for("gemini").supports_root_usage


def test_custom_profile_default_and_closed_serialized_modes():
    base = get_profile("openai.conversation_only").model_dump(mode="json")
    base.pop("accounting_mode")
    base["profile_id"] = "openai.custom"
    assert Profile.model_validate(base).accounting_mode is AccountingMode.REQUEST_SPANS
    with pytest.raises(ValidationError, match="accounting_mode"):
        Profile.model_validate({**base, "accounting_mode": "unreviewed"})
