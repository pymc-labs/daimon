"""Fixtures for agent_setup tests."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.testing.ma import build_stub_anthropic


@pytest.fixture
def make_stub_anthropic() -> Callable[
    [Callable[[httpx.Request], httpx.Response] | None], AsyncAnthropic
]:
    return build_stub_anthropic


@pytest.fixture
def stub_anthropic() -> AsyncAnthropic:
    return build_stub_anthropic()


@pytest.fixture
def mock_interaction() -> MagicMock:
    """A discord.Interaction stand-in — Discord boundary mock (allowed)."""
    interaction = MagicMock()
    interaction.user.id = 42
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


@pytest.fixture(autouse=True)
def _stub_panel_tenant_resolution(monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID) -> None:
    """Stub per-interaction tenant resolution for panel/modal callback unit tests.

    Panel callbacks resolve the guild's tenant via ``resolve_tenant_for_panel``
    which hits the tenants table. These unit tests use a stub
    anthropic + MagicMock interaction with no real DB, so we resolve straight
    to the in-scope ``tenant_id`` fixture — the value the callbacks thread into
    the write helpers, which is exactly what the assertions check.
    """
    resolver = AsyncMock(return_value=tenant_id)
    monkeypatch.setattr("daimon.adapters.discord.agent_setup.panel._resolve_tenant", resolver)
    monkeypatch.setattr("daimon.adapters.discord.agent_setup.edit_view._resolve_tenant", resolver)
    monkeypatch.setattr(
        "daimon.adapters.discord.agent_setup.modals.resolve_tenant_for_panel", resolver
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.agent_setup.modals_mcp.resolve_tenant_for_panel", resolver
    )
