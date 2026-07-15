"""Tests for tools/slack/_read.py — channel/thread/message read implementations."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from aioresponses import aioresponses
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._read import (  # pyright: ignore[reportPrivateUsage]
    _slack_get_message_impl,
    _slack_list_channels_impl,
    _slack_read_channel_impl,
    _slack_read_thread_impl,
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
    SlackSettings,
)
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.slack_turn_contexts import create_slack_turn_context
from daimon.core.stores.slack_user_tokens import upsert_slack_user_token
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CONVERSATIONS_INFO = re.compile(r"https://slack\.com/api/conversations\.info.*")
_CONVERSATIONS_HISTORY = re.compile(r"https://slack\.com/api/conversations\.history.*")
_CONVERSATIONS_REPLIES = re.compile(r"https://slack\.com/api/conversations\.replies.*")
_CONVERSATIONS_MEMBERS = re.compile(r"https://slack\.com/api/conversations\.members.*")
_USERS_CONVERSATIONS = re.compile(r"https://slack\.com/api/users\.conversations.*")
_USERS_INFO = re.compile(r"https://slack\.com/api/users\.info.*")

_FULL_MEMBER = {"ok": True, "user": {"id": "U_CALLER", "is_restricted": False}}
_GUEST = {"ok": True, "user": {"id": "U_CALLER", "is_restricted": True}}


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


def _build_settings(*, fernet_key: SecretStr, mintable: bool = False) -> Settings:
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
        mcp=McpSettings(public_url=HttpUrl("https://mcp.example.com/mcp"))
        if mintable
        else McpSettings(),
        slack=(
            SlackSettings(signing_secret=SecretStr("shh-secret"), app_token=SecretStr("xapp-test"))
            if mintable
            else None
        ),
    )


async def _make_runtime(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    mintable: bool = False,
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
        settings=_build_settings(fernet_key=fernet_key, mintable=mintable),
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )


async def _seed_user_token(
    runtime: McpRuntime,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    team_id: str = "T_TEST",
    slack_user_id: str = "U_CALLER",
) -> None:
    assert runtime.fernet is not None, "runtime must have a fernet configured to seed a user token"
    async with committing_sessionmaker() as session:
        await upsert_slack_user_token(
            session,
            team_id=team_id,
            slack_user_id=slack_user_id,
            encrypted_token=encrypt_token(runtime.fernet, "xoxp-secret"),
            scopes="channels:history,groups:history,im:history,mpim:history",
        )
        await session.commit()


async def _seed_turn_context(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    auth: AuthIdentity,
    *,
    channel_id: str,
) -> None:
    async with committing_sessionmaker() as session:
        await create_slack_turn_context(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            channel_id=channel_id,
            thread_ts="1.0",
            started_at=datetime.now(tz=UTC),
        )
        await session.commit()


@pytest.mark.asyncio
async def test_read_channel_public_full_member_returns_oldest_first(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C1", "name": "general", "is_private": False}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={
                "ok": True,
                "messages": [
                    {"ts": "3", "user": "U_A", "text": "newest"},
                    {"ts": "2", "user": "U_A", "text": "mid"},
                    {"ts": "1", "user": "U_A", "text": "oldest"},
                ],
            },
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="C1", limit=50)
    assert [r.text for r in rows] == ["oldest", "mid", "newest"], (
        "read_channel must return oldest-first like the Discord tool"
    )
    assert rows[0].username == "alice", "author display names should be resolved"


@pytest.mark.asyncio
async def test_read_channel_private_nonmember_raises_missing_access(
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
            _CONVERSATIONS_MEMBERS,
            payload={"ok": True, "members": ["U_SOMEONE_ELSE"]},
        )
        with pytest.raises(ToolError, match="missing channel access"):
            await _slack_read_channel_impl(runtime, auth, channel_id="C_PRIV", limit=50)


@pytest.mark.asyncio
async def test_read_channel_unknown_channel_same_error_as_denied(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": False, "error": "channel_not_found"},
        )
        with pytest.raises(ToolError, match="missing channel access"):
            await _slack_read_channel_impl(runtime, auth, channel_id="C_NOPE", limit=50)


@pytest.mark.asyncio
async def test_read_channel_user_path_users_info_missing_scope_names_reconnect(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A user token minted before users:read joined SLACK_USER_SCOPES fails the
    users.info display-name lookup with missing_scope. That must tell the
    caller to reconnect via /privacy — a workspace re-install can't refresh
    an already-minted user grant."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C_PRIV", "name": "sekret", "is_private": True}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "hi"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": False, "error": "missing_scope"},
        )
        with pytest.raises(ToolError, match="reconnect via /privacy"):
            await _slack_read_channel_impl(runtime, auth, channel_id="C_PRIV", limit=10)


@pytest.mark.asyncio
async def test_read_channel_missing_scope_names_reinstall(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": False, "error": "missing_scope"},
        )
        with pytest.raises(ToolError, match="re-install daimon"):
            await _slack_read_channel_impl(runtime, auth, channel_id="C1", limit=50)


@pytest.mark.asyncio
async def test_list_channels_omits_private_channels_user_is_not_in(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_CONVERSATIONS,
            payload={
                "ok": True,
                "channels": [
                    {"id": "C_PUB", "name": "general", "is_private": False},
                    {"id": "C_PRIV_IN", "name": "sekret", "is_private": True},
                    {"id": "C_PRIV_OUT", "name": "hidden", "is_private": True},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_CONVERSATIONS,
            payload={
                "ok": True,
                "channels": [{"id": "C_PRIV_IN", "name": "sekret", "is_private": True}],
                "response_metadata": {"next_cursor": ""},
            },
        )
        rows = await _slack_list_channels_impl(runtime, auth)
    assert {r.id for r in rows} == {"C_PUB", "C_PRIV_IN"}, (
        "private channels the caller is not in must be silently omitted"
    )


@pytest.mark.asyncio
async def test_list_channels_guest_sees_only_membership_channels(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_CONVERSATIONS,
            payload={
                "ok": True,
                "channels": [
                    {"id": "C_PUB", "name": "general", "is_private": False},
                    {"id": "C_PRIV_IN", "name": "sekret", "is_private": True},
                    {"id": "C_PRIV_OUT", "name": "hidden", "is_private": True},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
        m.get(_USERS_INFO, payload=_GUEST)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_CONVERSATIONS,
            payload={
                "ok": True,
                "channels": [{"id": "C_PUB", "name": "general", "is_private": False}],
                "response_metadata": {"next_cursor": ""},
            },
        )
        rows = await _slack_list_channels_impl(runtime, auth)
    assert {r.id for r in rows} == {"C_PUB"}, "guests see only channels they are members of"


@pytest.mark.asyncio
async def test_read_thread_composite_id_returns_thread(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C1", "name": "general", "is_private": False}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={
                "ok": True,
                "has_more": False,
                "messages": [
                    {
                        "ts": "1.0",
                        "user": "U_A",
                        "text": "root",
                        "thread_ts": "1.0",
                        "reply_count": 1,
                    },
                    {"ts": "2.0", "user": "U_B", "text": "reply", "thread_ts": "1.0"},
                ],
            },
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_B", "profile": {"display_name": "bob"}}},
        )
        result = await _slack_read_thread_impl(runtime, auth, thread_id="C1:1.0", limit=50)
    assert result.thread_ts == "1.0" and [m_.text for m_ in result.messages] == ["root", "reply"]


@pytest.mark.asyncio
async def test_read_thread_malformed_id_explains_format(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with pytest.raises(ToolError, match="channel_id:thread_ts"):
        await _slack_read_thread_impl(runtime, auth, thread_id="justatimestamp", limit=50)


@pytest.mark.asyncio
async def test_get_message_top_level_found_via_history(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C1", "name": "general", "is_private": False}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "5.0", "user": "U_A", "text": "hit"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        row = await _slack_get_message_impl(runtime, auth, channel_id="C1", message_id="5.0")
    assert row.text == "hit"


@pytest.mark.asyncio
async def test_get_message_thread_reply_found_via_replies_fallback(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C1", "name": "general", "is_private": False}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "6.0", "user": "U_A", "text": "parent"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={
                "ok": True,
                "messages": [
                    {"ts": "6.0", "user": "U_A", "text": "parent"},
                    {"ts": "6.5", "user": "U_A", "text": "reply-hit"},
                ],
            },
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        row = await _slack_get_message_impl(runtime, auth, channel_id="C1", message_id="6.5")
    assert row.text == "reply-hit"


@pytest.mark.asyncio
async def test_get_message_nonexistent_ts_raises_message_not_found(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A bogus message_id with no history match and no thread to reply into
    surfaces Slack's thread_not_found from conversations.replies — that must
    map to a clean ToolError("message not found"), not an opaque SlackApiError."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C1", "name": "general", "is_private": False}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": []},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={"ok": False, "error": "thread_not_found"},
            status=200,
        )
        with pytest.raises(ToolError, match="message not found"):
            await _slack_get_message_impl(runtime, auth, channel_id="C1", message_id="9999.9999")


@pytest.mark.asyncio
async def test_read_channel_user_path_revoked_token_raises_reconnect_hint(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A revoked/expired user token must not dead-end in a raw SlackApiError —
    conversations.history answering token_revoked on the user-token path must
    map to an actionable ToolError telling the caller to reconnect."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="C_PUB")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={
                "ok": True,
                "channel": {"id": "C_PUB", "name": "general", "is_private": False},
            },
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": False, "error": "token_revoked"},
        )
        with pytest.raises(ToolError, match="reconnect"):
            await _slack_read_channel_impl(runtime, auth, channel_id="C_PUB", limit=10)


@pytest.mark.asyncio
async def test_read_channel_unmapped_slack_error_propagates(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An unmapped SlackApiError (e.g. ratelimited) from conversations.info must
    propagate raw to the dispatcher boundary — map_slack_api_error only collapses
    known channel-access errors, and get_message's local not-found mapping must
    not widen that."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": False, "error": "ratelimited"},
            status=200,
        )
        with pytest.raises(SlackApiError):
            await _slack_read_channel_impl(runtime, auth, channel_id="C1", limit=50)


# --- Hybrid xoxp/bot path matrix (Task 8) ------------------------------------


@pytest.mark.asyncio
async def test_read_channel_user_path_private_same_channel_destination_returns_messages(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 1: private source == destination — no visibility calls hit the fake."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="C_PRIV")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C_PRIV", "name": "sekret", "is_private": True}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "hi"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        # No conversations.members mock registered: the user path never scans
        # membership, so an unregistered members URL would fail the test.
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="C_PRIV", limit=10)
    assert [r.text for r in rows] == ["hi"], (
        "user-token path should return the private channel's messages"
    )


@pytest.mark.asyncio
async def test_read_channel_user_path_private_dm_destination_returns_messages(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 2: private source, destination is a DM — allowed regardless of channel match."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="D_WITH_BOT")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C_PRIV", "name": "sekret", "is_private": True}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "hi"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="C_PRIV", limit=10)
    assert [r.text for r in rows] == ["hi"], "DM destination should always be an allowed audience"


@pytest.mark.asyncio
async def test_read_channel_user_path_private_cross_channel_destination_returns_messages(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Private source, destination a different (non-DM) channel — now allowed (parity)."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="C_DEST")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C_PRIV", "name": "sekret", "is_private": True}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "hi"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="C_PRIV", limit=10)
    assert [r.text for r in rows] == ["hi"], (
        "a private channel the user can see is answerable from any destination"
    )


@pytest.mark.asyncio
async def test_read_channel_user_path_dm_source_channel_destination_denied(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """DM source, destination a channel — refused (DM content is DM-only)."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="C_DEST")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "D_SRC", "name": None, "is_im": True}},
        )
        with pytest.raises(ToolError, match="only shareable in a DM"):
            await _slack_read_channel_impl(runtime, auth, channel_id="D_SRC", limit=10)


