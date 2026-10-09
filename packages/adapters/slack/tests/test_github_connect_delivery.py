"""A bare connection confirms in a private Slack DM."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.slack import app as slack_app
from daimon.core.github_connect_delivery import ConnectNotice


class _Sessions:
    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_bare_connect_confirms_in_requester_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(
        slack_app, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="T1"))
    )
    client = SimpleNamespace(
        conversations_open=AsyncMock(return_value={"channel": {"id": "D1"}}),
        chat_postMessage=AsyncMock(),
    )
    resolver = AsyncMock(return_value=client)
    monkeypatch.setattr(slack_app, "resolve_web_client", resolver)
    app = SimpleNamespace(runtime=SimpleNamespace(sessionmaker=_Sessions()))
    notice = ConnectNotice(
        token_hash="abc",
        tenant_id=tenant_id,
        requester_platform_user_id="U1",
        agent_name="ResearchBot",
        connected_repos=[{"name": "private/repo", "access": "read"}],
        notice_claimed_at=datetime.now(UTC),
    )
    assert await slack_app.SlackApp._send_connect_notice(app, notice)  # type: ignore[arg-type]
    resolver.assert_awaited_once_with(app.runtime, team_id="T1")
    client.conversations_open.assert_awaited_once_with(users="U1")
    client.chat_postMessage.assert_awaited_once_with(
        channel="D1", text="Connected private/repo, Read only. Ready."
    )
