"""DB-backed tests for the plain-text mention tools: admin-gated write, open read."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.plain_text_mentions import (
    _get_plain_text_mentions_impl,  # pyright: ignore[reportPrivateUsage]
    _set_plain_text_mentions_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_plain_text_mentions
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        deployment_default=DeploymentDefault(),
    )


def _auth(tenant_id: uuid.UUID, *, admin: bool, platform: str = "teams") -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.ADMIN if admin else Role.USER,
        platform=platform,
        is_admin=admin,
    )


async def _seed(sessionmaker: async_sessionmaker[AsyncSession], platform: str) -> uuid.UUID:
    async with sessionmaker.begin() as session:
        return (await make_tenant(session, platform=platform)).id  # type: ignore[arg-type]


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_admin_turns_plain_text_mentions_on_and_off(
    committing_sessionmaker: async_sessionmaker[AsyncSession], platform: str
) -> None:
    """An admin's write is committed and the note says what changed."""
    tenant_id = await _seed(committing_sessionmaker, platform)
    runtime = _runtime(committing_sessionmaker)
    auth = _auth(tenant_id, admin=True, platform=platform)

    on = await _set_plain_text_mentions_impl(runtime, auth, True)
    assert on.enabled is True, "the result must echo the new value"
    assert "plain text" in on.note, "the note must describe the plain-text trigger"
    assert await get_plain_text_mentions(committing_sessionmaker, tenant_id=tenant_id), (
        "the flag must be committed on"
    )

    off = await _set_plain_text_mentions_impl(runtime, auth, False)
    assert off.enabled is False, "the result must echo the value turned off"
    assert not await get_plain_text_mentions(committing_sessionmaker, tenant_id=tenant_id), (
        "the flag must be committed off"
    )


async def test_non_admin_is_refused_and_nothing_is_written(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant-wide switch needs an admin; a refused call must not write."""
    tenant_id = await _seed(committing_sessionmaker, "teams")

    with pytest.raises(ToolError, match="requires an admin"):
        await _set_plain_text_mentions_impl(
            _runtime(committing_sessionmaker), _auth(tenant_id, admin=False), True
        )

    assert not await get_plain_text_mentions(committing_sessionmaker, tenant_id=tenant_id), (
        "the refused call must leave the flag off"
    )


async def test_cli_tenant_is_refused_because_it_has_no_channels(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The switch only means something on the chat platforms."""
    tenant_id = await _seed(committing_sessionmaker, "cli")

    with pytest.raises(ToolError, match="aren't available here"):
        await _set_plain_text_mentions_impl(
            _runtime(committing_sessionmaker), _auth(tenant_id, admin=True, platform="cli"), True
        )


async def test_member_reads_the_current_value(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Reading needs no admin and reflects the stored value."""
    tenant_id = await _seed(committing_sessionmaker, "slack")
    runtime = _runtime(committing_sessionmaker)

    before = await _get_plain_text_mentions_impl(runtime, _auth(tenant_id, admin=False))
    assert before.enabled is False, "a new tenant must read as off"
    assert "does not" in before.note, "the off note must say plain text does not trigger"

    await _set_plain_text_mentions_impl(
        runtime, _auth(tenant_id, admin=True, platform="slack"), True
    )
    after = await _get_plain_text_mentions_impl(runtime, _auth(tenant_id, admin=False))
    assert after.enabled is True, "a member must see the admin's change"
