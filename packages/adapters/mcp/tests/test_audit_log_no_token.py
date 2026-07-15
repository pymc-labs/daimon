"""Combined audit-log integration test — Phase 19 plan-checker W10.

Asserts the COMBINED log output across ``daimon.core.broker`` (the
dispatch + provider layer) and ``daimon.adapters.mcp.tools.cli_token``
(the tool boundary) does not contain the token plaintext under any
log level (T-19-04-02).

Real DB row, real broker, real tool, real loggers. Mocking would defeat
the purpose: the invariant we care about is exactly that nothing in the
real pipeline accidentally logs the token.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import MagicMock

import pytest
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
    Settings,
)
from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
from daimon.core.scope import DeploymentDefault
from fastmcp import Client, FastMCP
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SENTINEL_TOKEN = "phase19-sentinel-pat-DO-NOT-LOG-2026"

pytestmark = pytest.mark.asyncio


async def _fixture_is_admin_resolver_false(_ctx: object) -> str | None:
    return None


async def test_combined_audit_log_across_broker_and_tool_omits_token_plaintext(
    db_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End-to-end invocation never logs the token plaintext on either logger.

    Captures DEBUG-level logs across BOTH ``daimon.core.broker`` and
    ``daimon.adapters.mcp.tools.cli_token``; asserts the sentinel never
    appears in caplog.text, in any record's formatted message, or in
    any record's args repr.
    """
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
    )
    account_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    # Insert the github credential row encrypted with the SENTINEL token.
    fernet = build_multifernet(tuple(k.get_secret_value() for k in settings.crypto.keys))
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=account_id,
        github_login="octocat",
        plaintext_token=SENTINEL_TOKEN,
        scopes=("repo",),
    )

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

    caplog.set_level(logging.DEBUG, logger="daimon.core.broker")
    caplog.set_level(logging.DEBUG, logger="daimon.adapters.mcp.tools.cli_token")

    async with Client(mcp) as client:
        result = await client.call_tool("get_cli_token", {"service": "github"})

    text = result.content[0].text  # type: ignore[union-attr]
    assert text == SENTINEL_TOKEN, (
        "tool must return the sentinel token verbatim — proves the "
        "broker + tool path actually executed"
    )
    assert SENTINEL_TOKEN not in caplog.text, (
        "sentinel token must not appear in any captured log line across broker + tool loggers"
    )
    for record in caplog.records:
        assert SENTINEL_TOKEN not in record.getMessage(), (
            f"sentinel must not appear in record.getMessage() for "
            f"{record.name}: {record.getMessage()!r}"
        )
        assert SENTINEL_TOKEN not in repr(record.args), (
            f"sentinel must not appear in record.args repr for {record.name}: {record.args!r}"
        )
