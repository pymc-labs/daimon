"""Channel names the web app asks Discord for stay inside the tenant's own server."""

from __future__ import annotations

import httpx
from cryptography.fernet import Fernet
from daimon.adapters.web.channel_names import channel_lookup_of, channel_names
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import platform_names
from daimon.core.config import DiscordSettings, Settings
from daimon.core.github_credentials import build_multifernet
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import web_settings

_CHANNELS = {
    "111": {"id": "111", "name": "general", "guild_id": "guild-1"},
    "222": {"id": "222", "name": "elsewhere", "guild_id": "guild-2"},
    "333": {"id": "333", "name": "direct"},
}


def _runtime(
    factory: async_sessionmaker[AsyncSession], asked: list[str], *, status: int = 200
) -> tuple[WebRuntime, httpx.AsyncClient]:
    def handle(request: httpx.Request) -> httpx.Response:
        channel_id = request.url.path.rsplit("/", 1)[-1]
        asked.append(channel_id)
        if status != 200 or channel_id not in _CHANNELS:
            return httpx.Response(status or 404, json={"message": "no"})
        return httpx.Response(200, json=_CHANNELS[channel_id])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        discord=DiscordSettings(bot_token=SecretStr("token")),
    )
    runtime = WebRuntime(
        settings=settings,
        web=web_settings(),
        sessionmaker=factory,
        http_client=http,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
    )
    return runtime, http


async def test_a_channel_of_another_server_is_not_named(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, http = _runtime(db_session_factory, [])
    lookup = channel_lookup_of(runtime)
    assert lookup is not None, "a deployment with a bot token can ask"
    async with http:
        own = await lookup("guild-1", "111")
        foreign = await lookup("guild-1", "222")
        direct = await lookup("guild-1", "333")
    assert own == "general", "a channel of the tenant's own server is named"
    assert foreign is None, "a channel of another server the bot sits in is refused"
    assert direct is None, "a channel with no server is refused"


async def test_a_foreign_channel_leaves_no_name_behind(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    platform_names.forget_all()
    tenant = await make_tenant(db_session, workspace_id="guild-1")
    await db_session.commit()
    asked: list[str] = []
    runtime, http = _runtime(db_session_factory, asked)
    async with http:
        platform, names = await channel_names(runtime, tenant.id, ["111", "222"])
        _, again = await channel_names(runtime, tenant.id, ["222"])
    assert platform == "discord", "the tenant's platform comes back"
    assert names == {"111": "general"}, "only the tenant's own channel is named"
    assert again == {}, "the foreign channel is still unnamed"
    assert asked.count("222") == 1, "a refused channel is not asked about again for a while"


async def test_lookup_failures_name_nothing_and_raise_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, http = _runtime(db_session_factory, [], status=403)
    lookup = channel_lookup_of(runtime)
    assert lookup is not None
    async with http:
        refused = await lookup("guild-1", "111")
    assert refused is None, "a 403 names nothing"