@pytest.mark.asyncio
async def test_read_channel_user_path_mpim_source_channel_destination_returns_messages(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Group-DM source, destination a channel — allowed (mpim follows user visibility)."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="C_DEST")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "G_MPIM", "name": None, "is_mpim": True}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "group dm"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="G_MPIM", limit=10)
    assert [r.text for r in rows] == ["group dm"], (
        "a group DM the user belongs to is answerable from any destination"
    )


@pytest.mark.asyncio
async def test_read_channel_user_path_dm_source_dm_destination_returns_messages(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """DM source, destination is a DM — allowed."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="D_WITH_BOT")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "D_SRC", "name": None, "is_im": True}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "hi"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="D_SRC", limit=10)
    assert [r.text for r in rows] == ["hi"], "a DM read in a DM destination is allowed"


@pytest.mark.asyncio
async def test_read_channel_user_path_public_channel_any_destination_returns_messages(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 4: public source — leak gate never consults the destination store."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    # Deliberately no turn context seeded: a public source must not require one.
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={
                "ok": True,
                "channel": {"id": "C_PUB", "name": "general", "is_private": False},
            },
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "hi"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="C_PUB", limit=10)
    assert [r.text for r in rows] == ["hi"], "public channels bypass the leak gate entirely"


@pytest.mark.asyncio
async def test_read_channel_user_path_public_not_in_channel_falls_back_to_bot_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 5: user token answers not_in_channel on a public channel — silent bot-path retry."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={
                "ok": True,
                "channel": {"id": "C_PUB", "name": "general", "is_private": False},
            },
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": False, "error": "not_in_channel"},
        )
        # Bot-path retry: conversations.info again, then check_channel_access
        # (users.info guest check, public channel needs no membership scan),
        # then a history call that this time succeeds on the bot token.
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={
                "ok": True,
                "channel": {"id": "C_PUB", "name": "general", "is_private": False},
            },
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "bot-token hit"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="C_PUB", limit=10)
    assert [r.text for r in rows] == ["bot-token hit"], (
        "not_in_channel on the user token must retry silently on the bot token"
    )


@pytest.mark.asyncio
async def test_read_channel_user_path_private_not_in_channel_fallback_still_denied_by_bot_path(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """not_in_channel fallback is unscoped by channel type, but the bot path still
    independently gates access — so falling back on a *private* channel cannot
    widen the audience beyond what check_channel_access would allow anyway."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="D_DEST")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={
                "ok": True,
                "channel": {"id": "C_PRIV_2", "name": "sekret", "is_private": True},
            },
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": False, "error": "not_in_channel"},
        )
        # Bot-path retry: conversations.info again, then check_channel_access —
        # the caller is absent from the private channel's membership, so the
        # bot path denies it even though the leak gate on the user path would
        # have allowed the destination.
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={
                "ok": True,
                "channel": {"id": "C_PRIV_2", "name": "sekret", "is_private": True},
            },
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_MEMBERS,
            payload={"ok": True, "members": ["U_SOMEONE_ELSE"]},
        )
        with pytest.raises(ToolError, match="missing channel access"):
            await _slack_read_channel_impl(runtime, auth, channel_id="C_PRIV_2", limit=10)


