"""A bare connection confirms privately, once the browser transaction commits."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.stores import tenants
from daimon.core.stores.github_connect_notices import ConnectNotice


class _Sessions:
    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_bare_connect_confirms_in_requester_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(
        tenants, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="123"))
    )
    dm = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(
        runtime=SimpleNamespace(sessionmaker=_Sessions()),
        open_member_dm=AsyncMock(return_value=dm),
    )
    notice = ConnectNotice(
        token_hash="abc",
        tenant_id=tenant_id,
        requester_platform_user_id="456",
        agent_name="ResearchBot",
        origin_parent_channel_id=None,
        origin_thread_id=None,
        connected_repos=[{"name": "private/repo", "access": "write"}],
        notice_claimed_at=datetime.now(UTC),
    )
    assert await DaimonBot._send_connect_notice(bot, notice)  # type: ignore[arg-type]
    bot.open_member_dm.assert_awaited_once_with(123, 456)
    assert dm.send.await_args.args[0] == (
        "GitHub connected for ResearchBot: private/repo, Read and write. "
        "Mention ResearchBot in a channel to start."
    )


@pytest.mark.asyncio
async def test_connect_from_thread_posts_count_without_repo_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tenants, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="123"))
    )
    thread = MagicMock(spec=discord.abc.Messageable)
    thread.send = AsyncMock()
    bot = SimpleNamespace(
        runtime=SimpleNamespace(sessionmaker=_Sessions()),
        _channel_by_id=AsyncMock(return_value=thread),
        open_member_dm=AsyncMock(),
    )
    notice = ConnectNotice(
        token_hash="abc",
        tenant_id=uuid.uuid4(),
        requester_platform_user_id="456",
        agent_name="ResearchBot",
        origin_parent_channel_id="100",
        origin_thread_id="200",
        connected_repos=[{"name": "private/repo", "access": "write"}],
        notice_claimed_at=datetime.now(UTC),
    )
    assert await DaimonBot._send_connect_notice(bot, notice)  # type: ignore[arg-type]
    bot._channel_by_id.assert_awaited_once_with(200)
    bot.open_member_dm.assert_not_awaited()
    assert thread.send.await_args.args[0] == (
        "GitHub connected: 1 repo(s), Read and write. What should I do first?"
    )
