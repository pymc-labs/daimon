"""Tests for tools/slack/_search.py — search.messages, user-token-only."""

from __future__ import annotations

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
from daimon.adapters.mcp.tools.channels import register_channel_tools
from daimon.adapters.mcp.tools.slack._search import (  # pyright: ignore[reportPrivateUsage]
    _slack_search_messages_impl,
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
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SEARCH_MESSAGES = re.compile(r"https://slack\.com/api/search\.messages.*")


class _SeedAuthMiddleware(Middleware):
    """Inject an AuthIdentity into request state so tool closures that read
    ``ctx.get_state("auth")`` resolve without the full identity middleware.

    Mirrors ``test_skills.py``'s helper of the same name — duplicated locally
    per the testing guideline (inline data/helpers, no cross-test-file
    sharing of private setup).
    """

    def __init__(self, auth: AuthIdentity) -> None:
        self._auth = auth

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Any,
    ) -> Any:
        await context.fastmcp_context.set_state("auth", self._auth, serializable=False)
        return await call_next(context)


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
            scopes="search:read",
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
async def test_search_requires_user_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No slack_user_tokens row → bot-token-only caller gets the connect-hint error."""
    runtime = await _make_runtime(committing_sessionmaker, mintable=True)
    auth = _auth()
    with pytest.raises(ToolError, match="connect"):
        await _slack_search_messages_impl(runtime, auth, content="q", limit=10)


@pytest.mark.asyncio
async def test_search_non_dm_destination_filters_dm_hits(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Non-DM destination: channel and group-DM hits returned, 1:1 DM hits dropped."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="C1")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _SEARCH_MESSAGES,
            payload={
                "ok": True,
                "messages": {
                    "paging": {"count": 20, "total": 3, "page": 1, "pages": 1},
                    "matches": [
                        {
                            "channel": {"id": "C1", "name": "general", "is_private": False},
                            "ts": "1.0",
                            "username": "alice",
                            "text": "channel hit",
                            "permalink": "https://slack.example.com/archives/C1/p1",
                        },
                        {
                            "channel": {"id": "D1", "name": None, "is_im": True},
                            "ts": "2.0",
                            "username": "bob",
                            "text": "dm hit",
                            "permalink": "https://slack.example.com/archives/D1/p2",
                        },
                        {
                            "channel": {"id": "G1", "name": None, "is_mpim": True},
                            "ts": "3.0",
                            "username": "carol",
                            "text": "group dm hit",
                            "permalink": "https://slack.example.com/archives/G1/p3",
                        },
                    ],
                },
            },
        )
        result = await _slack_search_messages_impl(runtime, auth, content="q", limit=10)
    assert [x.text for x in result.matches] == ["channel hit", "group dm hit"], (
        "a non-DM destination must drop 1:1 DM hits but keep channel and group-DM hits"
    )


@pytest.mark.asyncio
async def test_search_dm_destination_keeps_dm_hits(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """DM destination: all hits returned, including DM-sourced ones."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="D9")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _SEARCH_MESSAGES,
            payload={
                "ok": True,
                "messages": {
                    "paging": {"count": 20, "total": 2, "page": 1, "pages": 1},
                    "matches": [
                        {
                            "channel": {"id": "C1", "name": "general", "is_private": False},
                            "ts": "1.0",
                            "username": "alice",
                            "text": "channel hit",
                            "permalink": "https://slack.example.com/archives/C1/p1",
                        },
                        {
                            "channel": {"id": "D1", "name": None, "is_im": True},
                            "ts": "2.0",
                            "username": "bob",
                            "text": "dm hit",
                            "permalink": "https://slack.example.com/archives/D1/p2",
                        },
                    ],
                },
            },
        )
        result = await _slack_search_messages_impl(runtime, auth, content="q", limit=10)
    assert [x.text for x in result.matches] == ["channel hit", "dm hit"], (
        "a DM destination surfaces every hit the user's own search can see"
    )


@pytest.mark.asyncio
async def test_dispatch_slack_search_rejects_discord_only_filters(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Via the registered tool: content + a Discord-only filter → rejected."""
    runtime = await _make_runtime(committing_sessionmaker)
    auth = _auth()
    await _seed_user_token(runtime, committing_sessionmaker)
    await _seed_turn_context(committing_sessionmaker, auth, channel_id="D9")
    mcp = FastMCP(name="test")
    mcp.add_middleware(_SeedAuthMiddleware(auth))
    register_channel_tools(mcp, runtime)

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="slack"):
            await client.call_tool("search_messages", {"content": "q", "author_ids": ["U1"]})
