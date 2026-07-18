"""Cog metadata invariants: guild_only, registration topology, admin-gating.

ConfigCog has been deleted.
/propagate and /unpropagate were folded into /agent-setup and
deleted the propagate cog and its package. The assertions here lock the
current cog set so a future re-introduction of ConfigCog or the propagate
command module fails CI.
"""

from __future__ import annotations

import inspect

import pytest
from daimon.adapters.discord.commands.agent_setup import AgentSetupCog
from daimon.adapters.discord.commands.billing import BillingCog
from daimon.adapters.discord.commands.help import HelpCog
from daimon.adapters.discord.commands.privacy import PrivacyCog
from daimon.adapters.discord.commands.routines import RoutinesCog
from discord import app_commands
from discord.ext import commands


@pytest.mark.parametrize(
    "cog_cls",
    [
        pytest.param("HelpCog", id="help"),
        pytest.param("AgentSetupCog", id="agent_setup"),
        pytest.param("RoutinesCog", id="routines"),
        pytest.param("BillingCog", id="billing"),
    ],
)
def test_guild_only_on_all_cogs(cog_cls: str) -> None:
    """Each Cog (group or flat) must have guild_only set via @app_commands.guild_only()."""
    cls = {
        "HelpCog": HelpCog,
        "AgentSetupCog": AgentSetupCog,
        "RoutinesCog": RoutinesCog,
        "BillingCog": BillingCog,
    }[cog_cls]

    assert getattr(cls, "__discord_app_commands_guild_only__", False) is True, (  # type: ignore[attr-defined]
        f"{cog_cls} must have @app_commands.guild_only() decorator"
    )


def _all_app_command_names(cog_cls: type[commands.Cog]) -> set[str]:
    """Return the set of slash-command names registered by ``cog_cls``."""
    names: set[str] = set()
    for cmd in cog_cls.__cog_app_commands__:  # type: ignore[attr-defined]
        if isinstance(cmd, app_commands.Group):
            names.add(cmd.name)
            for child in cmd.commands:
                names.add(f"{cmd.name} {child.name}")
        else:
            names.add(cmd.name)
    return names


def test_config_cog_not_importable() -> None:
    """ConfigCog must be gone after D-CONFIG-01 — the import must fail.

    Plan 26-PLAN T10 deleted ``commands/config.py`` outright; this regression
    test guards against a future re-introduction by asserting the module is
    no longer importable.
    """
    with pytest.raises(ModuleNotFoundError):
        import daimon.adapters.discord.commands.config  # type: ignore[import-not-found]  # noqa: F401  intentional: must fail


def test_propagate_cog_not_importable() -> None:
    """commands/propagate.py is deleted; the import must fail."""
    with pytest.raises(ModuleNotFoundError):
        import daimon.adapters.discord.commands.propagate  # type: ignore[import-not-found]  # noqa: F401  intentional: must fail


def test_propagate_package_not_importable() -> None:
    """The propagate/ adapter package is deleted; the import must fail."""
    with pytest.raises(ModuleNotFoundError):
        import daimon.adapters.discord.propagate.panel  # type: ignore[import-not-found]  # noqa: F401  intentional: must fail


def test_routines_cog_registers_routines_slash() -> None:
    """RoutinesCog must own /routines (DUX-03)."""
    names = _all_app_command_names(RoutinesCog)
    assert "routines" in names, f"/routines must be registered on RoutinesCog; got {names!r}"


def test_routines_slash_is_admin_gated() -> None:
    """The /routines panel is admin-only and hidden from non-admins.

    /routines is a pure-admin command: hidden in the Discord UI
    via @app_commands.default_permissions(manage_guild=True) and hard-gated with
    @require_manage_guild (defense in depth). It is NOT the DB-role @require_admin.
    """
    import daimon.adapters.discord.commands.routines as routines_module

    src = inspect.getsource(routines_module)
    assert "require_manage_guild" in src, (
        f"/routines must be admin-gated with @require_manage_guild; not found in source:\n{src}"
    )
    assert "default_permissions(manage_guild=True)" in src, (
        "/routines must be hidden from non-admins via "
        f"@app_commands.default_permissions(manage_guild=True); not found in source:\n{src}"
    )


