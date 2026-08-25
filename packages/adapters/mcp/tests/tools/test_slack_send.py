"""Tests for tools/slack/_send.py — send_message implementation."""

from __future__ import annotations

import re
import uuid
from unittest.mock import MagicMock

import pytest
from aioresponses import aioresponses
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._send import (  # pyright: ignore[reportPrivateUsage]
    _slack_send_message_impl,
)
from daimon.adapters.mcp.tools.slack._visibility import (
    MISSING_ACCESS,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import (
    AnthropicSettings,
    CredentialsSettings,
    CryptoSettings,
    DatabaseSettings,
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
_CONVERSATIONS_MEMBERS = re.compile(r"https://slack\.com/api/conversations\.members.*")
_USERS_INFO = re.compile(r"https://slack\.com/api/users\.info.*")
_CHAT_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
_POST_KEY = ("POST", URL(_CHAT_POST_MESSAGE))

_FULL_MEMBER = {"ok": True, "user": {"id": "U_CALLER", "is_restricted": False}}


def _auth(**overrides: object) -> AuthIdentity:
    base: dict[str, object] = {
        "account_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "role": Role.USER,
        "platform": "slack",
        "external_id": "T_TEST",
        "platform_user_id": "U_CALLER",
    }
    base.update(overrides)
    return AuthIdentity(**base)  # type: ignore[arg-type]  # test kwargs are shape-correct


def _build_settings(*, fernet_key: SecretStr) -> Settings:
    return Settings(
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
        slack=None,
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
    return McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=_build_settings(fernet_key=fernet_key),
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )


def _mock_public_channel_access(m: aioresponses) -> None:
    m.get(  # pyright: ignore[reportUnknownMemberType]
        _CONVERSATIONS_INFO,
        payload={"ok": True, "channel": {"id": "C1", "name": "general", "is_private": False}},
    )
    m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]


def _post_body(m: aioresponses) -> dict[str, object]:
    posts = m.requests[_POST_KEY]
    return posts[0].kwargs["json"]  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_send_message_channel_root_returns_row_and_posts_one_markdown_block(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000001.000100"}
        )
        row = await _slack_send_message_impl(
            runtime,
            auth,
            channel_id="C1",
            content="hello there",
            attachments=None,
            file_handles=None,
        )
        body = _post_body(m)

    assert row.ts == "1700000001.000100", "returned row must carry the posted ts"
    assert row.thread_ts is None, "a channel-root post must not carry a thread_ts"
    blocks = body["blocks"]
    assert isinstance(blocks, list) and len(blocks) == 1, "exactly one block must be posted"
    assert blocks[0] == {"type": "markdown", "text": "hello there"}, (
        "the markdown block text must be the content verbatim"
    )
    assert body["text"] == "hello there", "text fallback must be non-empty"
    assert "thread_ts" not in body, "a channel-root post must not carry a thread_ts key"


@pytest.mark.asyncio
async def test_send_message_content_with_mentions_and_bold_is_not_escaped(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    content = "hey <@U123> **bold** thing"
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000002.000200"}
        )
        await _slack_send_message_impl(
            runtime, auth, channel_id="C1", content=content, attachments=None, file_handles=None
        )
        body = _post_body(m)

    blocks = body["blocks"]
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == content, (
        "entities the agent deliberately authored must reach Slack unescaped"
    )


@pytest.mark.asyncio
async def test_send_message_long_content_bounds_notification_text_not_block_text(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    content = "x" * 3500
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000003.000300"}
        )
        await _slack_send_message_impl(
            runtime, auth, channel_id="C1", content=content, attachments=None, file_handles=None
        )
        body = _post_body(m)

    text = body["text"]
    assert isinstance(text, str) and len(text) == 3000, (
        "the text notification fallback must be bounded at 3000 chars"
    )
    blocks = body["blocks"]
    assert isinstance(blocks, list)
    assert blocks[0]["text"] == content, "the block text must carry the full, unbounded content"


@pytest.mark.asyncio
async def test_send_message_thread_composite_target_validates_then_posts_with_thread_ts(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    thread_ts = "1717171717.123456"
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={"ok": True, "messages": [{"ts": thread_ts, "user": "U_A", "text": "root"}]},
        )
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE,
            payload={
                "ok": True,
                "ts": "1700000004.000400",
                "message": {"thread_ts": thread_ts, "user": "U_BOT"},
            },
        )
        row = await _slack_send_message_impl(
            runtime,
            auth,
            channel_id=f"C1:{thread_ts}",
            content="a reply",
            attachments=None,
            file_handles=None,
        )
        body = _post_body(m)
        replies_calls = [
            reqs
            for (method, url), reqs in m.requests.items()
            if method == "GET" and "conversations.replies" in str(url)
        ]

    assert len(replies_calls) == 1 and len(replies_calls[0]) == 1, (
        "conversations.replies must be called exactly once before the post"
    )
    assert body["thread_ts"] == thread_ts, "the post must carry the validated thread_ts"
    assert row.thread_ts == thread_ts, (
        "the returned row's thread_ts is whatever message.thread_ts the response gave"
    )


