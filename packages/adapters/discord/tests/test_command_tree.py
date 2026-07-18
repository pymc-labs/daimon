"""Regression tests for the Discord slash-command tree.

Plan 25-02 deleted the legacy ``/agents``, ``/skills``, ``/environments`` cogs.
Plan 25-03 added ``/agent-setup``. These tests lock both invariants so a
future re-introduction of a deprecated slash, or accidental removal of
``/agent-setup``, fails CI.
"""

from __future__ import annotations

import inspect

from daimon.adapters.discord.commands.agent_setup import AgentSetupCog
from daimon.adapters.discord.commands.help import HelpCog
from discord import app_commands
from discord.ext import commands


def _all_app_command_names(cog_cls: type[commands.Cog]) -> set[str]:
    """Return the set of slash-command names registered by ``cog_cls``.

    For ``GroupCog`` subclasses, each child command's full path is included
    (group + child) — for regular ``Cog`` subclasses, the top-level command
    names are returned.
    """
    names: set[str] = set()
    for cmd in cog_cls.__cog_app_commands__:  # type: ignore[attr-defined]
        if isinstance(cmd, app_commands.Group):
            names.add(cmd.name)
            for child in cmd.commands:
                names.add(f"{cmd.name} {child.name}")
        else:
            names.add(cmd.name)
    return names


def test_agent_setup_slash_is_registered() -> None:
    """``/agent-setup`` must be present on AgentSetupCog after Plan 25-03."""
    names = _all_app_command_names(AgentSetupCog)
    assert "agent-setup" in names, (
        f"/agent-setup must be registered on AgentSetupCog; got {names!r}"
    )


def test_deprecated_slashes_are_unregistered() -> None:
    """``/agents``, ``/skills``, ``/environments`` must not exist on any current Cog.

    Plan 25-02 deleted these slashes; this test guards against accidental
    re-introduction by enumerating every Cog the bot's ``setup_hook`` loads
    and asserting the deprecated names are absent.
    """
    all_cog_names: set[str] = set()
    for cog_cls in (HelpCog, AgentSetupCog):
        all_cog_names |= _all_app_command_names(cog_cls)

    deprecated = {"agents", "skills", "environments", "propagate", "unpropagate"}
    leaked = deprecated & all_cog_names
    assert not leaked, (
        f"deprecated slashes must remain unregistered; found {leaked!r} in {all_cog_names!r}"
    )


def test_agent_setup_slash_does_not_require_admin() -> None:
    """``/agent-setup`` is per-user: guard against accidental admin gating.

    The source file decorates the handler with ``@require_registered_guild`` but
    NOT ``@require_admin``. We inspect the source as the simplest unambiguous
    signal — the decorator chain is preserved by ``functools.wraps`` but loses
    the wrapper's own identity, so source inspection is more reliable than
    walking ``__wrapped__``.
    """
    import daimon.adapters.discord.commands.agent_setup as module

    source = inspect.getsource(module)
    # The handler must be guarded by registered-guild — that's per-user trust scope.
    assert "@require_registered_guild" in source, (
        "agent_setup.py must keep @require_registered_guild on the /agent-setup handler"
    )
    # The handler must NOT be guarded by admin role — per-user, not per-admin.
    assert "@require_admin" not in source, (
        "agent_setup.py must not gate /agent-setup behind @require_admin (per-user panel)"
    )
