"""Behavioral tests for the ``get_cli_token`` MCP tool.

Started life as Wave 0 RED stubs (ImportError-only) and turned green by
when ``daimon.adapters.mcp.tools.cli_token`` lands.
Plan 04 task 4.3 promotes this file to ``test_cli_token.py``.

Per testing skill: real DB sessions, no ``model_construct``, no ``AsyncMock``
on broker/tool methods. The tool is exercised through the FastMCP in-process
``Client`` so the JWT-derived ``AuthIdentity`` flows through the real
middleware.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.cli_token import register_cli_token_tool
from daimon.core.config import (
    AnthropicSettings,
    CredentialsSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubSettings,
    Settings,
)
from daimon.core.scope import DeploymentDefault
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _fixture_is_admin_resolver_false(_ctx: object) -> str | None:
    return None


def _build_settings(
    *,
    fernet_key: SecretStr,
    github_fallback_pat: SecretStr | None = None,
) -> Settings:
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
        github=GithubSettings(fallback_pat=github_fallback_pat),
    )


@pytest_asyncio.fixture
async def cli_token_app(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[FastMCP, uuid.UUID, str, async_sessionmaker[AsyncSession], Settings]]:
    """Build a FastMCP app with cli_token registered + IdentityMiddleware seeded.

    Yields (mcp, account_id, plaintext_token, sessionmaker, settings) for tests
    that want to assert on log lines or DB state alongside the tool call.
    """
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    settings = _build_settings(fernet_key=fernet_key)
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    runtime = McpRuntime(
        session_factory=db_session_factory,
        client=MagicMock(spec=AsyncAnthropic),
        settings=settings,
        deployment_default=DeploymentDefault(),
    )

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_is_admin_resolver_false,
            sessionmaker=db_session_factory,
        )
    )
    register_cli_token_tool(mcp, runtime)
    yield mcp, account_id, "ghp_supersecret", db_session_factory, settings


async def test_get_cli_token_unknown_service_rejected_by_literal(
    cli_token_app: tuple[FastMCP, uuid.UUID, str, async_sessionmaker[AsyncSession], Settings],
) -> None:
    """Unknown service strings rejected by FastMCP's Literal schema validation
    before the tool body runs."""
    mcp, _account_id, _plaintext_token, _sessionmaker, _settings = cli_token_app

    async with Client(mcp) as client:
        with pytest.raises((McpError, ToolError), match="gcloud|literal"):
            await client.call_tool("get_cli_token", {"service": "not-a-real-service"})


async def test_get_cli_token_gcloud_with_no_agent_id_in_jwt_raises_no_binding(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """JWT without agent_id + gcloud service → NoBindingError → ToolError.

    Defense-in-depth: broker also enforces this per plan 03 Pitfall 7.
    """
    import json

    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    # Agent identity is checked before credential selection. This deliberately
    # incomplete service-account JSON must never be parsed without an agent_id.
    sa_json = json.dumps({"type": "service_account", "client_email": "x@x"})
    settings = Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://daimon:daimon@localhost:5432/daimon"),
        ),
        anthropic=AnthropicSettings(
            api_key=SecretStr("sk-test"),
            base_url=HttpUrl("https://api.anthropic.com"),
        ),
        crypto=CryptoSettings(keys=(fernet_key,)),
        credentials=CredentialsSettings(google_sa_json=SecretStr(sa_json)),
    )
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    runtime = McpRuntime(
        session_factory=db_session_factory,
        client=MagicMock(spec=AsyncAnthropic),
        settings=settings,
        deployment_default=DeploymentDefault(),
    )

    async def fixture_subject_resolver(_ctx: object) -> str:
        return str(account_id)

    async def fixture_tenant_resolver(_ctx: object) -> str:
        return str(tenant_id)

    async def fixture_role_resolver(_ctx: object) -> str:
        return "user"

    async def fixture_agent_id_resolver(_ctx: object) -> str | None:
        return None  # JWT has no agent_id claim

    mcp = FastMCP(name="test")
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=fixture_subject_resolver,
            tenant_resolver=fixture_tenant_resolver,
            role_resolver=fixture_role_resolver,
            agent_id_resolver=fixture_agent_id_resolver,
            is_admin_resolver=_fixture_is_admin_resolver_false,
            internal_resolver=_fixture_is_admin_resolver_false,
            sessionmaker=db_session_factory,
        )
    )
    register_cli_token_tool(mcp, runtime)

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="agent_id"):
            await client.call_tool("get_cli_token", {"service": "gcloud"})
