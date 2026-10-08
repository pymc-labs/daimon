"""Discord setup avatar uploads and resets."""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from daimon.adapters.discord.agent_setup import avatar as avatar_module
from daimon.adapters.discord.agent_setup.avatar import reset_agent_avatar, upload_agent_avatar
from daimon.core.stores.agent_avatars import AvatarRow, get_or_create_avatar
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (400, 300), "orange").save(output, format="PNG")
    return output.getvalue()


def _attachment(
    *,
    size: int | None = None,
    content_type: str = "image/png",
    url: str = "https://cdn.discordapp.com/attachments/123/456/avatar.png",
) -> MagicMock:
    body = _image()
    attachment = MagicMock(spec=discord.Attachment)
    attachment.size = len(body) if size is None else size
    attachment.content_type = content_type
    attachment.url = url
    attachment.read = AsyncMock(return_value=body)
    return attachment


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


@pytest.mark.parametrize("excluded", [False, True])
async def test_upload_refuses_when_agent_pictures_are_off_or_guild_excluded(
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
    attachment = _attachment()
    message, avatar = await upload_agent_avatar(
        _interaction(), runtime, tenant_id=uuid.uuid4(), agent_name="Ada", attachment=attachment
    )
    assert message == "Agent pictures are turned off."
    assert avatar is None
    lookup.assert_not_awaited()
    attachment.read.assert_not_awaited()


@pytest.mark.parametrize("is_admin", [False, True])
async def test_upload_refuses_member_and_built_in(
    monkeypatch: pytest.MonkeyPatch, is_admin: bool
) -> None:
    runtime = _runtime()
    attachment = _attachment()
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: is_admin)
    monkeypatch.setattr(
        avatar_module,
        "find_agent_by_daimon_tag",
        AsyncMock(return_value=SimpleNamespace(name="daimon", metadata={"daimon_managed": "true"})),
    )
    audit = AsyncMock()
    monkeypatch.setattr(avatar_module, "_audit", audit)

    _message, result = await upload_agent_avatar(
        _interaction(), runtime, tenant_id=uuid.uuid4(), agent_name="daimon", attachment=attachment
    )

    assert result is None
    attachment.read.assert_not_awaited()
    assert audit.await_args.kwargs["outcome"] == "denied"


@pytest.mark.parametrize(
    ("size", "content_type"),
    [(2 * 1024 * 1024 + 1, "image/png"), (100, "application/octet-stream")],
)
async def test_upload_rejects_oversize_and_wrong_type_before_read(
    monkeypatch: pytest.MonkeyPatch, size: int, content_type: str
) -> None:
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: True)
    monkeypatch.setattr(
        avatar_module,
        "find_agent_by_daimon_tag",
        AsyncMock(return_value=SimpleNamespace(name="analyst", metadata={})),
    )
    audit = AsyncMock()
    monkeypatch.setattr(avatar_module, "_audit", audit)
    attachment = _attachment(size=size, content_type=content_type)

    _message, result = await upload_agent_avatar(
        _interaction(),
        _runtime(),
        tenant_id=uuid.uuid4(),
        agent_name="analyst",
        attachment=attachment,
    )

    assert result is None
    attachment.read.assert_not_awaited()
    assert audit.await_args.kwargs["outcome"] == "error"


