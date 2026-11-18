"""DB-backed tests for billing_panel.read.

Covers load_billing_snapshot, is_platform_admin, resolve_shown_names.
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
from daimon.adapters.discord.billing_panel.read import (
    invoking_channel_id,
    is_platform_admin,
    load_billing_snapshot,
    resolve_shown_names,
)
from daimon.core import platform_names

# pyright: reportPrivateUsage=false
from daimon.core.billing_panel import MemberRow
from daimon.core.platform_names import KnownName
from daimon.core.stores import tenant_ledger
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.platform_names import get_user_names, upsert_user_names
from daimon.testing.factories import (
    make_tenant,
    make_usage_event,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---- resolve_shown_names ----


def _row(user_id: str, stored: str | None = None) -> MemberRow:
    return MemberRow(
        platform_user_id=user_id,
        display_name=stored,
        cost_usd=1.0,
        turn_count=1,
        is_caller=False,
    )


def _member(name: str) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.display_name = name
    member.name = name.lower().replace(" ", "")
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


def _client(users: dict[int, tuple[str | None, str]]) -> tuple[discord.Client, list[int]]:
    """A client whose `fetch_user` answers (global name, username) from `users`, else NotFound."""
    client = MagicMock(spec=discord.Client)
    calls: list[int] = []

    async def fetch_user(snowflake: int) -> discord.User:
        calls.append(snowflake)
        if snowflake not in users:
            raise _http_error(discord.NotFound, 404)
        user = MagicMock(spec=discord.User)
        user.global_name, user.name = users[snowflake]
        return user

    client.fetch_user.side_effect = fetch_user
    return client, calls


async def test_resolve_shown_names_uses_the_cache_before_fetching() -> None:
    guild, calls = _guild(cached={100000000000000001: "alice"})

    [row], found = await resolve_shown_names(guild, (_row("100000000000000001"),))

    assert row.display_name == "alice", "a cached member is named from the cache"
    assert calls == [], "a cache hit needs no REST fetch"
    assert found == {"100000000000000001": KnownName("alice", "alice")}, "and is remembered"


async def test_resolve_shown_names_fetches_a_cache_miss() -> None:
    guild, calls = _guild(fetched={100000000000000002: "bob"})

    [row], _ = await resolve_shown_names(guild, (_row("100000000000000002", "old bob"),))

    assert row.display_name == "bob", "a live member fetch wins over the stored name"
    assert calls == [100000000000000002]


async def test_someone_who_left_shows_their_stored_name() -> None:
    guild, _ = _guild(fetched={})
    client, user_calls = _client({100000000000004993: ("Priya", "priya")})

    [row], found = await resolve_shown_names(
        guild, (_row("100000000000004993", "Priya N."),), client=client
    )

    assert row.display_name == "Priya N.", "NotFound (they left) falls back to the stored name"
    assert user_calls == [] and found == {}, "a stored name needs no user fetch"


async def test_someone_who_left_and_was_never_seen_gets_their_account_name() -> None:
    guild, _ = _guild(fetched={})
    client, _ = _client(
        {100000000000004993: ("Priya N.", "priya"), 100000000000004994: (None, "sam_o")}
    )

    rows, found = await resolve_shown_names(
        guild, (_row("100000000000004993"), _row("100000000000004994")), client=client
    )

    assert [r.display_name for r in rows] == ["Priya N.", "sam_o"], (
        "a user fetch answers for people who left: global name, else username"
    )
    assert found["100000000000004994"] == KnownName(None, "sam_o"), "and is remembered"


async def test_forbidden_and_http_errors_fall_back_to_the_stored_name() -> None:
    guild, _ = _guild(
        fetched={
            100000000000000003: _http_error(discord.Forbidden, 403),
            100000000000000004: _http_error(discord.HTTPException, 500),
        }
    )

    rows, _ = await resolve_shown_names(
        guild, (_row("100000000000000003", "Cy"), _row("100000000000000004", "Di"))
    )

    assert [r.display_name for r in rows] == ["Cy", "Di"]


async def test_a_fetch_still_pending_at_the_timeout_shows_the_stored_name() -> None:
    guild, _ = _guild(fetched={100000000000000005: "eve"}, slow=frozenset({100000000000000006}))

    async with asyncio.timeout(2):
        rows, _ = await resolve_shown_names(
            guild,
            (_row("100000000000000005"), _row("100000000000000006", "Sam O.")),
            timeout_s=0.1,
        )

    assert [r.display_name for r in rows] == ["eve", "Sam O."], (
        "the timed-out fetch shows the stored name; the others are named live"
    )


async def test_someone_never_named_to_us_keeps_no_name() -> None:
    guild, _ = _guild(fetched={})
    client, _ = _client({})

    [row], found = await resolve_shown_names(guild, (_row("100000000000000007"),), client=client)

    assert row.display_name is None and found == {}, "the panel then shows a mention"


async def test_resolve_shown_names_only_fetches_the_rows_the_panel_shows() -> None:
    ids = [f"1000000000000000{i:02d}" for i in range(10, 18)]
    guild, calls = _guild(fetched={int(uid): f"name{uid[-2:]}" for uid in ids})

    rows, _ = await resolve_shown_names(guild, tuple(_row(uid, "stored") for uid in ids))

    assert sorted(calls) == [int(uid) for uid in ids[:5]], "only the top five are fetched"
    assert [r.display_name for r in rows[:5]] == [f"name{uid[-2:]}" for uid in ids[:5]]
    assert rows[5].display_name == "stored", "rows past the top five keep what was stored"
    assert [r.platform_user_id for r in rows] == ids, "order is unchanged"


async def test_resolve_shown_names_skips_an_id_that_is_not_a_snowflake() -> None:
    guild, calls = _guild()

    [row], _ = await resolve_shown_names(guild, (_row("not-a-number"),))

    assert row.display_name is None and calls == [], "a bad id is neither fetched nor named"


async def test_the_admin_snapshot_names_spenders_and_remembers_live_names(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="777000000000000001")
    for uid, tokens in (("100000000000000011", 3000), ("100000000000000012", 2000)):
        await make_usage_event(db_session, tenant=tenant, platform_user_id=uid, input_tokens=tokens)
        await get_or_create_platform_principal(
            db_session, tenant_id=tenant.id, platform="discord", external_id=uid
        )
    await upsert_user_names(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        names={"100000000000000012": KnownName("Left Person", "left")},
    )
    await db_session.commit()
    platform_names.forget_all()
    guild, _ = _guild(fetched={100000000000000011: "Here Now"})

    state = await load_billing_snapshot(
        db_session,
        guild=guild,
        guild_id="777000000000000001",
        caller_user_id="100000000000000011",
        is_admin=True,
        since=datetime(2000, 1, 1, tzinfo=UTC),
        now=datetime.now(UTC),
        sessionmaker=db_session_factory,
    )
    await platform_names.settle()

    assert [row.display_name for row in state.member_rows] == ["Here Now", "Left Person"]
    known = await get_user_names(
        db_session, tenant_id=tenant.id, platform="discord", user_ids=["100000000000000011"]
    )
    assert known == {"100000000000000011": KnownName("Here Now", "herenow")}, (
        "a live name is stored for next time"
    )


# ---- is_platform_admin ----


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


def test_is_platform_admin_true_for_owner() -> None:
    assert is_platform_admin(_make_interaction(is_member=True, is_owner=True)), (
        "guild owner should always be admin"
    )


def test_is_platform_admin_true_for_manage_guild_perm() -> None:
    assert is_platform_admin(_make_interaction(is_member=True, manage_guild=True)), (
        "manage_guild perm should grant admin view"
    )


def test_is_platform_admin_true_for_administrator_perm() -> None:
    assert is_platform_admin(_make_interaction(is_member=True, administrator=True)), (
        "administrator perm should grant admin view"
    )


def test_is_platform_admin_false_for_regular_member() -> None:
    assert not is_platform_admin(_make_interaction(is_member=True)), (
        "member without manage_guild/administrator/owner should NOT be admin"
    )


def test_is_platform_admin_false_for_non_member_user() -> None:
    assert not is_platform_admin(_make_interaction(is_member=False)), (
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


def test_invoking_channel_id_resolves_a_thread_to_its_parent() -> None:
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = 42
    in_thread = MagicMock(channel=thread, channel_id=7)
    in_channel = MagicMock(channel=MagicMock(spec=discord.TextChannel), channel_id=7)
    assert invoking_channel_id(in_thread) == "42"
    assert invoking_channel_id(in_channel) == "7"
