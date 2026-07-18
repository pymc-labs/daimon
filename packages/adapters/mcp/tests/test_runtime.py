from __future__ import annotations

from anthropic import AsyncAnthropic
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.scope import DeploymentDefault
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_mcp_runtime_is_frozen_dataclass_with_required_fields() -> None:
    """McpRuntime carries process-scoped collaborators and is immutable."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(McpRuntime)}
    assert fields == {
        "session_factory",
        "client",
        "settings",
        "deployment_default",
        "gemini_client",
        "file_store",
        "notebook_rate_limiter",
        "fernet",
    }, (
        "McpRuntime exposes core collaborators + deployment_default + optional media-tool slots + fernet"
    )
    # Frozen: assignment raises FrozenInstanceError
    sf: async_sessionmaker[AsyncSession] = async_sessionmaker()  # type: ignore[call-arg]
    rt = McpRuntime(
        session_factory=sf,
        client=AsyncAnthropic(api_key="x"),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        ),
        deployment_default=DeploymentDefault(),
    )
    try:
        rt.client = AsyncAnthropic(api_key="y")  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("McpRuntime should be frozen")


def test_mcp_runtime_media_fields_default_to_none() -> None:
    """gemini_client and file_store are optional, default to None
    so non-Gemini deployments don't have to construct them."""
    sf: async_sessionmaker[AsyncSession] = async_sessionmaker()  # type: ignore[call-arg]
    rt = McpRuntime(
        session_factory=sf,
        client=AsyncAnthropic(api_key="x"),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        ),
        deployment_default=DeploymentDefault(),
    )
    assert rt.gemini_client is None
    assert rt.file_store is None
