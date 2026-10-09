"""Connect confirmations stay in private Slack interaction surfaces."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aioresponses import aioresponses
from cryptography.fernet import Fernet
from daimon.adapters.slack import app as slack_app
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.github_connect_notices import ConnectNotice
from pydantic import SecretStr


class _Sessions:
    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_bare_connect_confirms_ephemerally_in_origin_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(
        slack_app, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="T1"))
    )
    client = SimpleNamespace(
        conversations_open=AsyncMock(),
        chat_postMessage=AsyncMock(),
        chat_postEphemeral=AsyncMock(),
    )
    resolver = AsyncMock(return_value=client)
    monkeypatch.setattr(slack_app, "resolve_web_client", resolver)
    app = SimpleNamespace(runtime=SimpleNamespace(sessionmaker=_Sessions()))
    notice = ConnectNotice(
        token_hash="abc",
        tenant_id=tenant_id,
        requester_platform_user_id="U1",
        agent_name="ResearchBot",
        origin_parent_channel_id="C1",
        origin_thread_id="123.456",
        connected_repos=[{"name": "private/repo", "access": "read"}],
        notice_claimed_at=datetime.now(UTC),
    )
    assert await slack_app.SlackApp._send_connect_notice(app, notice)  # type: ignore[arg-type]
    resolver.assert_awaited_once_with(app.runtime, team_id="T1")
    client.conversations_open.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()
    client.chat_postEphemeral.assert_awaited_once_with(
        channel="C1",
        user="U1",
        thread_ts="123.456",
        text="Connected private/repo, Read only. Ready.",
    )


@pytest.mark.asyncio
async def test_slash_connect_confirms_ephemerally_without_dm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        slack_app, "get_tenant", AsyncMock(return_value=SimpleNamespace(external_id="T1"))
    )
    client = SimpleNamespace(
        conversations_open=AsyncMock(), chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock()
    )
    monkeypatch.setattr(slack_app, "resolve_web_client", AsyncMock(return_value=client))
    key = SecretStr(Fernet.generate_key().decode())
    app = SimpleNamespace(
        runtime=SimpleNamespace(
            sessionmaker=_Sessions(), settings=SimpleNamespace(crypto=SimpleNamespace(keys=[key]))
        )
    )
    notice = ConnectNotice(
        token_hash="abc",
        tenant_id=uuid.uuid4(),
        requester_platform_user_id="U1",
        agent_name="ResearchBot",
        origin_parent_channel_id="C1",
        encrypted_origin_followup=encrypt_token(
            build_multifernet((key.get_secret_value(),)),
            "https://hooks.slack.com/services/test",
        ),
        origin_followup_expires_at=datetime.now(UTC).replace(year=2099),
        connected_repos=[{"name": "private/repo", "access": "read"}],
        notice_claimed_at=datetime.now(UTC),
    )
    with aioresponses() as mock:
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            "https://hooks.slack.com/services/test", status=200, body="ok"
        )
        assert await slack_app.SlackApp._send_connect_notice(app, notice)  # type: ignore[arg-type]
    client.conversations_open.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()
    client.chat_postEphemeral.assert_not_awaited()
