"""The deployment switch and workspace exclusions use one policy."""

import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from daimon.core.agent_identity import identity_enabled_for, resolve_agent_identity
from daimon.core.config import AgentIdentitySettings, Settings


def _settings(identity: AgentIdentitySettings) -> Settings:
    return cast(Settings, SimpleNamespace(agent_identity=identity))


def test_identity_defaults_and_global_switch() -> None:
    off = _settings(AgentIdentitySettings())
    on = _settings(AgentIdentitySettings(enabled=True))
    for platform, workspace in (("discord", "123"), ("slack", "T1"), ("teams", "tenant")):
        assert not identity_enabled_for(off, platform, workspace)
        assert identity_enabled_for(on, platform, workspace)


def test_discord_and_slack_exclusions_are_platform_specific() -> None:
    settings = _settings(
        AgentIdentitySettings(
            enabled=True,
            excluded_discord_guild_ids=["123"],
            excluded_slack_team_ids=["T1"],
        )
    )
    assert not identity_enabled_for(settings, "discord", 123)
    assert identity_enabled_for(settings, "discord", 456)
    assert not identity_enabled_for(settings, "slack", "T1")
    assert identity_enabled_for(settings, "slack", "T2")
    assert identity_enabled_for(settings, "teams", "123")
    assert identity_enabled_for(settings, "discord", None)
    assert identity_enabled_for(settings, "slack", None)


@pytest.mark.asyncio
async def test_excluded_workspace_resolves_as_builtin_without_avatar_lookup() -> None:
    settings = _settings(AgentIdentitySettings(enabled=True, excluded_slack_team_ids=["T1"]))
    session = AsyncMock()
    identity = await resolve_agent_identity(
        session,
        tenant_id=uuid.uuid4(),
        agent_name="Analyst",
        is_builtin=False,
        public_base_url="https://example.test",
        enabled=identity_enabled_for(settings, "slack", "T1"),
    )
    assert identity.builtin and identity.avatar_url is None
    session.execute.assert_not_awaited()
