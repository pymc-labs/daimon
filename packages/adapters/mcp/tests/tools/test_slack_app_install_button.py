"""Tests for tools/slack/_app_install_button.py, driven through the shared
post_github_app_install_link impl so the platform dispatch is exercised too.

Slack Web API calls are faked at transport level with aioresponses, the same
way the Slack send tests do it.
"""

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from aioresponses import aioresponses
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.github_app import (
    _post_app_install_link_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import (
    AnthropicSettings,
    CredentialsSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

_CONVERSATIONS_INFO = re.compile(r"https://slack\.com/api/conversations\.info.*")
_CONVERSATIONS_REPLIES = re.compile(r"https://slack\.com/api/conversations\.replies.*")
_USERS_INFO = re.compile(r"https://slack\.com/api/users\.info.*")
_CHAT_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
_POST_KEY = ("POST", URL(_CHAT_POST_MESSAGE))

_FULL_MEMBER = {"ok": True, "user": {"id": "U_CALLER", "is_restricted": False}}
_INSTALL_URL = "https://github.com/apps/acme-daimon/installations/new"

pytestmark = pytest.mark.asyncio


def _auth() -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="slack",
        external_id="T_TEST",
        platform_user_id="U_CALLER",
    )


async def _make_runtime(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> McpRuntime:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    fernet = build_multifernet((fernet_key.get_secret_value(),))
    async with committing_sessionmaker() as session:
        await upsert_slack_bot_token(
            session, team_id="T_TEST", encrypted_token=encrypt_token(fernet, "xoxb-secret")
        )
        await session.commit()
    settings = Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://daimon:daimon@localhost:5432/daimon"),
        ),
        anthropic=AnthropicSettings(
            api_key=SecretStr("sk-test"),
            base_url=HttpUrl("https://api.anthropic.com"),
        ),
        crypto=CryptoSettings(keys=(fernet_key,)),
        credentials=CredentialsSettings(google_sa_json=None),
        mcp=McpSettings(),
        github=GithubSettings(app_slug="acme-daimon"),
        slack=None,
    )
    return McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=settings,
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )


def _mock_public_channel_access(m: aioresponses) -> None:
    m.get(  # pyright: ignore[reportUnknownMemberType]
        _CONVERSATIONS_INFO,
        payload={"ok": True, "channel": {"id": "C1", "name": "general", "is_private": False}},
    )
    m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]


def _post_body(m: aioresponses) -> dict[str, Any]:
    return m.requests[_POST_KEY][0].kwargs["json"]  # type: ignore[no-any-return]


def _only_button(body: dict[str, Any]) -> dict[str, Any]:
    actions = [b for b in body["blocks"] if b["type"] == "actions"]
    assert len(actions) == 1, "exactly one actions block must be posted"
    elements = actions[0]["elements"]
    assert len(elements) == 1, "exactly one button must be posted"
    return elements[0]


async def test_slack_caller_posts_a_url_button_carrying_the_install_url(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(_CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000001.000100"})  # pyright: ignore[reportUnknownMemberType]
        result = await _post_app_install_link_impl(
            runtime, _auth(), channel_id="C1", purpose="reading a private repo"
        )
        body = _post_body(m)

    assert result.message_id == "1700000001.000100", "message_id is the posted ts on Slack"
    assert result.channel_id == "C1"
    assert result.install_url == _INSTALL_URL
    button = _only_button(body)
    assert button["url"] == _INSTALL_URL, "the button opens the built install URL"
    assert "value" not in button, "a url button carries nothing for the bot process to dispatch"
    assert "<@U_CALLER>" in body["text"], "the notification text names the requester"
    assert "thread_ts" not in body, "a bare channel target posts at the channel root"


async def test_slack_composite_target_validates_the_thread_and_posts_into_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={"ok": True, "messages": [{"ts": "1700000000.000001", "text": "root"}]},
        )
        m.post(_CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000002.000100"})  # pyright: ignore[reportUnknownMemberType]
        await _post_app_install_link_impl(
            runtime,
            _auth(),
            channel_id="C1:1700000000.000001",
            purpose="reading a private repo",
        )
        body = _post_body(m)

    assert body["channel"] == "C1"
    assert body["thread_ts"] == "1700000000.000001", "the button lands in the requested thread"


async def test_slack_nonexistent_thread_target_posts_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.get(_CONVERSATIONS_REPLIES, payload={"ok": False, "error": "thread_not_found"})  # pyright: ignore[reportUnknownMemberType]
        with pytest.raises(ToolError, match="does not exist"):
            await _post_app_install_link_impl(
                runtime, _auth(), channel_id="C1:9.9", purpose="reading a private repo"
            )
        assert _POST_KEY not in m.requests, "a bad thread target must post nothing"


async def test_slack_private_channel_nonmember_posts_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C1", "name": "secret", "is_private": True}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            re.compile(r"https://slack\.com/api/conversations\.members.*"),
            payload={"ok": True, "members": ["U_SOMEONE_ELSE"]},
        )
        with pytest.raises(ToolError):
            await _post_app_install_link_impl(
                runtime, _auth(), channel_id="C1", purpose="reading a private repo"
            )
        assert _POST_KEY not in m.requests, "the requester must be able to see the channel"
