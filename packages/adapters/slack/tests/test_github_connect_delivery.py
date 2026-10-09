"""A bare connection confirms in a private Slack DM."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.slack import app as slack_app
from daimon.core.stores.github_connect_notices import ConnectNotice


class _Sessions:
    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin_parent_channel_id", "origin_thread_id"),
    [(None, None), ("D0", "123.456")],
)
async def test_bare_connect_confirms_in_requester_dm(
    monkeypatch: pytest.MonkeyPatch,
    origin_parent_channel_id: str | None,
    origin_thread_id: str | None,
) -> None:
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
        origin_parent_channel_id=origin_parent_channel_id,
        origin_thread_id=origin_thread_id,
        connected_repos=[{"name": "private/repo", "access": "read"}],
        notice_claimed_at=datetime.now(UTC),
    )
    assert await slack_app.SlackApp._send_connect_notice(app, notice)  # type: ignore[arg-type]
    resolver.assert_awaited_once_with(app.runtime, team_id="T1")
    client.conversations_open.assert_awaited_once_with(users="U1")
    client.chat_postMessage.assert_awaited_once_with(
        channel="D1",
        text=(
            "GitHub connected for ResearchBot: private/repo, Read only. "
            "Mention ResearchBot in a channel to start."
        ),
    )


@pytest.mark.asyncio
async def test_connect_from_thread_posts_count_without_repo_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_app, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="T1"))
    )
    client = SimpleNamespace(conversations_open=AsyncMock(), chat_postMessage=AsyncMock())
    monkeypatch.setattr(slack_app, "resolve_web_client", AsyncMock(return_value=client))
    app = SimpleNamespace(runtime=SimpleNamespace(sessionmaker=_Sessions()))
    notice = ConnectNotice(
        token_hash="abc",
        tenant_id=uuid.uuid4(),
        requester_platform_user_id="U1",
        agent_name="ResearchBot",
        origin_parent_channel_id="C1",
        origin_thread_id="123.456",
        connected_repos=[{"name": "private/repo", "access": "read"}],
        notice_claimed_at=datetime.now(UTC),
    )
    assert await slack_app.SlackApp._send_connect_notice(app, notice)  # type: ignore[arg-type]
    client.conversations_open.assert_not_awaited()
    client.chat_postMessage.assert_awaited_once_with(
        channel="C1",
        thread_ts="123.456",
        text="GitHub connected: 1 repo(s), Read only. What should I do first?",
    )
