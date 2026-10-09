"""Discord setup picture resets."""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.discord.agent_setup import avatar as avatar_module
from daimon.adapters.discord.agent_setup.avatar import reset_agent_avatar
from daimon.core.stores.agent_avatars import AvatarRow, get_or_create_avatar, replace_avatar
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (256, 256), "orange").save(output, format="PNG")
    return output.getvalue()


def _interaction(user_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.guild_id = 123
    return interaction


def _runtime(factory: async_sessionmaker[AsyncSession] | None = None) -> MagicMock:
    runtime = MagicMock()
    runtime.sessionmaker = factory
    runtime.deployment_default.agent_name = "daimon"
    runtime.settings.mcp.app_root_url = "https://mcp.example.com"
    runtime.settings.agent_identity.enabled = True
    return runtime


def test_avatar_url_uses_app_root_not_mcp_endpoint() -> None:
    runtime = _runtime()
    runtime.settings.mcp.public_url = "https://mcp.example.com/mcp"
    avatar = AvatarRow(token="token", sha256="abcdef123456more", png=b"", source="default")
    assert avatar_module.avatar_public_url(runtime, avatar) == (
        "https://mcp.example.com/avatars/token/abcdef123456.png"
    )


def test_upload_route_is_gone() -> None:
    assert not hasattr(avatar_module, "upload_agent_avatar")
    assert not hasattr(avatar_module, "replace_avatar")


@pytest.mark.parametrize("excluded", [False, True])
async def test_reset_refuses_when_agent_pictures_are_off_or_guild_excluded(
    monkeypatch: pytest.MonkeyPatch,
    excluded: bool,
) -> None:
    runtime = _runtime()
    if excluded:
        runtime.settings.agent_identity.excluded_discord_guild_ids = ["123"]
    else:
        runtime.settings.agent_identity.enabled = False
    lookup = AsyncMock()
    monkeypatch.setattr(avatar_module, "find_agent_by_daimon_tag", lookup)
    monkeypatch.setattr(avatar_module, "_audit", AsyncMock())
    message, avatar = await reset_agent_avatar(
        _interaction(), runtime, tenant_id=uuid.uuid4(), agent_name="Ada"
    )
    assert message == "Agent pictures are turned off."
    assert avatar is None
    lookup.assert_not_awaited()


@pytest.mark.parametrize("is_admin", [False, True])
async def test_reset_refuses_member_and_built_in(
    monkeypatch: pytest.MonkeyPatch, is_admin: bool
) -> None:
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: is_admin)
    monkeypatch.setattr(
        avatar_module,
        "find_agent_by_daimon_tag",
        AsyncMock(return_value=SimpleNamespace(name="daimon", metadata={"daimon_managed": "true"})),
    )
    audit = AsyncMock()
    monkeypatch.setattr(avatar_module, "_audit", audit)

    _message, result = await reset_agent_avatar(
        _interaction(), _runtime(), tenant_id=uuid.uuid4(), agent_name="daimon"
    )

    assert result is None
    assert audit.await_args.kwargs["outcome"] == "denied"


async def test_use_default_replaces_an_existing_uploaded_picture(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        uploaded = await replace_avatar(
            session, tenant_id=tenant.id, agent_name="analyst", png=_png(), source="upload"
        )
    runtime = _runtime(db_session_factory)
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: True)
    monkeypatch.setattr(
        avatar_module,
        "find_agent_by_daimon_tag",
        AsyncMock(return_value=SimpleNamespace(name="analyst", metadata={})),
    )

    message, restored = await reset_agent_avatar(
        _interaction(), runtime, tenant_id=tenant.id, agent_name="analyst"
    )

    assert message == "Default picture restored."
    assert restored is not None
    assert restored.source == "default"
    assert restored.token != uploaded.token
    async with db_session_factory() as session:
        current = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="analyst")
        events = await list_events(session, tenant_id=tenant.id)
    assert (current.token, current.source) == (restored.token, "default")
    assert [(event.operation, event.agent_name) for event in events] == [
        ("agent_avatar_reset", "analyst")
    ]
