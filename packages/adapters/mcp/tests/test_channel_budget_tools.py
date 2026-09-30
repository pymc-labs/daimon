"""DB-backed tests for the channel budget MCP tools.

The platform visibility lookup is patched on the tool module; the Discord
resolver itself is covered in `tools/test_thread_participation_verify.py`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import channel_budgets as tool_module
from daimon.adapters.mcp.tools.channel_budgets import (
    _clear_channel_budget_impl,  # pyright: ignore[reportPrivateUsage]
    _get_channel_budget_impl,  # pyright: ignore[reportPrivateUsage]
    _list_channel_budgets_impl,  # pyright: ignore[reportPrivateUsage]
    _set_channel_budget_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import channel_budgets
from daimon.core.stores.domain import Role, TenantRow
from daimon.testing.factories import (
    make_account,
    make_channel_budget,
    make_ledger_entry,
    make_tenant,
)
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PARENT = "222"
_THREAD = "999"


@pytest.fixture(autouse=True)
def visible(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every channel is visible and `_THREAD`'s parent is `_PARENT`."""
    calls: list[str] = []

    async def fake(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
        calls.append(channel_id)
        return _PARENT if channel_id == _THREAD else channel_id

    monkeypatch.setattr(tool_module, "_budget_channel", fake)
    return calls


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


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[TenantRow, uuid.UUID]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        return tenant, (await make_account(session, tenant=tenant)).id


def _auth(
    tenant: TenantRow, account_id: uuid.UUID, *, admin: bool, platform: str = "discord"
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant.id,
        role=Role.ADMIN if admin else Role.USER,
        platform=platform,
        is_admin=admin,
    )


async def test_admin_sets_a_budget_on_a_threads_parent_and_members_read_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        await make_ledger_entry(
            session, tenant=tenant, delta_usd=Decimal("-1.25"), channel_id=_PARENT
        )

    result = await _set_channel_budget_impl(
        runtime,
        _auth(tenant, account_id, admin=True),
        channel_id=_THREAD,
        limit_usd="5",
        window="monthly",
        starts_at=None,
        ends_at=None,
    )

    assert (result.channel_id, result.limit_usd) == (_PARENT, "5.00")
    assert Decimal(result.spent_usd) == Decimal("1.25"), "exact ledger precision, as a string"
    assert result.summary == "$1.25 of $5.00 (monthly)"
    lookup = await _get_channel_budget_impl(
        runtime, _auth(tenant, account_id, admin=False), _THREAD
    )
    assert lookup.channel_id == _PARENT
    assert lookup.budget is not None and Decimal(lookup.budget.remaining_usd) == Decimal("3.75")
    async with committing_sessionmaker() as session:
        stored = await channel_budgets.get_channel_budget(
            session, tenant_id=tenant.id, platform="discord", channel_id=_PARENT
        )
    assert stored is not None and stored.set_by_account_id == account_id


async def test_a_channel_without_a_budget_reads_as_none(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    lookup = await _get_channel_budget_impl(
        _runtime(committing_sessionmaker), _auth(tenant, account_id, admin=False), "333"
    )
    assert lookup.budget is None


async def test_get_without_a_channel_asks_for_one(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    with pytest.raises(ToolError, match="channel_id is required"):
        await _get_channel_budget_impl(
            _runtime(committing_sessionmaker), _auth(tenant, account_id, admin=False), None
        )


@pytest.mark.parametrize("call", ["list", "set", "clear"])
async def test_mutations_and_listing_are_admin_only(
    committing_sessionmaker: async_sessionmaker[AsyncSession], call: str
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    member = _auth(tenant, account_id, admin=False)
    async with committing_sessionmaker.begin() as session:
        await make_channel_budget(session, tenant=tenant, channel_id=_PARENT)

    with pytest.raises(ToolError, match="requires a workspace or server admin"):
        if call == "list":
            await _list_channel_budgets_impl(runtime, member)
        elif call == "set":
            await _set_channel_budget_impl(
                runtime,
                member,
                channel_id=_PARENT,
                limit_usd="0",
                window="total",
                starts_at=None,
                ends_at=None,
            )
        else:
            await _clear_channel_budget_impl(runtime, member, _PARENT)
    async with committing_sessionmaker() as session:
        (budget,) = await channel_budgets.list_channel_budgets(session, tenant_id=tenant.id)
    assert (budget.limit_usd, budget.window) == (Decimal("5"), "monthly"), "nothing changed"


async def test_a_bad_request_is_refused_before_any_lookup_or_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession], visible: list[str]
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    with pytest.raises(ToolError, match="needs both starts_at and ends_at. Nothing was saved"):
        await _set_channel_budget_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant, account_id, admin=True),
            channel_id=_PARENT,
            limit_usd="5",
            window="fixed",
            starts_at="2026-07-01",
            ends_at=None,
        )
    assert visible == []
    async with committing_sessionmaker() as session:
        assert await channel_budgets.list_channel_budgets(session, tenant_id=tenant.id) == []


async def test_admin_lists_this_platforms_budgets_and_clears_one(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    admin = _auth(tenant, account_id, admin=True)
    async with committing_sessionmaker.begin() as session:
        await make_channel_budget(session, tenant=tenant, channel_id="1")
        await make_channel_budget(session, tenant=tenant, channel_id="2", window="total")
        await make_channel_budget(session, tenant=tenant, platform="slack", channel_id="C")

    listed = await _list_channel_budgets_impl(runtime, admin)
    assert [(b.channel_id, b.window) for b in listed] == [("1", "monthly"), ("2", "total")]
    assert (await _clear_channel_budget_impl(runtime, admin, "1")).cleared
    assert not (await _clear_channel_budget_impl(runtime, admin, "1")).cleared
    assert [b.channel_id for b in await _list_channel_budgets_impl(runtime, admin)] == ["2"]


async def test_callers_without_a_chat_platform_are_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    with pytest.raises(ToolError, match="only for Discord servers and Slack workspaces"):
        await _list_channel_budgets_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant, account_id, admin=True, platform="cli"),
        )
