"""DB-backed tests for billing_panel.read.

Covers load_billing_snapshot, is_guild_admin, _resolve_member_name.
Uses real Postgres via the `db_session` fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)

# pyright: reportPrivateUsage=false
from daimon.adapters.discord.billing_panel.read import (
    _resolve_member_name,
    is_guild_admin,
    load_billing_snapshot,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import tenant_ledger, usage_events
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ---- _resolve_member_name ----


def test_resolve_member_name_returns_display_name_on_cache_hit() -> None:
    guild = MagicMock(spec=discord.Guild)
    member = MagicMock(spec=discord.Member)
    member.display_name = "alice"
    guild.get_member.return_value = member
    assert _resolve_member_name(guild, "100000000000000001") == "alice", (
        "cache hit should return member.display_name"
    )


def test_resolve_member_name_falls_back_to_last_four_on_cache_miss() -> None:
    guild = MagicMock(spec=discord.Guild)
    guild.get_member.return_value = None
    assert _resolve_member_name(guild, "100000000000004993") == "User 4993", (
        "cache miss should fall back to 'User XXXX' (last 4 of snowflake)"
    )


def test_resolve_member_name_returns_unknown_for_short_id() -> None:
    guild = MagicMock(spec=discord.Guild)
    guild.get_member.return_value = None
    assert _resolve_member_name(guild, "ab") == "<unknown user>", (
        "ids shorter than 4 chars cannot form a User XXXX label"
    )


def test_resolve_member_name_handles_non_numeric_id() -> None:
    guild = MagicMock(spec=discord.Guild)
    # int("not-a-number") raises ValueError — helper must swallow it.
    assert _resolve_member_name(guild, "not-a-number").startswith(("User ", "<unknown")), (
        "non-numeric id should not raise; falls back to id-suffix or unknown"
    )


def test_resolve_member_name_handles_none_guild() -> None:
    assert _resolve_member_name(None, "100000000000004993") == "User 4993", (
        "absent guild should still produce a stable fallback label"
    )


# ---- is_guild_admin ----


def _make_interaction(
    *,
    is_member: bool,
    administrator: bool = False,
    manage_guild: bool = False,
    is_owner: bool = False,
) -> Any:
    interaction = MagicMock()
    if is_member:
        member = MagicMock(spec=discord.Member)
        member.id = 42
        perms = MagicMock(spec=discord.Permissions)
        perms.administrator = administrator
        perms.manage_guild = manage_guild
        member.guild_permissions = perms
        interaction.user = member
        guild = MagicMock(spec=discord.Guild)
        guild.owner_id = 42 if is_owner else 999
        interaction.guild = guild
    else:
        # Non-Member (e.g., User in a DM context — guild_only should prevent this,
        # but guard for it explicitly).
        interaction.user = MagicMock(spec=discord.User)
        interaction.guild = None
    return interaction


def test_is_guild_admin_true_for_owner() -> None:
    assert is_guild_admin(_make_interaction(is_member=True, is_owner=True)), (
        "guild owner should always be admin"
    )


def test_is_guild_admin_true_for_manage_guild_perm() -> None:
    assert is_guild_admin(_make_interaction(is_member=True, manage_guild=True)), (
        "manage_guild perm should grant admin view"
    )


def test_is_guild_admin_true_for_administrator_perm() -> None:
    assert is_guild_admin(_make_interaction(is_member=True, administrator=True)), (
        "administrator perm should grant admin view"
    )


def test_is_guild_admin_false_for_regular_member() -> None:
    assert not is_guild_admin(_make_interaction(is_member=True)), (
        "member without manage_guild/administrator/owner should NOT be admin"
    )


def test_is_guild_admin_false_for_non_member_user() -> None:
    assert not is_guild_admin(_make_interaction(is_member=False)), (
        "non-Member interaction.user (DM context) should NOT be admin"
    )


# ---- load_billing_snapshot ----


def _make_guild_with_members(members: dict[int, str], owner_id: int = 999) -> discord.Guild:
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = owner_id

    def _get_member(snowflake: int) -> Any:
        if snowflake in members:
            m = MagicMock(spec=discord.Member)
            m.display_name = members[snowflake]
            return m
        return None

    guild.get_member.side_effect = _get_member
    return guild


async def _record_usage(
    session: AsyncSession,
    *,
    user_id: str,
    session_id: str,
    event_id: str,
    guild_id: str = "guild_1",
    input_tokens: int = 1_000_000,
) -> None:
    from daimon.core._models import Tenant

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    # Ensure the tenant row exists (FK requirement); idempotent across calls.
    from sqlalchemy.dialects.postgresql import insert as pg_insert_tenant

    await session.execute(
        pg_insert_tenant(Tenant)
        .values(id=tenant_id, platform="discord", external_id=guild_id)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.flush()
    await usage_events.record(
        session,
        tenant_id=tenant_id,
        platform_user_id=user_id,
        managed_session_id=session_id,
        model="claude-opus-4-7",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=input_tokens,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        event_id=event_id,
    )


async def test_load_billing_snapshot_regular_view_excludes_other_users(
    db_session: AsyncSession,
) -> None:
    since = datetime.now(UTC) - timedelta(days=1)
    caller = "100000000000000001"
    other = "100000000000000002"
    await _record_usage(db_session, user_id=caller, session_id="s_a", event_id="e_a")
    await _record_usage(db_session, user_id=other, session_id="s_b", event_id="e_b")
    guild = _make_guild_with_members({})

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id="guild_1",
        caller_user_id=caller,
        is_admin=False,
        since=since,
    )

    assert state.is_admin is False, "regular view should set is_admin=False"
    assert state.caller_spend > 0, "caller should have nonzero spend"
    assert state.member_rows == (), "regular view must not include any per-member rows"
    assert state.guild_spend == 0.0 and state.guild_turns == 0, (
        "regular view must not populate guild aggregates"
    )


async def test_load_billing_snapshot_admin_view_includes_per_member_breakdown(
    db_session: AsyncSession,
) -> None:
    since = datetime.now(UTC) - timedelta(days=1)
    caller = "100000000000000001"
    big_spender = "100000000000000002"
    # caller: 1 turn at 1M input tokens
    await _record_usage(db_session, user_id=caller, session_id="s_a", event_id="e_a")
    # big_spender: 1 turn at 10M input tokens (more expensive)
    await _record_usage(
        db_session,
        user_id=big_spender,
        session_id="s_b",
        event_id="e_b",
        input_tokens=10_000_000,
    )
    guild = _make_guild_with_members(
        {
            int(caller): "alice",
            int(big_spender): "bob",
        }
    )

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id="guild_1",
        caller_user_id=caller,
        is_admin=True,
        since=since,
    )

    assert state.is_admin is True, "admin view should set is_admin=True"
    assert len(state.member_rows) == 2, "expected exactly two spending members"
    assert state.member_rows[0].platform_user_id == big_spender, (
        "rows should be sorted by spend desc — big_spender first"
    )
    assert state.member_rows[1].platform_user_id == caller
    caller_row = state.member_rows[1]
    assert caller_row.is_caller is True, "caller's row should be flagged is_caller=True"
    assert state.member_rows[0].is_caller is False
    assert state.member_rows[0].display_name == "bob"
    assert state.member_rows[1].display_name == "alice"
    assert state.guild_distinct_members == 2


async def test_load_billing_snapshot_admin_view_top_25_truncation(
    db_session: AsyncSession,
) -> None:
    since = datetime.now(UTC) - timedelta(days=1)
    caller = "100000000000000001"
    # Insert 30 distinct spenders with increasing spend so order is deterministic.
    for i in range(30):
        uid = f"1000000000000{i:05d}"
        await _record_usage(
            db_session,
            user_id=uid,
            session_id=f"s_{i}",
            event_id=f"e_{i}",
            input_tokens=(i + 1) * 100_000,
        )
    guild = _make_guild_with_members({})

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id="guild_1",
        caller_user_id=caller,
        is_admin=True,
        since=since,
    )

    assert len(state.member_rows) == 25, "should truncate to top-25 spenders"
    assert state.over_cap_count == 5, "5 members beyond top-25"
    assert state.guild_distinct_members == 30, (
        "K members should be total distinct spenders, not capped at 25"
    )


async def test_load_billing_snapshot_excludes_null_user_rows_from_guild_total(
    db_session: AsyncSession,
) -> None:
    """Rows with platform_user_id IS NULL must not appear in guild distinct count."""
    from daimon.core._models import UsageEvent

    since = datetime.now(UTC) - timedelta(days=1)
    caller = "100000000000000001"
    await _record_usage(db_session, user_id=caller, session_id="s_a", event_id="e_a")
    # Insert a NULL-attributed row directly via the ORM.
    db_session.add(
        UsageEvent(
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id="guild_1"),
            platform_user_id=None,
            managed_session_id="s_null",
            model="claude-opus-4-7",
            input_tokens=5_000_000,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            occurred_at=datetime.now(UTC),
            event_id="e_null",
        )
    )
    await db_session.flush()
    guild = _make_guild_with_members({})

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id="guild_1",
        caller_user_id=caller,
        is_admin=True,
        since=since,
    )

    assert state.guild_distinct_members == 1, (
        "NULL platform_user_id rows must not count as a distinct member"
    )


async def test_load_billing_snapshot_regular_view_empty_state(
    db_session: AsyncSession,
) -> None:
    since = datetime.now(UTC) - timedelta(days=1)
    guild = _make_guild_with_members({})

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id="guild_1",
        caller_user_id="100000000000000001",
        is_admin=False,
        since=since,
    )

    assert state.caller_spend == 0.0, "no rows -> zero spend"
    assert state.caller_turns == 0, "no rows -> zero turns"
    assert state.caller_cap is None, "no cap configured -> None"
    assert state.member_rows == (), "regular empty state -> empty member_rows"


# ---- guild_balance_usd in snapshot ----


async def _seed_tenant_for_guild(session: AsyncSession, guild_id: str) -> uuid.UUID:
    """Create a tenants row at the deterministically derived UUID for (discord, guild_id).

    The tenant_ledger FK requires the tenant to exist. load_billing_snapshot
    derives tenant_id via derive_tenant_uuid — no workspaces table lookup is needed.
    """
    from daimon.core._models import Tenant

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    session.add(Tenant(id=tenant_id, platform="discord", external_id=guild_id))
    await session.flush()
    return tenant_id


async def test_load_billing_snapshot_uses_derived_tenant_id(
    db_session: AsyncSession,
) -> None:
    """load_billing_snapshot reads the balance at the deterministically derived tenant_id.

    Tenant identity is derive_tenant_uuid(platform='discord', workspace_id=guild_id) —
    there is no workspaces-table lookup. Ledger credits posted against that derived UUID
    must appear in the snapshot.
    """
    from daimon.core._models import Tenant

    guild_id = f"gbal_derived_{uuid.uuid4().hex[:8]}"
    derived_tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)

    db_session.add(Tenant(id=derived_tenant_id, platform="discord", external_id=guild_id))
    await db_session.flush()
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=derived_tenant_id,
        delta_usd=Decimal("10000.00"),
        reason="topup",
        idempotency_key=f"topup:derived_{guild_id}",
    )

    state = await load_billing_snapshot(
        db_session,
        guild=_make_guild_with_members({}),
        guild_id=guild_id,
        caller_user_id="100000000000000001",
        is_admin=False,
        since=datetime.now(UTC) - timedelta(days=1),
    )

    assert state.guild_balance_usd == Decimal("10000.00"), (
        "balance must come from derive_tenant_uuid(discord, guild_id)"
    )


async def test_load_billing_snapshot_member_view_carries_guild_balance(
    db_session: AsyncSession,
) -> None:
    """Regular (member) view must carry guild_balance_usd from the ledger."""
    guild_id = f"gbal_member_{uuid.uuid4().hex[:8]}"
    tenant_id = await _seed_tenant_for_guild(db_session, guild_id)
    since = datetime.now(UTC) - timedelta(days=1)
    guild = _make_guild_with_members({})

    # Seed a topup ledger entry for this tenant.
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_id,
        delta_usd=Decimal("30.00"),
        reason="topup",
        idempotency_key=f"topup:test_member_{guild_id}",
    )

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id=guild_id,
        caller_user_id="100000000000000001",
        is_admin=False,
        since=since,
    )

    assert state.guild_balance_usd == Decimal("30.00"), (
        "member view snapshot must include the guild balance from the ledger"
    )


async def test_load_billing_snapshot_admin_view_carries_guild_balance(
    db_session: AsyncSession,
) -> None:
    """Admin view must carry guild_balance_usd from the ledger."""
    guild_id = f"gbal_admin_{uuid.uuid4().hex[:8]}"
    tenant_id = await _seed_tenant_for_guild(db_session, guild_id)
    since = datetime.now(UTC) - timedelta(days=1)
    guild = _make_guild_with_members({})

    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_id,
        delta_usd=Decimal("75.50"),
        reason="topup",
        idempotency_key=f"topup:test_admin_{guild_id}",
    )

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id=guild_id,
        caller_user_id="100000000000000001",
        is_admin=True,
        since=since,
    )

    assert state.guild_balance_usd == Decimal("75.50"), (
        "admin view snapshot must include the guild balance from the ledger"
    )


async def test_load_billing_snapshot_guild_balance_zero_when_no_ledger_rows(
    db_session: AsyncSession,
) -> None:
    """Unprovisioned guild (no workspace row) -> guild_balance_usd is Decimal('0')."""
    guild_id = f"gbal_zero_{uuid.uuid4().hex[:8]}"
    since = datetime.now(UTC) - timedelta(days=1)
    guild = _make_guild_with_members({})

    # No workspace row -> resolve_tenant_from_workspace returns None -> balance is 0
    # without touching the ledger.
    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id=guild_id,
        caller_user_id="100000000000000001",
        is_admin=False,
        since=since,
    )

    assert state.guild_balance_usd == Decimal("0"), (
        "empty ledger should yield guild_balance_usd of Decimal('0')"
    )
