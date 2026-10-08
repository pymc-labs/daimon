"""DB-backed tests for billing_panel.read.

Covers load_billing_snapshot, is_guild_admin, resolve_shown_names.
Uses real Postgres via the `db_session` fixture.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import discord
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.discord.billing_panel.read import (
    invoking_channel_id,
    is_guild_admin,
    load_billing_snapshot,
    resolve_shown_names,
)

# pyright: reportPrivateUsage=false
from daimon.adapters.discord.billing_panel.state import BillingPanelState, MemberRow
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import (
    make_channel_budget,
    make_ledger_entry,
    make_tenant,
    make_usage_event,
)
from sqlalchemy.ext.asyncio import AsyncSession

# ---- resolve_shown_names ----


def _row(user_id: str) -> MemberRow:
    return MemberRow(
        platform_user_id=user_id,
        display_name=f"User {user_id[-4:]}",
        cost_usd=1.0,
        turn_count=1,
        is_caller=False,
    )


def _member(name: str) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.display_name = name
    return member


def _http_error(cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return cls(MagicMock(status=status, reason="no"), "no")


def _guild(
    *,
    cached: dict[int, str] | None = None,
    fetched: dict[int, str | discord.HTTPException] | None = None,
    slow: frozenset[int] = frozenset(),
) -> tuple[discord.Guild, list[int]]:
    """A guild whose cache holds `cached` and whose REST fetch answers from `fetched`.

    A `slow` id never answers in time. Returns the guild and the ids fetched.
    """
    guild = MagicMock(spec=discord.Guild)
    calls: list[int] = []

    def get_member(snowflake: int) -> discord.Member | None:
        name = (cached or {}).get(snowflake)
        return None if name is None else _member(name)

    async def fetch_member(snowflake: int) -> discord.Member:
        calls.append(snowflake)
        if snowflake in slow:
            await asyncio.sleep(10)
        answer = (fetched or {}).get(snowflake)
        if answer is None:
            raise _http_error(discord.NotFound, 404)
        if isinstance(answer, discord.HTTPException):
            raise answer
        return _member(answer)

    guild.get_member.side_effect = get_member
    guild.fetch_member.side_effect = fetch_member
    return guild, calls


async def test_resolve_shown_names_uses_the_cache_before_fetching() -> None:
    guild, calls = _guild(cached={100000000000000001: "alice"})

    [row] = await resolve_shown_names(guild, (_row("100000000000000001"),))

    assert row.display_name == "alice", "a cached member is named from the cache"
    assert calls == [], "a cache hit needs no REST fetch"


async def test_resolve_shown_names_fetches_a_cache_miss() -> None:
    guild, calls = _guild(fetched={100000000000000002: "bob"})

    [row] = await resolve_shown_names(guild, (_row("100000000000000002"),))

    assert row.display_name == "bob", "a cache miss is named from a member fetch"
    assert calls == [100000000000000002]


async def test_resolve_shown_names_keeps_the_label_for_someone_who_left() -> None:
    guild, _ = _guild(fetched={})

    [row] = await resolve_shown_names(guild, (_row("100000000000004993"),))

    assert row.display_name == "User 4993", "NotFound (they left) keeps `User XXXX`"


async def test_resolve_shown_names_keeps_the_label_on_forbidden_and_http_errors() -> None:
    guild, _ = _guild(
        fetched={
            100000000000000003: _http_error(discord.Forbidden, 403),
            100000000000000004: _http_error(discord.HTTPException, 500),
        }
    )

    rows = await resolve_shown_names(
        guild, (_row("100000000000000003"), _row("100000000000000004"))
    )

    assert [r.display_name for r in rows] == ["User 0003", "User 0004"], (
        "Forbidden and other HTTP errors fall back to `User XXXX`"
    )


async def test_resolve_shown_names_gives_up_on_slow_fetches_after_the_timeout() -> None:
    guild, _ = _guild(fetched={100000000000000005: "eve"}, slow=frozenset({100000000000000006}))

    async with asyncio.timeout(2):
        rows = await resolve_shown_names(
            guild, (_row("100000000000000005"), _row("100000000000000006")), timeout_s=0.1
        )

    assert [r.display_name for r in rows] == ["eve", "User 0006"], (
        "a fetch still pending at the timeout keeps `User XXXX`; the others are named"
    )


async def test_resolve_shown_names_only_fetches_the_rows_the_panel_shows() -> None:
    ids = [f"1000000000000000{i:02d}" for i in range(10, 18)]
    guild, calls = _guild(fetched={int(uid): f"name{uid[-2:]}" for uid in ids})

    rows = await resolve_shown_names(guild, tuple(_row(uid) for uid in ids))

    assert sorted(calls) == [int(uid) for uid in ids[:5]], "only the top five are fetched"
    assert [r.display_name for r in rows[:5]] == [f"name{uid[-2:]}" for uid in ids[:5]]
    assert rows[5].display_name == "User 0015", "rows past the top five keep `User XXXX`"
    assert [r.platform_user_id for r in rows] == ids, "order is unchanged"


async def test_resolve_shown_names_skips_an_id_that_is_not_a_snowflake() -> None:
    guild, calls = _guild()

    [row] = await resolve_shown_names(guild, (_row("not-a-number"),))

    assert row.display_name == "User mber" and calls == [], "a bad id is neither fetched nor named"


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

    async def _fetch_member(snowflake: int) -> Any:
        raise discord.NotFound(MagicMock(status=404, reason="no"), "Unknown Member")

    guild.get_member.side_effect = _get_member
    guild.fetch_member.side_effect = _fetch_member
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
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    # Ensure the tenant row exists (FK requirement); idempotent across calls
    # (check-then-create — this helper is called multiple times per test with
    # the same default guild_id).
    if await get_tenant(session, tenant_id) is None:
        await make_tenant(session, platform="discord", workspace_id=guild_id)
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
        now=datetime.now(UTC),
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
        now=datetime.now(UTC),
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
        now=datetime.now(UTC),
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
    since = datetime.now(UTC) - timedelta(days=1)
    caller = "100000000000000001"
    await _record_usage(db_session, user_id=caller, session_id="s_a", event_id="e_a")
    # Insert a NULL-attributed row via the make_usage_event factory (supports
    # platform_user_id=None for exactly this edge case).
    tenant_row = await get_tenant(
        db_session, derive_tenant_uuid(platform="discord", workspace_id="guild_1")
    )
    assert tenant_row is not None, "_record_usage above must have provisioned the guild_1 tenant"
    await make_usage_event(
        db_session,
        tenant=tenant_row,
        platform_user_id=None,
        managed_session_id="s_null",
        model="claude-opus-4-7",
        input_tokens=5_000_000,
        event_id="e_null",
    )
    guild = _make_guild_with_members({})

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id="guild_1",
        caller_user_id=caller,
        is_admin=True,
        since=since,
        now=datetime.now(UTC),
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
        now=datetime.now(UTC),
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
    tenant = await make_tenant(session, platform="discord", workspace_id=guild_id)
    return tenant.id


async def test_load_billing_snapshot_uses_derived_tenant_id(
    db_session: AsyncSession,
) -> None:
    """load_billing_snapshot reads the balance at the deterministically derived tenant_id.

    Tenant identity is derive_tenant_uuid(platform='discord', workspace_id=guild_id) —
    there is no workspaces-table lookup. Ledger credits posted against that derived UUID
    must appear in the snapshot.
    """
    guild_id = f"gbal_derived_{uuid.uuid4().hex[:8]}"
    tenant = await make_tenant(db_session, platform="discord", workspace_id=guild_id)
    derived_tenant_id = tenant.id
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
        now=datetime.now(UTC),
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
        now=datetime.now(UTC),
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
        now=datetime.now(UTC),
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
        now=datetime.now(UTC),
    )

    assert state.guild_balance_usd == Decimal("0"), (
        "empty ledger should yield guild_balance_usd of Decimal('0')"
    )


def test_invoking_channel_id_resolves_a_thread_to_its_parent() -> None:
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = 42
    in_thread = MagicMock(channel=thread, channel_id=7)
    in_channel = MagicMock(channel=MagicMock(spec=discord.TextChannel), channel_id=7)
    assert invoking_channel_id(in_thread) == "42"
    assert invoking_channel_id(in_channel) == "7"


async def test_load_billing_snapshot_reads_the_invoking_channels_budget(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="guild_b")
    await make_channel_budget(db_session, tenant=tenant, channel_id="c1", limit_usd=Decimal("5"))
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("-2"), channel_id="c1")
    guild = _make_guild_with_members({})
    since = datetime.now(UTC) - timedelta(days=1)

    for is_admin in (False, True):
        state = await load_billing_snapshot(
            db_session,
            guild=guild,
            guild_id="guild_b",
            caller_user_id="1",
            is_admin=is_admin,
            since=since,
            now=datetime.now(UTC),
            channel_id="c1",
        )
        assert state.channel_budget is not None
        assert state.channel_budget.spent_usd == Decimal("2")
    unbudgeted = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id="guild_b",
        caller_user_id="1",
        is_admin=False,
        since=since,
        now=datetime.now(UTC),
        channel_id="c2",
    )
    assert unbudgeted.channel_budget is None


async def test_load_billing_snapshot_lists_channel_budgets_for_admins_only(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="guild_c")
    await make_channel_budget(db_session, tenant=tenant, channel_id="c1", limit_usd=Decimal("5"))
    await make_channel_budget(db_session, tenant=tenant, channel_id="c2", limit_usd=Decimal("5"))
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("-4"), channel_id="c2")
    guild = _make_guild_with_members({})
    now = datetime.now(UTC)

    async def snapshot(is_admin: bool) -> BillingPanelState:
        return await load_billing_snapshot(
            db_session,
            guild=guild,
            guild_id="guild_c",
            caller_user_id="1",
            is_admin=is_admin,
            since=now - timedelta(days=1),
            now=now,
        )

    admin = await snapshot(True)
    assert [s.budget.channel_id for s in admin.channel_budgets] == ["c2", "c1"], "most used first"
    assert (await snapshot(False)).channel_budgets == (), "a member sees no other channel"