async def test_upload_rechecks_agent_after_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: True)
    lookup = AsyncMock(side_effect=[SimpleNamespace(name="analyst", metadata={}), None])
    monkeypatch.setattr(avatar_module, "find_agent_by_daimon_tag", lookup)
    audit = AsyncMock()
    monkeypatch.setattr(avatar_module, "_audit", audit)
    attachment = _attachment()
    runtime = _runtime()

    message, result = await upload_agent_avatar(
        _interaction(),
        runtime,
        tenant_id=uuid.uuid4(),
        agent_name="analyst",
        attachment=attachment,
    )

    assert result is None and "no longer available" in message
    attachment.read.assert_awaited_once()
    assert lookup.await_count == 2
    assert audit.await_args.kwargs["outcome"] == "denied"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/avatar.png",
        "https://cdn.discordapp.com.evil.test/avatar.png",
        "http://cdn.discordapp.com/avatar.png",
        "https://cdn.discordapp.com:8443/avatar.png",
    ],
)
async def test_upload_rejects_untrusted_attachment_url(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: True)
    monkeypatch.setattr(
        avatar_module,
        "find_agent_by_daimon_tag",
        AsyncMock(return_value=SimpleNamespace(name="analyst", metadata={})),
    )
    audit = AsyncMock()
    monkeypatch.setattr(avatar_module, "_audit", audit)
    attachment = _attachment(url=url)
    message, result = await upload_agent_avatar(
        _interaction(),
        _runtime(),
        tenant_id=uuid.uuid4(),
        agent_name="analyst",
        attachment=attachment,
    )
    assert result is None and "couldn't use that picture" in message
    attachment.read.assert_not_awaited()
    assert audit.await_args.kwargs["outcome"] == "error"


async def test_upload_handles_attachment_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: True)
    monkeypatch.setattr(
        avatar_module,
        "find_agent_by_daimon_tag",
        AsyncMock(return_value=SimpleNamespace(name="analyst", metadata={})),
    )
    audit = AsyncMock()
    monkeypatch.setattr(avatar_module, "_audit", audit)
    attachment = _attachment()
    attachment.read.side_effect = aiohttp.ClientError("connection lost")
    message, result = await upload_agent_avatar(
        _interaction(),
        _runtime(),
        tenant_id=uuid.uuid4(),
        agent_name="analyst",
        attachment=attachment,
    )
    assert result is None and "couldn't read that file" in message
    assert audit.await_args.kwargs["outcome"] == "error"


@pytest.mark.parametrize(
    "body, expected", [(b"", "We couldn't read that file."), (b"animated", "Use a still picture.")]
)
async def test_upload_explains_empty_and_animated_files(
    monkeypatch: pytest.MonkeyPatch, body: bytes, expected: str
) -> None:
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: True)
    monkeypatch.setattr(
        avatar_module,
        "find_agent_by_daimon_tag",
        AsyncMock(return_value=SimpleNamespace(name="analyst", metadata={})),
    )
    monkeypatch.setattr(avatar_module, "_audit", AsyncMock())
    attachment = _attachment(size=len(body))
    if body == b"animated":
        output = io.BytesIO()
        first = Image.new("RGB", (20, 20), "red")
        second = Image.new("RGB", (20, 20), "blue")
        first.save(output, format="GIF", save_all=True, append_images=[second])
        body = output.getvalue()
        attachment.size = len(body)
    attachment.read.return_value = body

    message, avatar = await upload_agent_avatar(
        _interaction(),
        _runtime(),
        tenant_id=uuid.uuid4(),
        agent_name="analyst",
        attachment=attachment,
    )

    assert avatar is None
    assert expected in message


async def test_upload_and_reset_rotate_tokens_and_sources(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        original = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="analyst")
    runtime = _runtime(db_session_factory)
    monkeypatch.setattr(avatar_module, "is_guild_admin", lambda _interaction: True)
    monkeypatch.setattr(
        avatar_module,
        "find_agent_by_daimon_tag",
        AsyncMock(return_value=SimpleNamespace(name="analyst", metadata={})),
    )
    interaction = _interaction()
    attachment = _attachment()

    _message, uploaded = await upload_agent_avatar(
        interaction,
        runtime,
        tenant_id=tenant.id,
        agent_name="analyst",
        attachment=attachment,
    )
    assert uploaded is not None
    assert uploaded.source == "upload"
    assert uploaded.token != original.token
    attachment.read.assert_awaited_once()

    _message, restored = await reset_agent_avatar(
        interaction, runtime, tenant_id=tenant.id, agent_name="analyst"
    )
    assert restored is not None
    assert restored.source == "default"
    assert restored.token not in {original.token, uploaded.token}
    async with db_session_factory() as session:
        current = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="analyst")
        events = await list_events(session, tenant_id=tenant.id)
    assert current.token == restored.token
    assert {event.operation for event in events} == {"agent_avatar_change", "agent_avatar_reset"}
    assert all(event.agent_name == "analyst" for event in events)
