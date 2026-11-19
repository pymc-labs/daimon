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
from daimon.testing.shared_helpers import all_app_command_names as _all_app_command_names


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
