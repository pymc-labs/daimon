"""RED stubs for require_manage_guild decorator (Phase 50 Wave 0).

These tests MUST FAIL until Wave 1 implements require_manage_guild in
daimon.adapters.discord.checks.

Behavior spec:
  - Non-manage_guild member: ephemeral D-28 message sent, wrapped fn NOT called.
  - Member with manage_guild=True: wrapped fn IS called.
  - Plain User (not Member): ephemeral D-28 message sent, wrapped fn NOT called.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.checks import require_manage_guild

_D28_MESSAGE = "Changing my setup needs Manage Server — ask a server admin to use /agent-setup"


def _make_interaction(
    *,
    is_member: bool,
    administrator: bool = False,
    manage_guild: bool = False,
    is_owner: bool = False,
) -> Any:
    """Build a minimal fake Interaction whose .user is a Member or User.

    Mirrors the helper in billing_panel/tests/test_read.py so the same
    is_guild_admin cases apply to the require_manage_guild decorator.
    """
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()

    if is_member:
        member = MagicMock(spec=discord.Member)
        member.id = 42
        perms = MagicMock(spec=discord.Permissions)
        perms.administrator = administrator
        perms.manage_guild = manage_guild
        member.guild_permissions = perms
        interaction.user = member
        guild = MagicMock(spec=discord.Guild)
        guild.owner_id = 42 if is_owner else 999
        interaction.guild = guild
    else:
        interaction.user = MagicMock(spec=discord.User)
        interaction.guild = None

    return interaction


# ---- test helpers ----


class _FakeCog:
    """Minimal cog stub so the decorator can call self.*."""


async def _run_decorated(interaction: Any) -> None:
    """Apply the decorator to a no-op cog method and invoke it."""
    inner_called: list[bool] = []

    async def _inner(self: Any, inner_interaction: Any) -> None:
        inner_called.append(True)

    decorated = require_manage_guild(_inner)
    await decorated(_FakeCog(), interaction)
    # Store result as attribute for assertions
    interaction._inner_called = inner_called


# ---- tests ----


async def test_require_manage_guild_rejects_member_without_perms() -> None:
    interaction = _make_interaction(
        is_member=True, manage_guild=False, administrator=False, is_owner=False
    )
    await _run_decorated(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_args = interaction.response.send_message.call_args
    assert call_args.kwargs.get("ephemeral") is True or (
        len(call_args.args) > 0 and call_args.args[-1] is True
        if call_args.kwargs.get("ephemeral") is None
        else call_args.kwargs.get("ephemeral") is True
    ), "non-admin member rejected ephemerally"
    sent_msg = call_args.args[0] if call_args.args else call_args.kwargs.get("content", "")
    assert _D28_MESSAGE in sent_msg, "D-28 message must be sent on rejection"
    assert not interaction._inner_called, (
        "non-admin member rejected ephemerally — inner fn not called"
    )


async def test_require_manage_guild_passes_manage_guild_member() -> None:
    interaction = _make_interaction(is_member=True, manage_guild=True)
    await _run_decorated(interaction)

    interaction.response.send_message.assert_not_awaited()
    assert interaction._inner_called, "member with manage_guild should pass the gate"


async def test_require_manage_guild_rejects_plain_user() -> None:
    interaction = _make_interaction(is_member=False)
    await _run_decorated(interaction)

    interaction.response.send_message.assert_awaited_once()
    call_args = interaction.response.send_message.call_args
    assert call_args.kwargs.get("ephemeral") is True, (
        "plain User (not Member) must be rejected ephemerally"
    )
    assert not interaction._inner_called, "plain User must not pass the gate"
