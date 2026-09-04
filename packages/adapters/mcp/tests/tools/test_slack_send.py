"""Tests for tools/slack/_send.py — send_message implementation."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from aioresponses import aioresponses
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._send import (  # pyright: ignore[reportPrivateUsage]
    _slack_create_thread_impl,
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
from daimon.core.stores.file_uploads import create_upload, store_upload_content
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
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
_GET_UPLOAD_URL = re.compile(r"https://slack\.com/api/files\.getUploadURLExternal.*")
_UPLOAD = re.compile(r"https://files\.slack\.com/upload/v1/.*")
_COMPLETE_UPLOAD = re.compile(r"https://slack\.com/api/files\.completeUploadExternal.*")

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


def _recorded(m: aioresponses, path_fragment: str) -> list[tuple[URL, dict[str, Any]]]:
    """Every recorded (url, kwargs) whose url contains the fragment, in call order."""
    return [
        (url, call.kwargs)  # pyright: ignore[reportUnknownMemberType]
        for (_, url), calls in m.requests.items()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if path_fragment in str(url)
        for call in calls  # pyright: ignore[reportUnknownVariableType]
    ]


def _mock_upload_flow(m: aioresponses) -> None:
    m.post(  # pyright: ignore[reportUnknownMemberType]
        _GET_UPLOAD_URL,
        payload={
            "ok": True,
            "file_id": "F1",
            "upload_url": "https://files.slack.com/upload/v1/ABC",
        },
    )
    m.post(_UPLOAD, status=200, body="OK", content_type="text/plain")  # pyright: ignore[reportUnknownMemberType]
    m.post(  # pyright: ignore[reportUnknownMemberType]
        _COMPLETE_UPLOAD, payload={"ok": True, "files": [{"id": "F1", "title": "chart.png"}]}
    )


async def _stage_uploads(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    payloads: list[bytes],
) -> list[str]:
    """Mint and fill upload rows for the caller's tenant, creating the tenant."""
    now = datetime.now(UTC)
    handles: list[str] = []
    async with committing_sessionmaker() as session:
        await make_tenant(session, platform="slack", workspace_id="T_TEST", id=tenant_id)
        for payload in payloads:
            row, token = await create_upload(
                session,
                tenant_id=tenant_id,
                title="chart",
                display_filename="chart.png",
                content_type="image/png",
                now=now,
            )
            await store_upload_content(session, upload_token=token, data=payload, now=now)
            handles.append(row.id)
        await session.commit()
    return handles


async def _stage_upload(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    payload: bytes,
) -> str:
    (handle,) = await _stage_uploads(
        committing_sessionmaker, tenant_id=tenant_id, payloads=[payload]
    )
    return handle


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
async def test_send_message_attachments_by_url_refused_pointing_at_file_handles_no_api_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        with pytest.raises(ToolError, match="file_handles"):
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
async def test_send_message_file_handles_with_empty_content_refused_no_api_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        with pytest.raises(ToolError, match="caption"):
            await _slack_send_message_impl(
                runtime,
                auth,
                channel_id="C1",
                content="   ",
                attachments=None,
                file_handles=["handle_1"],
            )
        assert m.requests == {}


@pytest.mark.asyncio
async def test_send_message_unknown_file_handle_refused_naming_it_no_api_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        with pytest.raises(ToolError, match="nope.png"):
            await _slack_send_message_impl(
                runtime,
                auth,
                channel_id="C1",
                content="here",
                attachments=None,
                file_handles=["nope.png"],
            )
        assert m.requests == {}, "a bad handle must be caught before the text is posted"


@pytest.mark.asyncio
async def test_send_message_file_handles_post_text_then_upload_threaded_under_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    payload = bytes(range(256)) * 4
    handle = await _stage_upload(committing_sessionmaker, tenant_id=auth.tenant_id, payload=payload)
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000001.000100"}
        )
        _mock_upload_flow(m)
        row = await _slack_send_message_impl(
            runtime,
            auth,
            channel_id="C1",
            content="the chart",
            attachments=None,
            file_handles=[handle],
        )
        get_url = _recorded(m, "getUploadURLExternal")
        upload = _recorded(m, "files.slack.com")
        complete = _recorded(m, "completeUploadExternal")

    assert row.ts == "1700000001.000100", "the returned row is the text message"
    assert len(get_url) == 1 and len(upload) == 1 and len(complete) == 1, (
        "one file must run the 3-request upload flow exactly once"
    )
    assert get_url[0][1]["params"]["filename"] == "chart.png", (
        "the upload row's display filename must name the Slack file"
    )
    assert upload[0][1]["data"] == payload, "uploaded bytes must reach Slack byte-identical"
    complete_params = complete[0][1]["params"]
    assert complete_params["channel_id"] == "C1"
    assert complete_params["thread_ts"] == "1700000001.000100", (
        "a channel-root post threads its files under the text it just posted"
    )
    assert "content_type" not in get_url[0][1]["params"]