@pytest.mark.asyncio
async def test_read_channel_user_path_im_source_dm_destination_returns_messages(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 6: an im the user is a member of — bot path would reject this outright."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="D_DEST")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "D_IM_SOURCE", "is_im": True}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY,
            payload={"ok": True, "messages": [{"ts": "1", "user": "U_A", "text": "dm content"}]},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO,
            payload={"ok": True, "user": {"id": "U_A", "profile": {"display_name": "alice"}}},
        )
        rows = await _slack_read_channel_impl(runtime, auth, channel_id="D_IM_SOURCE", limit=10)
    assert [r.text for r in rows] == ["dm content"], (
        "user-token path should serve an im the bot path would reject"
    )


@pytest.mark.asyncio
async def test_read_channel_bot_path_denial_appends_connect_hint_when_mintable_and_no_user_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 7: bot-path MISSING_ACCESS + mintable hint + no user token → hint appended."""
    runtime = await _make_runtime(committing_sessionmaker, mintable=True)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C_PRIV", "name": "sekret", "is_private": True}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_MEMBERS,
            payload={"ok": True, "members": ["U_SOMEONE_ELSE"]},
        )
        with pytest.raises(ToolError) as exc_info:
            await _slack_read_channel_impl(runtime, auth, channel_id="C_PRIV", limit=10)
    message = str(exc_info.value)
    assert MISSING_ACCESS in message, "denial must still carry the plain MISSING_ACCESS text"
    assert "/oauth/slack/connect?state=" in message, "mintable + no user token must append the hint"


@pytest.mark.asyncio
async def test_read_channel_bot_path_denial_omits_hint_when_unconfigured(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 8: bot-path MISSING_ACCESS, workspace/app unconfigured for hints → plain message."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": "C_PRIV", "name": "sekret", "is_private": True}},
        )
        m.get(_USERS_INFO, payload=_FULL_MEMBER)  # pyright: ignore[reportUnknownMemberType]
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_MEMBERS,
            payload={"ok": True, "members": ["U_SOMEONE_ELSE"]},
        )
        with pytest.raises(ToolError) as exc_info:
            await _slack_read_channel_impl(runtime, auth, channel_id="C_PRIV", limit=10)
    assert str(exc_info.value) == MISSING_ACCESS, (
        "no connect hint should be appended when unconfigured"
    )


@pytest.mark.asyncio
async def test_list_channels_user_path_non_dm_destination_hides_only_im(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 9: non-DM (here ambiguous) destination lists channels, hides only 1:1 DM entries."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="C_OTHER_1")
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="C_OTHER_2")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_CONVERSATIONS,
            payload={
                "ok": True,
                "channels": [
                    {"id": "C_PUB", "name": "general", "is_private": False},
                    {"id": "C_PRIV", "name": "sekret", "is_private": True},
                    {"id": "D_IM", "name": "", "is_im": True},
                    {"id": "G_MPIM", "name": "mpdm-alice--bob--carol-1", "is_mpim": True},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
        rows = await _slack_list_channels_impl(runtime, auth)
    assert {r.id for r in rows} == {"C_PUB", "C_PRIV", "G_MPIM"}, (
        "channels and group DMs the user belongs to are listed from any "
        "destination; only 1:1 DM entries are hidden outside a DM"
    )


@pytest.mark.asyncio
async def test_list_channels_user_path_dm_destination_shows_public_private_and_im(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Row 10: DM destination — public, private, im, and mpim of the user all listed."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="D_DEST")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_CONVERSATIONS,
            payload={
                "ok": True,
                "channels": [
                    {"id": "C_PUB", "name": "general", "is_private": False},
                    {"id": "C_PRIV", "name": "sekret", "is_private": True},
                    {"id": "D_IM", "name": "", "is_im": True},
                    {"id": "G_MPIM", "name": "mpdm-alice--bob--carol-1", "is_mpim": True},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
        rows = await _slack_list_channels_impl(runtime, auth)
    assert {r.id for r in rows} == {"C_PUB", "C_PRIV", "D_IM", "G_MPIM"}, (
        "DM destination should permit listing all private-ish entries too"
    )
