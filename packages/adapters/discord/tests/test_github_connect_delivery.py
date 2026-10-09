"""A bare connection confirms privately, once the browser transaction commits."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.github_connect_delivery import ConnectNotice
from daimon.core.stores import tenants


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
        connected_repos=[{"name": "private/repo", "access": "write"}],
        notice_claimed_at=datetime.now(UTC),
    )
    assert await DaimonBot._send_connect_notice(bot, notice)  # type: ignore[arg-type]
    bot.open_member_dm.assert_awaited_once_with(123, 456)
    assert dm.send.await_args.args[0] == "Connected private/repo, Read and write. Ready."