@pytest.mark.asyncio
async def test_send_message_file_handles_into_thread_target_upload_uses_that_thread(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    handle = await _stage_upload(committing_sessionmaker, tenant_id=auth.tenant_id, payload=b"x")
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={"ok": True, "messages": [{"ts": "1700000000.000001", "text": "root"}]},
        )
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE,
            payload={
                "ok": True,
                "ts": "1700000002.000100",
                "message": {"thread_ts": "1700000000.000001"},
            },
        )
        _mock_upload_flow(m)
        row = await _slack_send_message_impl(
            runtime,
            auth,
            channel_id="C1:1700000000.000001",
            content="the chart",
            attachments=None,
            file_handles=[handle],
        )
        complete_params = _recorded(m, "completeUploadExternal")[0][1]["params"]

    assert row.thread_ts == "1700000000.000001"
    assert complete_params["thread_ts"] == "1700000000.000001", (
        "files aimed at a thread land in that thread, not under the new reply"
    )


@pytest.mark.asyncio
async def test_send_message_two_file_handles_share_one_completion_call(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    first, second = await _stage_uploads(
        committing_sessionmaker, tenant_id=auth.tenant_id, payloads=[b"a", b"b"]
    )
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000001.000100"}
        )
        for file_id in ("F1", "F2"):
            m.post(  # pyright: ignore[reportUnknownMemberType]
                _GET_UPLOAD_URL,
                payload={
                    "ok": True,
                    "file_id": file_id,
                    "upload_url": f"https://files.slack.com/upload/v1/{file_id}",
                },
            )
            m.post(_UPLOAD, status=200, body="OK", content_type="text/plain")  # pyright: ignore[reportUnknownMemberType]
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _COMPLETE_UPLOAD,
            payload={"ok": True, "files": [{"id": "F1"}, {"id": "F2"}]},
        )
        await _slack_send_message_impl(
            runtime,
            auth,
            channel_id="C1",
            content="two charts",
            attachments=None,
            file_handles=[first, second],
        )
        complete = _recorded(m, "completeUploadExternal")

    assert len(complete) == 1, "several files complete in one call, so they share one message"
    completed_ids = [f["id"] for f in json.loads(complete[0][1]["params"]["files"])]
    assert completed_ids == ["F1", "F2"]


@pytest.mark.asyncio
async def test_send_message_missing_files_scope_names_reinstall_and_the_already_posted_text(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    handle = await _stage_upload(committing_sessionmaker, tenant_id=auth.tenant_id, payload=b"x")
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000001.000100"}
        )
        m.post(_GET_UPLOAD_URL, payload={"ok": False, "error": "missing_scope"})  # pyright: ignore[reportUnknownMemberType]
        with pytest.raises(ToolError) as excinfo:
            await _slack_send_message_impl(
                runtime,
                auth,
                channel_id="C1",
                content="the chart",
                attachments=None,
                file_handles=[handle],
            )
        assert len(m.requests[_POST_KEY]) == 1, "the text goes out before the upload is tried"

    message = str(excinfo.value)
    assert "files:write" in message and "reinstall" in message, (
        "a scope-less install must be told what to fix, not shown a raw error code"
    )
    assert "already posted" in message, "the agent must learn the text landed, or it re-sends it"


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


# ---------------------------------------------------------------------------
# _slack_create_thread_impl
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_thread_happy_path_posts_root_message_no_thread_ts(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": True, "ts": "1700000010.000100"}
        )
        row = await _slack_create_thread_impl(
            runtime, auth, channel_id="C1", content="new thread root"
        )
        body = _post_body(m)

    assert row.ts == "1700000010.000100", "returned row must carry the posted ts"
    assert row.text == "new thread root", "returned row's text must be the content as passed"
    assert "thread_ts" not in body, "a thread-root post must not carry a thread_ts key"


@pytest.mark.asyncio
async def test_create_thread_composite_target_refused_before_any_api_call(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        with pytest.raises(ToolError, match="thread root"):
            await _slack_create_thread_impl(
                runtime,
                auth,
                channel_id="C1:1717171717.123456",
                content="a root",
            )
        assert m.requests == {}, "a composite target must cost zero Slack API calls"


@pytest.mark.asyncio
async def test_create_thread_over_12000_chars_refused_with_zero_api_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    content = "x" * 12_001
    with aioresponses() as m:
        with pytest.raises(ToolError, match="shorten"):
            await _slack_create_thread_impl(runtime, auth, channel_id="C1", content=content)
        assert m.requests == {}, "an over-length call must cost zero Slack API calls"


@pytest.mark.asyncio
async def test_create_thread_not_in_channel_from_post_yields_invite_instruction(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        _mock_public_channel_access(m)
        m.post(  # pyright: ignore[reportUnknownMemberType]
            _CHAT_POST_MESSAGE, payload={"ok": False, "error": "not_in_channel"}
        )
        with pytest.raises(ToolError, match="/invite"):
            await _slack_create_thread_impl(runtime, auth, channel_id="C1", content="hi")


@pytest.mark.asyncio
async def test_create_thread_channel_access_validated_before_posting(
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
            await _slack_create_thread_impl(runtime, auth, channel_id="C_PRIV", content="hi")
        assert _POST_KEY not in m.requests, (
            "the deny path must raise before ever reaching chat.postMessage"
        )
