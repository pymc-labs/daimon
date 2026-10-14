"""Exact opt-in settings, explicit missing dependencies and no ambient mutation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from daimon.testing import product_profiles as profiles
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings


class Mcp(BaseModel):
    require_routine_destination: bool = False
    endpoint: str = "https://fixture.invalid/mcp"


class Discord(BaseModel):
    credential_form_expiry: bool = False
    writers_none_notice: bool = False
    tenant_cap_waiting_card: bool = False
    table_rendering: bool = False
    bot_token: SecretStr = SecretStr("offline-fixture")


class Turn(BaseModel):
    native_footer_cost: bool = False
    path: str = "legacy"


class Core(BaseSettings):
    turn: Turn = Turn()
    mcp: Mcp = Mcp()
    discord: Discord | None = Discord()
    backend: str = "anthropic"


class Host(BaseSettings):
    html_link_errors: bool = False
    other_feature: bool = False


def install_declarations(monkeypatch):
    def module(name):
        if name == "daimon.core.config":
            return SimpleNamespace(McpSettings=Mcp, DiscordSettings=Discord, TurnSettings=Turn)
        return SimpleNamespace(Settings=Host)

    monkeypatch.setattr(profiles, "import_module", module)


def test_exact_five_flags_and_unknown_profile_are_closed() -> None:
    expected = {
        "core.mcp.require_routine_destination": True,
        "core.discord.credential_form_expiry": True,
        "core.discord.writers_none_notice": True,
        "core.discord.tenant_cap_waiting_card": True,
        "core.turn.native_footer_cost": True,
    }
    assert profiles.OPT_IN_PRODUCT_PROFILE.flags == expected
    assert profiles.DEFAULT_PRODUCT_PROFILE.flags == dict.fromkeys(expected, False)
    flags = profiles.OPT_IN_PRODUCT_PROFILE.flags
    flags["table_rendering"] = True
    assert profiles.OPT_IN_PRODUCT_PROFILE.flags == expected
    with pytest.raises(ValueError, match="unknown QA product profile"):
        profiles.QAProductProfile("something-else")


def test_copy_preserves_secrets_backend_and_unrelated_flags_without_constructor_reads(monkeypatch):
    base = Core(_env_file=None)
    host = Host(_env_file=None)
    constructor = MagicMock(side_effect=AssertionError("must not consult environment or .env"))
    monkeypatch.setattr(Core, "__init__", constructor)
    monkeypatch.setattr(Host, "__init__", constructor)
    enabled = profiles.OPT_IN_PRODUCT_PROFILE.apply(base, service="core")
    assert enabled.turn.native_footer_cost is True
    assert enabled.turn.path == base.turn.path == "legacy"
    assert base.turn.native_footer_cost is False
    assert enabled.mcp.require_routine_destination is True
    assert enabled.discord is not None and enabled.discord.credential_form_expiry is True
    assert enabled.discord.writers_none_notice is True
    assert enabled.discord.tenant_cap_waiting_card is True
    assert enabled.discord.table_rendering is False and enabled.backend == base.backend
    assert enabled.discord.bot_token is base.discord.bot_token
    assert base.mcp.require_routine_destination is False
    assert base.discord.credential_form_expiry is False
    assert base.discord.writers_none_notice is False
    assert base.discord.tenant_cap_waiting_card is False
    for service in ("notebook", "report"):
        # The notebook and report hosts have no granted switch: their settings pass through.
        assert profiles.OPT_IN_PRODUCT_PROFILE.apply(host, service=service) is host
    disabled = profiles.DEFAULT_PRODUCT_PROFILE.apply(enabled, service="core")
    assert disabled.turn.native_footer_cost is False
    assert disabled.mcp.require_routine_destination is False
    assert disabled.discord.credential_form_expiry is False
    assert disabled.discord.writers_none_notice is False
    assert disabled.discord.tenant_cap_waiting_card is False
    constructor.assert_not_called()


@pytest.mark.parametrize("service", ["core"])
def test_absent_settings_refuse_instead_of_injecting_unknown_fields(service):
    class Old(BaseModel):
        other: bool = False

    old = Old()
    with pytest.raises(profiles.ProductProfileUnavailable):
        profiles.OPT_IN_PRODUCT_PROFILE.apply(old, service=service)
    assert profiles.DEFAULT_PRODUCT_PROFILE.apply(old, service=service) is old
    assert old.model_dump() == {"other": False}


def test_absent_discord_refuses_instead_of_constructing_a_bot() -> None:
    with pytest.raises(profiles.ProductProfileUnavailable, match="discord.credential_form_expiry"):
        profiles.OPT_IN_PRODUCT_PROFILE.apply(Core(_env_file=None, discord=None), service="core")


def test_declaration_probe_never_constructs_settings_or_reads_credentials(monkeypatch):
    install_declarations(monkeypatch)
    constructor = MagicMock(side_effect=AssertionError("must not allocate or read settings"))
    monkeypatch.setattr(Core, "__init__", constructor)
    monkeypatch.setattr(Host, "__init__", constructor)
    profiles.OPT_IN_PRODUCT_PROFILE.require_available()
    constructor.assert_not_called()
    monkeypatch.setattr(profiles, "import_module", MagicMock(side_effect=ImportError))
    with pytest.raises(profiles.ProductProfileUnavailable) as refused:
        profiles.OPT_IN_PRODUCT_PROFILE.require_available()
    assert set(refused.value.missing) == set(profiles.OPT_IN_PRODUCT_PROFILE.flags)
    profiles.DEFAULT_PRODUCT_PROFILE.require_available()


async def test_async_children_keep_their_profile_and_restore_on_failure(monkeypatch):
    install_declarations(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def opt_in_child():
        with profiles.use_product_profile(profiles.OPT_IN_PRODUCT_PROFILE):
            entered.set()
            await release.wait()
            assert profiles.current_product_profile() == profiles.OPT_IN_PRODUCT_PROFILE
            raise RuntimeError("offline fixture refused")

    child = asyncio.create_task(opt_in_child())
    await entered.wait()
    assert profiles.current_product_profile() == profiles.DEFAULT_PRODUCT_PROFILE
    release.set()
    with pytest.raises(RuntimeError):
        await child
    assert profiles.current_product_profile() == profiles.DEFAULT_PRODUCT_PROFILE


def test_flag_application_receipt_is_scoped_and_not_an_outcome(monkeypatch):
    install_declarations(monkeypatch)
    assert profiles.applied_product_flags() == frozenset()
    with profiles.use_product_profile(profiles.OPT_IN_PRODUCT_PROFILE):
        assert profiles.applied_product_flags() == frozenset()
        profiles.OPT_IN_PRODUCT_PROFILE.apply(Core(_env_file=None), service="core")
        assert profiles.applied_product_flags() == {
            "core.mcp.require_routine_destination",
            "core.discord.credential_form_expiry",
            "core.discord.writers_none_notice",
            "core.discord.tenant_cap_waiting_card",
            "core.turn.native_footer_cost",
        }
        profiles.OPT_IN_PRODUCT_PROFILE.apply(Host(_env_file=None), service="notebook")
        profiles.OPT_IN_PRODUCT_PROFILE.apply(Host(_env_file=None), service="report")
        assert profiles.applied_product_flags() == set(profiles.OPT_IN_PRODUCT_PROFILE.flags)
    assert profiles.applied_product_flags() == frozenset()


def test_invalid_service_and_nonboolean_field_refuse() -> None:
    with pytest.raises(ValueError, match="unknown QA product service"):
        profiles.OPT_IN_PRODUCT_PROFILE.apply(Host(_env_file=None), service="unknown")

    class MalformedMcp(BaseModel):
        require_routine_destination: str = "off"

    class Malformed(BaseModel):
        mcp: MalformedMcp = MalformedMcp()

    with pytest.raises(profiles.ProductProfileUnavailable):
        profiles.OPT_IN_PRODUCT_PROFILE.apply(Malformed(), service="core")


def test_missing_granted_declarations_are_named_without_partial_application(monkeypatch):
    install_declarations(monkeypatch)

    class EarlierDiscord(BaseModel):
        credential_form_expiry: bool = False

    class EarlierTurn(BaseModel):
        path: str = "legacy"

    def module(name):
        if name == "daimon.core.config":
            return SimpleNamespace(
                McpSettings=Mcp, DiscordSettings=EarlierDiscord, TurnSettings=EarlierTurn
            )
        return SimpleNamespace(Settings=Host)

    monkeypatch.setattr(profiles, "import_module", module)
    with pytest.raises(profiles.ProductProfileUnavailable) as refused:
        profiles.OPT_IN_PRODUCT_PROFILE.require_available()
    assert set(refused.value.missing) == {
        "core.discord.writers_none_notice",
        "core.discord.tenant_cap_waiting_card",
        "core.turn.native_footer_cost",
    }
    profiles.DEFAULT_PRODUCT_PROFILE.require_available()


def test_missing_queue_card_field_refuses_without_modifying_existing_settings():
    class EarlierDiscord(BaseModel):
        credential_form_expiry: bool = False
        writers_none_notice: bool = False

    class EarlierCore(BaseModel):
        mcp: Mcp = Mcp()
        discord: EarlierDiscord = EarlierDiscord()
        turn: Turn = Turn()

    original = EarlierCore()
    snapshot = original.model_dump()
    with pytest.raises(
        profiles.ProductProfileUnavailable, match="core.discord.tenant_cap_waiting_card"
    ):
        profiles.OPT_IN_PRODUCT_PROFILE.apply(original, service="core")
    assert original.model_dump() == snapshot
    assert profiles.DEFAULT_PRODUCT_PROFILE.apply(original, service="core").model_dump() == snapshot
