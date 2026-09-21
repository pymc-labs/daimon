"""Fixtures for agent_setup tests."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.testing.ma import make_stub_anthropic, stub_anthropic  # noqa: F401


@pytest.fixture
def mock_interaction() -> MagicMock:
    """A discord.Interaction stand-in — Discord boundary mock (allowed).

    ``user`` is spec'd as a live guild admin (``discord.Member`` with
    ``administrator=True``) so `authz.refuse_if_reachable_and_not_admin`'s
    real admin short-circuit passes without a DB read — these unit tests
    otherwise carry no real Postgres session for it to read from. Tests that
    need a non-admin caller build their own interaction (see
    ``test_authz.py``).
    """
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.user.guild_permissions.administrator = True
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-0000000000aa")