@pytest.mark.asyncio
async def test_send_message_reply_ts_input_posts_as_given_row_reports_parent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    reply_ts = "1717171717.999999"
    parent_ts = "1717171717.123456"
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={
                "ok": True,
                "messages": [{"ts": reply_ts, "thread_ts": parent_ts, "user": "U_A", "text": "r"}],
            },
        )
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE,
            payload={
                "ok": True,
                "ts": "1700000005.000500",
                "message": {"thread_ts": parent_ts, "user": "U_BOT"},
            },
        )
        row = await _slack_send_message_impl(
            runtime,
            auth,
            channel_id=f"C1:{reply_ts}",
            content="a reply",
            attachments=None,
            file_handles=None,
        )
        body = _post_body(m)

    assert body["thread_ts"] == reply_ts, "the post uses the ts as given, unnormalized"
    assert row.thread_ts == parent_ts, (
        "Slack normalizes; the row reports the parent from the response"
    )


@pytest.mark.asyncio
async def test_send_message_nonexistent_thread_raises_before_any_post(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES, payload={"ok": False, "error": "thread_not_found"}
        )
        with pytest.raises(ToolError, match="thread"):
            await _slack_send_message_impl(
                runtime,
                auth,
                channel_id="C1:1717171717.000000",
                content="a reply",
                attachments=None,
                file_handles=None,
            )
        assert _POST_KEY not in m.requests, "chat.postMessage must never be called"


@pytest.mark.asyncio
async def test_send_message_im_channel_raises_missing_access_no_post(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO, payload={"ok": True, "channel": {"id": "D1", "is_im": True}}
        )
        with pytest.raises(ToolError, match=MISSING_ACCESS):
            await _slack_send_message_impl(
                runtime, auth, channel_id="D1", content="hi", attachments=None, file_handles=None
            )
        assert _POST_KEY not in m.requests


@pytest.mark.asyncio
async def test_send_message_private_channel_nonmember_raises_missing_access_no_post(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C_PRIV", "name": "sekret", "is_private": True}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_MEMBERS, payload={"ok": True, "members": ["U_SOMEONE_ELSE"]}
        )
        with pytest.raises(ToolError, match=MISSING_ACCESS):
            await _slack_send_message_impl(
                runtime,
                auth,
                channel_id="C_PRIV",
                content="hi",
                attachments=None,
                file_handles=None,
            )
        assert _POST_KEY not in m.requests


@pytest.mark.asyncio
async def test_send_message_not_in_channel_from_post_yields_invite_instruction(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": False, "error": "not_in_channel"}
        )
        with pytest.raises(ToolError, match="/invite") as exc_info:
            await _slack_send_message_impl(
                runtime, auth, channel_id="C1", content="hi", attachments=None, file_handles=None
            )
    assert MISSING_ACCESS not in str(exc_info.value), (
        "not_in_channel on send must yield the invite instruction, not MISSING_ACCESS"
    )


@pytest.mark.asyncio
async def test_send_message_msg_too_long_from_post_yields_over_length_guidance(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": False, "error": "msg_too_long"}
        )
        with pytest.raises(ToolError, match="shorten"):
            await _slack_send_message_impl(
                runtime, auth, channel_id="C1", content="hi", attachments=None, file_handles=None
            )


@pytest.mark.asyncio
async def test_send_message_over_12000_chars_refused_with_zero_api_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    content = "x" * 12_001
    with aioresponses() as m:
        with pytest.raises(ToolError, match="shorten"):
            await _slack_send_message_impl(
                runtime,
                auth,
                channel_id="C1",
                content=content,
                attachments=None,
                file_handles=None,
            )
        assert m.requests == {}, "an over-length call must cost zero Slack API calls"


@pytest.mark.asyncio
async def test_send_message_attachments_refused_naming_files_write_no_api_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        with pytest.raises(ToolError, match="files:write"):
            await _slack_send_message_impl(
                runtime,
                auth,
                channel_id="C1",
                content="hi",
                attachments=[{"url": "https://example.com/f.png", "filename": "f.png"}],
                file_handles=None,
            )
        assert m.requests == {}


@pytest.mark.asyncio
async def test_send_message_file_handles_refused_naming_files_write_no_api_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        with pytest.raises(ToolError, match="files:write"):
            await _slack_send_message_impl(
                runtime,
                auth,
                channel_id="C1",
                content="hi",
                attachments=None,
                file_handles=["handle_1"],
            )
        assert m.requests == {}


@pytest.mark.asyncio
async def test_send_message_malformed_composite_target_raises_format_error_no_api_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        with pytest.raises(ToolError, match="channel_id:thread_ts"):
            await _slack_send_message_impl(
                runtime, auth, channel_id="C1:", content="hi", attachments=None, file_handles=None
            )
        assert m.requests == {}
