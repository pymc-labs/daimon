"""DB-backed tests for the channel budget MCP tools.

The platform visibility lookup is patched on the tool module; the Discord
resolver itself is covered in `tools/test_thread_participation_verify.py`.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import channel_budgets as tool_module
from daimon.adapters.mcp.tools.channel_budgets import (
    _clear_channel_budget_impl,  # pyright: ignore[reportPrivateUsage]
    _get_channel_budget_impl,  # pyright: ignore[reportPrivateUsage]
    _list_channel_budgets_impl,  # pyright: ignore[reportPrivateUsage]
    _resolve_slack_channel,  # pyright: ignore[reportPrivateUsage]
    _set_channel_budget_impl,  # pyright: ignore[reportPrivateUsage]
    origin_budget_channel,
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import channel_budgets
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.factories import (
    make_account,
    make_channel_budget,
    make_dm_conversation,
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


async def _origin(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant: TenantRow,
    account_id: uuid.UUID,
    *,
    parent_channel_id: str,
    thread_id: str,
) -> str:
    now = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account_id,
            platform="discord",
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            responder_ma_agent_id="agent_x",
            responder_name="daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
    return str(origin.id)


async def _dm(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant: TenantRow,
    account_id: uuid.UUID,
    *,
    scope_id: str,
    source_channel_id: str | None,
) -> None:
    async with sessionmaker.begin() as session:
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account_id,
            route_key=f"route-{scope_id}",
            scope_id=scope_id,
            source_channel_id=source_channel_id,
        )


async def test_get_without_a_channel_reads_the_calling_turns_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession], visible: list[str]
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        await make_channel_budget(session, tenant=tenant, channel_id=_PARENT)
    runtime = _runtime(committing_sessionmaker)
    member = _auth(tenant, account_id, admin=False)
    here = await _origin(
        committing_sessionmaker, tenant, account_id, parent_channel_id=_PARENT, thread_id=_THREAD
    )
    dm = await _origin(
        committing_sessionmaker, tenant, account_id, parent_channel_id="dm-chan", thread_id="dm:x"
    )
    await _dm(committing_sessionmaker, tenant, account_id, scope_id="dm:x", source_channel_id=None)
    moved = await _origin(
        committing_sessionmaker, tenant, account_id, parent_channel_id="dm-chan", thread_id="dm:m"
    )
    await _dm(
        committing_sessionmaker, tenant, account_id, scope_id="dm:m", source_channel_id=_PARENT
    )

    lookup = await _get_channel_budget_impl(runtime, member, None, here)
    assert lookup.channel_id == _PARENT and lookup.budget is not None
    assert visible == [], "the origin already places the caller in the channel"
    from_dm = await _get_channel_budget_impl(runtime, member, None, moved)
    assert from_dm.channel_id == _PARENT and from_dm.budget is not None, (
        "a moved DM reads the budget of the channel it came from"
    )
    with pytest.raises(ToolError, match="direct message belongs to no channel"):
        await _get_channel_budget_impl(runtime, member, None, dm)
    with pytest.raises(ToolError, match="unavailable or expired"):
        await _get_channel_budget_impl(runtime, member, None, str(uuid.uuid4()))


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


async def test_clear_resolves_a_visible_thread_and_takes_any_other_id_as_given(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
        if channel_id == _THREAD:
            return _PARENT
        raise ToolError("cannot see that channel")

    monkeypatch.setattr(tool_module, "_budget_channel", fake)
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    admin = _auth(tenant, account_id, admin=True)
    async with committing_sessionmaker.begin() as session:
        await make_channel_budget(session, tenant=tenant, channel_id=_PARENT)
        await make_channel_budget(session, tenant=tenant, channel_id="404")

    thread = await _clear_channel_budget_impl(runtime, admin, _THREAD)
    assert (thread.channel_id, thread.cleared) == (_PARENT, True), "a thread clears its parent"
    gone = await _clear_channel_budget_impl(runtime, admin, "404")
    assert (gone.channel_id, gone.cleared) == ("404", True), "a hidden channel is cleared as given"


async def test_origin_budget_channel_is_the_turns_channel_or_none(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    member = _auth(tenant, account_id, admin=False)
    here = await _origin(
        committing_sessionmaker, tenant, account_id, parent_channel_id=_PARENT, thread_id=_THREAD
    )
    dm = await _origin(
        committing_sessionmaker, tenant, account_id, parent_channel_id="dm-chan", thread_id="dm:x"
    )
    await _dm(committing_sessionmaker, tenant, account_id, scope_id="dm:x", source_channel_id=None)
    moved = await _origin(
        committing_sessionmaker, tenant, account_id, parent_channel_id="dm-chan", thread_id="dm:m"
    )
    await _dm(
        committing_sessionmaker, tenant, account_id, scope_id="dm:m", source_channel_id=_PARENT
    )
    other, other_account = await _seed(committing_sessionmaker)
    await _dm(committing_sessionmaker, other, other_account, scope_id="dm:y", source_channel_id="7")
    foreign = await _origin(
        committing_sessionmaker, tenant, account_id, parent_channel_id="dm-chan", thread_id="dm:y"
    )
    responder = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_x")

    async def channel(auth: AuthIdentity, origin_context_id: str | None) -> str | None:
        return await origin_budget_channel(committing_sessionmaker, auth, origin_context_id)

    assert await channel(member, here) == _PARENT
    assert await channel(dataclasses.replace(member, agent_id=responder), here) == _PARENT
    other_agent = dataclasses.replace(member, agent_id=uuid.uuid4())
    assert await channel(other_agent, here) is None, "another agent's origin is not attributed"
    assert await channel(member, moved) == _PARENT, "a moved DM counts toward its source"
    assert await channel(member, dm) is None, "an older DM belongs to no channel"
    assert await channel(member, foreign) is None, "another tenant's DM is not consulted"
    assert await channel(member, str(uuid.uuid4())) is None, "an unknown origin"
    assert await channel(member, "not-a-uuid") is None, "a malformed origin"
    assert await channel(member, None) is None


@pytest.mark.parametrize(("is_private", "caller_in_channel"), [(False, False), (True, True)])
async def test_slack_channel_resolves_when_the_caller_can_see_it(
    monkeypatch: pytest.MonkeyPatch, is_private: bool, caller_in_channel: bool
) -> None:
    client = _slack_client(monkeypatch, is_private=is_private, caller_in_channel=caller_in_channel)
    resolved = await _resolve_slack_channel(MagicMock(), _slack_auth(), "C1:1717.5")
    assert resolved == "C1", "a thread resolves to its channel"
    client.conversations_info.assert_awaited_once_with(channel="C1")


async def test_slack_channel_hidden_from_the_caller_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _slack_client(monkeypatch, is_private=True, caller_in_channel=False)
    with pytest.raises(ToolError, match="missing channel access"):
        await _resolve_slack_channel(MagicMock(), _slack_auth(), "C1")


def _slack_auth() -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="slack",
        external_id="T1",
        platform_user_id="U1",
    )


def _slack_client(
    monkeypatch: pytest.MonkeyPatch, *, is_private: bool, caller_in_channel: bool
) -> MagicMock:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        return_value={"channel": {"id": "C1", "is_member": True, "is_private": is_private}}
    )
    client.users_info = AsyncMock(return_value={"user": {"id": "U1"}})
    client.conversations_members = AsyncMock(
        return_value={"members": ["U1"] if caller_in_channel else ["U2"]}
    )

    async def fake_client(runtime: object, *, team_id: str) -> MagicMock:
        assert team_id == "T1", "resolved in the caller's own workspace"
        return client

    monkeypatch.setattr(tool_module, "slack_web_client", fake_client)
    return client
