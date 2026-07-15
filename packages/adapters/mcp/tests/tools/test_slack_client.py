"""Tests for tools/slack/_client.py — identity guards and bot-token resolution."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import (  # pyright: ignore[reportPrivateUsage]
    _require_slack_identity,
    _require_team_id,
    build_connect_hint,
    slack_read_client,
    slack_web_client,
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
from daimon.core.stores.slack_user_tokens import upsert_slack_user_token
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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


def test_require_slack_identity_returns_platform_user_id() -> None:
    assert _require_slack_identity(_auth()) == "U_CALLER", (
        "guard should return the caller's slack user id"
    )


def test_require_slack_identity_raises_when_missing() -> None:
    with pytest.raises(ToolError, match="slack-bound identity"):
        _require_slack_identity(_auth(platform_user_id=None))


def test_require_team_id_returns_external_id() -> None:
    assert _require_team_id(_auth()) == "T_TEST", "guard should return the workspace team id"


def test_require_team_id_raises_when_missing() -> None:
    with pytest.raises(ToolError, match="workspace context"):
        _require_team_id(_auth(external_id=None))


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
    )


@pytest.mark.asyncio
async def test_slack_web_client_decrypts_stored_bot_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    fernet = build_multifernet((fernet_key.get_secret_value(),))
    async with committing_sessionmaker() as session:
        await upsert_slack_bot_token(
            session, team_id="T_TEST", encrypted_token=encrypt_token(fernet, "xoxb-secret")
        )
        await session.commit()

    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=_build_settings(fernet_key=fernet_key),
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )

    client = await slack_web_client(runtime, team_id="T_TEST")
    assert client.token == "xoxb-secret", "client must carry the decrypted workspace bot token"


@pytest.mark.asyncio
async def test_slack_web_client_raises_when_no_installation(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    fernet = build_multifernet((fernet_key.get_secret_value(),))
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=_build_settings(fernet_key=fernet_key),
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )

    with pytest.raises(ToolError, match="no slack installation"):
        await slack_web_client(runtime, team_id="T_ABSENT")


@pytest.mark.asyncio
async def test_slack_read_client_prefers_user_token_when_row_exists(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    fernet = build_multifernet((fernet_key.get_secret_value(),))
    async with committing_sessionmaker() as session:
        await upsert_slack_user_token(
            session,
            team_id="T1",
            slack_user_id="U1",
            encrypted_token=encrypt_token(fernet, "xoxp-user"),
            scopes="channels:history",
        )
        await upsert_slack_bot_token(
            session, team_id="T1", encrypted_token=encrypt_token(fernet, "xoxb-bot")
        )
        await session.commit()

    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=_build_settings(fernet_key=fernet_key),
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )

    rc = await slack_read_client(runtime, team_id="T1", slack_user_id="U1")
    assert rc.runs_as_user is True, "a stored user token must win"
    assert rc.client.token == "xoxp-user", "client must carry the decrypted xoxp token"


@pytest.mark.asyncio
async def test_slack_read_client_falls_back_to_bot_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    fernet = build_multifernet((fernet_key.get_secret_value(),))
    async with committing_sessionmaker() as session:
        await upsert_slack_bot_token(
            session, team_id="T1", encrypted_token=encrypt_token(fernet, "xoxb-bot")
        )
        await session.commit()

    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=_build_settings(fernet_key=fernet_key),
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )

    rc = await slack_read_client(runtime, team_id="T1", slack_user_id="U1")
    assert rc.runs_as_user is False, "no user token → bot path"
    assert rc.client.token == "xoxb-bot", "client must carry the decrypted bot token"


@pytest.mark.asyncio
async def test_slack_read_client_corrupt_user_token_raises_tool_error(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    fernet = build_multifernet((fernet_key.get_secret_value(),))
    other_key = Fernet.generate_key()
    other_fernet = build_multifernet((other_key.decode("ascii"),))
    async with committing_sessionmaker() as session:
        await upsert_slack_user_token(
            session,
            team_id="T1",
            slack_user_id="U1",
            encrypted_token=encrypt_token(other_fernet, "xoxp-user"),
            scopes="channels:history",
        )
        await session.commit()

    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=_build_settings(fernet_key=fernet_key),
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )

    with pytest.raises(ToolError, match="reconnect"):
        await slack_read_client(runtime, team_id="T1", slack_user_id="U1")


def test_build_connect_hint_none_when_unconfigured() -> None:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    runtime = McpRuntime(
        session_factory=MagicMock(spec=async_sessionmaker),
        client=MagicMock(spec=AsyncAnthropic),
        settings=_build_settings(fernet_key=fernet_key),
        deployment_default=DeploymentDefault(),
        fernet=build_multifernet((fernet_key.get_secret_value(),)),
    )

    assert build_connect_hint(runtime, team_id="T1", slack_user_id="U1", now=0.0) is None, (
        "no connect URL can be minted without slack settings + app_root_url"
    )


def test_build_connect_hint_contains_signed_connect_url() -> None:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
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
        mcp=McpSettings(public_url=HttpUrl("https://mcp.example.com/mcp")),
        slack=SlackSettings(
            signing_secret=SecretStr("shh-secret"),
            app_token=SecretStr("xapp-test"),
        ),
    )
    runtime = McpRuntime(
        session_factory=MagicMock(spec=async_sessionmaker),
        client=MagicMock(spec=AsyncAnthropic),
        settings=settings,
        deployment_default=DeploymentDefault(),
        fernet=build_multifernet((fernet_key.get_secret_value(),)),
    )

    hint = build_connect_hint(runtime, team_id="T1", slack_user_id="U1", now=1000.0)
    assert hint is not None and "/oauth/slack/connect?state=" in hint, (
        "hint must carry the per-user signed connect URL"
    )
    assert "admin" in hint, "hint must warn about admin-approval workspaces"
