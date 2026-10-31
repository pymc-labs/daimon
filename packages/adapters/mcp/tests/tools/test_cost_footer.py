"""DB-backed tests for the cost footer tools: admin-gated write, open read."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.cost_footer import (
    _get_cost_footer_impl,  # pyright: ignore[reportPrivateUsage]
    _set_cost_footer_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_cost_footer
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


def _auth(tenant_id: uuid.UUID, *, admin: bool, platform: str = "discord") -> AuthIdentity:
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


async def _stored(sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> bool:
    async with sessionmaker() as session:
        return await get_cost_footer(session, tenant_id=tenant_id)


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_admin_hides_and_shows_the_cost_footer(
    committing_sessionmaker: async_sessionmaker[AsyncSession], platform: str
) -> None:
    tenant_id = await _seed(committing_sessionmaker, platform)
    runtime = _runtime(committing_sessionmaker)
    auth = _auth(tenant_id, admin=True, platform=platform)

    hidden = await _set_cost_footer_impl(runtime, auth, False)
    assert hidden.shown is False, "the result must echo the new value"
    assert "without the footer" in hidden.note
    assert await _stored(committing_sessionmaker, tenant_id) is False, "hiding must be committed"

    shown = await _set_cost_footer_impl(runtime, auth, True)
    assert shown.shown is True
    assert await _stored(committing_sessionmaker, tenant_id) is True, "showing must be committed"


async def test_non_admin_is_refused_and_the_footer_stays(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed(committing_sessionmaker, "discord")

    with pytest.raises(ToolError, match="requires a server admin"):
        await _set_cost_footer_impl(
            _runtime(committing_sessionmaker), _auth(tenant_id, admin=False), False
        )

    assert await _stored(committing_sessionmaker, tenant_id) is True, (
        "a refused call must leave the footer shown"
    )


async def test_cli_tenant_is_refused_because_it_has_no_answers_in_chat(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed(committing_sessionmaker, "cli")

    with pytest.raises(ToolError, match="isn't available here"):
        await _set_cost_footer_impl(
            _runtime(committing_sessionmaker), _auth(tenant_id, admin=True, platform="cli"), False
        )


async def test_member_reads_the_current_value(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed(committing_sessionmaker, "discord")
    runtime = _runtime(committing_sessionmaker)

    before = await _get_cost_footer_impl(runtime, _auth(tenant_id, admin=False))
    assert before.shown is True, "a new tenant must read as shown"

    await _set_cost_footer_impl(runtime, _auth(tenant_id, admin=True), False)
    after = await _get_cost_footer_impl(runtime, _auth(tenant_id, admin=False))
    assert after.shown is False, "a member must see the admin's change"