def test_routines_slash_uses_require_registered_guild() -> None:
    """The /routines slash must keep @require_registered_guild."""
    import daimon.adapters.discord.commands.routines as routines_module

    src = inspect.getsource(routines_module)
    assert "@require_registered_guild" in src, (
        "routines.py must keep @require_registered_guild on the /routines handler"
    )


def test_billing_cog_registers_billing_slash() -> None:
    """BillingCog must own /billing (DUX-04)."""
    names = _all_app_command_names(BillingCog)
    assert "billing" in names, f"/billing must be registered on BillingCog; got {names!r}"


def test_billing_slash_is_not_admin_gated() -> None:
    """The /billing panel is open-read; @require_admin must NOT appear."""
    import daimon.adapters.discord.commands.billing as billing_module

    src = inspect.getsource(billing_module)
    assert "require_admin" not in src, (
        f"/billing must be open-read (Discord-native admin resolution at click time); "
        f"found @require_admin in source:\n{src}"
    )


def test_billing_slash_uses_require_registered_guild() -> None:
    """The /billing slash must keep @require_registered_guild."""
    import daimon.adapters.discord.commands.billing as billing_module

    src = inspect.getsource(billing_module)
    assert "@require_registered_guild" in src, (
        "billing.py must keep @require_registered_guild on the /billing handler"
    )


# ---- PrivacyCog inverse invariants ----------------------------------------
#
# /privacy is per-person and must be invocable from DM AND guild. Unlike every
# other Cog in this file, PrivacyCog must NOT carry @app_commands.guild_only()
# — a future contributor adding the decorator would silently break DM access.
# These tests lock the absence of guild_only and the presence of DM-capable
# context registration.


def test_privacy_cog_is_not_guild_only() -> None:
    """Privacy is per-person, available in DM AND guild.

    PrivacyCog MUST NOT carry @app_commands.guild_only(). A future contributor
    adding the decorator would silently break DM access; this test fails first.
    """
    assert getattr(PrivacyCog, "__discord_app_commands_guild_only__", False) is False, (
        "PrivacyCog MUST NOT have @app_commands.guild_only() — "
        "/privacy is invoker-personal, DM-capable"
    )


def test_privacy_cog_registers_privacy_slash() -> None:
    """PrivacyCog owns /privacy."""
    names = _all_app_command_names(PrivacyCog)
    assert "privacy" in names, f"/privacy must be registered on PrivacyCog; got {names!r}"


def test_privacy_slash_allows_dms() -> None:
    """/privacy must be invocable in DM context.

    discord.py 2.4+ replaced @app_commands.dm_permission with the more
    granular @app_commands.allowed_contexts(guilds, dms, private_channels)
    decorator. We assert the runtime-attached AppCommandContext exposes
    dm_channel=True so Discord lists /privacy in the bot's DM channel.
    """
    cmd: app_commands.Command[PrivacyCog, ..., None] | None = None
    for c in PrivacyCog.__cog_app_commands__:  # type: ignore[attr-defined]
        if isinstance(c, app_commands.Command) and c.name == "privacy":
            cmd = c
            break
    assert cmd is not None, "PrivacyCog must register a /privacy command"
    ctx = cmd.allowed_contexts
    assert ctx is not None, (
        "PrivacyCog's /privacy command must declare allowed_contexts "
        "(guilds=True, dms=True, private_channels=True) to be DM-invocable."
    )
    assert ctx.dm_channel is True, (
        f"/privacy must allow DM invocation (allowed_contexts.dm_channel=True); got {ctx!r}"
    )
    assert ctx.guild is True, f"/privacy must also allow guild invocation; got {ctx!r}"
