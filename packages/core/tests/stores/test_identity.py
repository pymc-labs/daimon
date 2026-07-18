from __future__ import annotations

import uuid

import pytest
from daimon.core._models import ChannelConfig, PlatformPrincipal
from daimon.core.errors import StoreError
from daimon.core.stores.identity import (
    count_principal_links_for_principal,
    create_principal_link,
    delete_for_principal,
    delete_principal_link,
    delete_principal_links_for_principal,
    find_platform_principal,
    get_discord_principal_for_account,
    get_or_create_cli_principal,
    get_or_create_platform_principal,
    get_slack_principal_for_account,
    list_cli_principals_for_account,
    list_links_for_cli,
    list_platform_principals_for_account,
    resolve_linked_platform_principal,
    set_active_agent_name,
)
from daimon.testing.factories import (
    link_principals,
    make_account,
    make_cli_principal,
    make_platform_principal,
    make_tenant,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def test_get_or_create_cli_principal_creates_new_account_when_unseen(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="alice")
    assert principal.os_user == "alice"
    assert principal.tenant_id == tenant.id
    assert principal.account_id is not None, "first-seen must mint an account"

    # Second call returns the same principal, same account.
    again = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="alice")
    assert again.id == principal.id
    assert again.account_id == principal.account_id


async def test_get_or_create_platform_principal_creates_new_account_when_unseen(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    p = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="12345"
    )
    assert p.platform == "discord"
    assert p.external_id == "12345"
    assert p.tenant_id == tenant.id

    again = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="12345"
    )
    assert again.id == p.id


async def test_cli_principal_tenant_isolation(
    db_session: AsyncSession,
) -> None:
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    p1 = await get_or_create_cli_principal(db_session, tenant_id=t1.id, os_user="alice")
    p2 = await get_or_create_cli_principal(db_session, tenant_id=t2.id, os_user="alice")
    assert p1.id != p2.id, "same os_user in different tenants must yield distinct principals"
    assert p1.account_id != p2.account_id


async def test_platform_principal_tenant_isolation(
    db_session: AsyncSession,
) -> None:
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    p1 = await get_or_create_platform_principal(
        db_session, tenant_id=t1.id, platform="discord", external_id="99"
    )
    p2 = await get_or_create_platform_principal(
        db_session, tenant_id=t2.id, platform="discord", external_id="99"
    )
    assert p1.id != p2.id, "different tenants must yield distinct principals"
    assert p1.account_id != p2.account_id


async def test_get_or_create_platform_principal_different_external_id_yields_distinct_principal(
    db_session: AsyncSession,
) -> None:
    """Different external_ids within the same tenant must yield distinct principals."""
    tenant = await make_tenant(db_session)
    p1 = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="user-a"
    )
    p2 = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="user-b"
    )
    assert p1.id != p2.id, (
        "distinct external_ids in the same tenant must produce distinct principals"
    )
    assert p1.account_id != p2.account_id, (
        "each new external_id must mint its own account — no account sharing"
    )
    # Idempotency: re-fetching each returns the same row.
    p1_again = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="user-a"
    )
    assert p1_again.id == p1.id, (
        "idempotent: same (tenant, platform, external_id) returns same principal"
    )
    assert p1_again.account_id == p1.account_id, (
        "idempotent: account_id must not change on repeat call"
    )


async def test_create_principal_link_connects_cli_to_platform_when_both_exist(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    cli = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="op")
    plat = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="999"
    )
    link = await create_principal_link(
        db_session, cli_principal_id=cli.id, platform_principal_id=plat.id
    )
    assert link.cli_principal_id == cli.id
    assert link.platform_principal_id == plat.id


async def test_list_links_returns_only_current_cli_principal_links_when_queried(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    cli_a = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="a")
    cli_b = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="b")
    plat = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="x"
    )
    await create_principal_link(
        db_session, cli_principal_id=cli_a.id, platform_principal_id=plat.id
    )

    a_links = await list_links_for_cli(db_session, cli_principal_id=cli_a.id)
    b_links = await list_links_for_cli(db_session, cli_principal_id=cli_b.id)

    assert len(a_links) == 1
    assert len(b_links) == 0, "cli B never linked to anyone"


async def test_delete_principal_link_removes_only_named_pair_when_multi_linked(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    cli_a = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="a")
    cli_b = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="b")
    plat = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="x"
    )
    await create_principal_link(
        db_session, cli_principal_id=cli_a.id, platform_principal_id=plat.id
    )
    await create_principal_link(
        db_session, cli_principal_id=cli_b.id, platform_principal_id=plat.id
    )

    await delete_principal_link(
        db_session, cli_principal_id=cli_a.id, platform_principal_id=plat.id
    )

    assert await list_links_for_cli(db_session, cli_principal_id=cli_a.id) == []
    b_links = await list_links_for_cli(db_session, cli_principal_id=cli_b.id)
    assert len(b_links) == 1, "cli B's link to the same platform must survive"


async def test_delete_principal_link_raises_when_pair_missing(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(StoreError):
        await delete_principal_link(
            db_session,
            cli_principal_id=uuid.uuid4(),
            platform_principal_id=uuid.uuid4(),
        )


async def test_resolve_linked_platform_principal_returns_row_when_link_exists(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    cli = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="op")
    plat = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="999"
    )
    await create_principal_link(db_session, cli_principal_id=cli.id, platform_principal_id=plat.id)

    resolved = await resolve_linked_platform_principal(
        db_session,
        cli_principal_id=cli.id,
        platform="discord",
        external_id="999",
    )
    assert resolved is not None
    assert resolved.id == plat.id


async def test_resolve_linked_platform_principal_returns_none_when_unlinked(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    cli = await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="op")
    await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="999"
    )
    # No link created.

    resolved = await resolve_linked_platform_principal(
        db_session,
        cli_principal_id=cli.id,
        platform="discord",
        external_id="999",
    )
    assert resolved is None, "unlinked platform principal must not be resolvable"


# ---------------------------------------------------------------------------
# delete_for_principal — idempotent DELETE-by-id, never raises on rowcount=0
# ---------------------------------------------------------------------------


async def test_delete_for_principal_returns_rowcount_when_cli_principal_exists(
    db_session: AsyncSession,
) -> None:
    cli = await make_cli_principal(db_session, os_user="alice")
    rowcount = await delete_for_principal(db_session, principal_id=cli.id, kind="cli")
    assert rowcount == 1, "deleting an existing cli principal returns rowcount=1"
    again = await list_cli_principals_for_account(db_session, account_id=cli.account_id)
    assert again == [], "principal row must be gone"


async def test_delete_for_principal_returns_rowcount_when_platform_principal_exists(
    db_session: AsyncSession,
) -> None:
    plat = await make_platform_principal(db_session, platform="discord", external_id="x")
    rowcount = await delete_for_principal(db_session, principal_id=plat.id, kind="platform")
    assert rowcount == 1, "deleting an existing platform principal returns rowcount=1"


async def test_delete_for_principal_returns_zero_when_id_missing_does_not_raise(
    db_session: AsyncSession,
) -> None:
    rowcount = await delete_for_principal(db_session, principal_id=uuid.uuid4(), kind="cli")
    assert rowcount == 0, "missing id must return 0, not raise (idempotency contract)"


# ---------------------------------------------------------------------------
# delete_principal_links_for_principal — bulk delete by side, no raise on 0
# ---------------------------------------------------------------------------


async def test_delete_principal_links_for_principal_deletes_both_directions_for_cli(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    cli = await make_cli_principal(db_session, os_user="op", tenant=tenant)
    plat = await make_platform_principal(
        db_session, platform="discord", external_id="x", tenant=tenant
    )
    await link_principals(db_session, cli=cli, platform=plat)

    rowcount = await delete_principal_links_for_principal(
        db_session, principal_id=cli.id, kind="cli"
    )
    assert rowcount == 1, "link from cli side must be deleted"
    assert await list_links_for_cli(db_session, cli_principal_id=cli.id) == []


async def test_delete_principal_links_for_principal_deletes_for_platform(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    cli = await make_cli_principal(db_session, os_user="op", tenant=tenant)
    plat = await make_platform_principal(
        db_session, platform="discord", external_id="x", tenant=tenant
    )
    await link_principals(db_session, cli=cli, platform=plat)

    rowcount = await delete_principal_links_for_principal(
        db_session, principal_id=plat.id, kind="platform"
    )
    assert rowcount == 1, "link from platform side must be deleted"


async def test_delete_principal_links_for_principal_returns_zero_when_no_links(
    db_session: AsyncSession,
) -> None:
    rowcount = await delete_principal_links_for_principal(
        db_session, principal_id=uuid.uuid4(), kind="cli"
    )
    assert rowcount == 0, "no links must return 0, not raise"


# ---------------------------------------------------------------------------
# list_*_principals_for_account — Pydantic row enumeration
# ---------------------------------------------------------------------------


async def test_list_cli_principals_for_account_returns_all_cli_rows_for_account(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli_a = await make_cli_principal(db_session, os_user="a", tenant=tenant, account=account)
    cli_b = await make_cli_principal(db_session, os_user="b", tenant=tenant, account=account)
    # An unrelated principal in a different account should not be returned.
    await make_cli_principal(db_session, os_user="other", tenant=tenant)

    rows = await list_cli_principals_for_account(db_session, account_id=account.id)
    ids = {r.id for r in rows}
    assert ids == {cli_a.id, cli_b.id}, "list must return all cli principals for the account"


async def test_list_platform_principals_for_account_returns_all_platform_rows_for_account(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    p1 = await make_platform_principal(
        db_session, platform="discord", external_id="1", tenant=tenant, account=account
    )
    p2 = await make_platform_principal(
        db_session, platform="discord", external_id="2", tenant=tenant, account=account
    )
    await make_platform_principal(
        db_session, platform="discord", external_id="other", tenant=tenant
    )

    rows = await list_platform_principals_for_account(db_session, account_id=account.id)
    ids = {r.id for r in rows}
    assert ids == {p1.id, p2.id}, "list must return all platform principals for the account"


async def test_list_cli_principals_for_account_returns_empty_list_when_no_principals(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    rows = await list_cli_principals_for_account(db_session, account_id=account.id)
    assert rows == [], "account with no cli principals returns []"


async def test_list_platform_principals_for_account_returns_empty_list_when_no_principals(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    rows = await list_platform_principals_for_account(db_session, account_id=account.id)
    assert rows == [], "account with no platform principals returns []"


# ---------------------------------------------------------------------------
# get_discord_principal_for_account — helper
# ---------------------------------------------------------------------------


async def test_get_discord_principal_for_account_returns_external_id(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="1234567890",
        tenant=tenant,
        account=account,
    )

    result = await get_discord_principal_for_account(db_session, account_id=account.id)

    assert result == "1234567890", "helper should return the discord external_id for the account"


async def test_get_discord_principal_for_account_returns_none_when_no_principal(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)

    result = await get_discord_principal_for_account(db_session, account_id=account.id)

    assert result is None, "account with no platform principal returns None, not raise"


async def test_get_discord_principal_for_account_ignores_non_discord_principals(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    # Seed a non-discord platform principal — the WHERE filter must reject it.
    await make_platform_principal(
        db_session,
        platform="cli",
        external_id="cli-user-1",
        tenant=tenant,
        account=account,
    )

    result = await get_discord_principal_for_account(db_session, account_id=account.id)

    assert result is None, "filter is platform='discord', not 'any non-null platform'"


# ---------------------------------------------------------------------------
# find_platform_principal — read-only lookup; returns None on miss, never creates
# ---------------------------------------------------------------------------


async def test_find_platform_principal_returns_principal_when_one_exists(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    seeded = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="42"
    )
    result = await find_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="42"
    )
    assert result is not None, "existing principal must be found"
    assert result.id == seeded.id, "find must return the same principal row"
    assert result.account_id == seeded.account_id, "account_id must round-trip"


async def test_find_platform_principal_returns_none_when_no_principal_exists(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    result = await find_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="999"
    )
    assert result is None, "miss must return None, not raise"


async def test_find_platform_principal_does_not_create_row_when_miss(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    count_stmt = select(func.count()).select_from(PlatformPrincipal)
    before = (await db_session.execute(count_stmt)).scalar_one()

    result = await find_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="never-seen"
    )
    after = (await db_session.execute(count_stmt)).scalar_one()

    assert result is None, "miss must return None"
    assert before == after, (
        "find_platform_principal must NOT insert a row on miss "
        "(this is the whole reason it exists vs get_or_create)"
    )


async def test_find_platform_principal_does_not_return_principal_from_other_tenant(
    db_session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    await get_or_create_platform_principal(
        db_session, tenant_id=tenant_a.id, platform="discord", external_id="shared-id"
    )

    result = await find_platform_principal(
        db_session, tenant_id=tenant_b.id, platform="discord", external_id="shared-id"
    )
    assert result is None, "cross-tenant lookup must return None — tenant_id is in the WHERE clause"


async def test_find_platform_principal_matches_external_id_exactly(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="123456"
    )

    result = await find_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="123456 "
    )
    assert result is None, "external_id matching is exact string equality, no normalization"


# ---------------------------------------------------------------------------
# count_principal_links_for_principal — read-only mirror of the delete helper
# ---------------------------------------------------------------------------


async def test_count_principal_links_for_principal_returns_zero_when_no_links(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    cli = await make_cli_principal(db_session, os_user="lonely", tenant=tenant)

    count = await count_principal_links_for_principal(db_session, principal_id=cli.id, kind="cli")
    assert count == 0, "no links seeded -> count must be 0"


async def test_count_principal_links_for_principal_counts_rows_when_present(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli = await make_cli_principal(db_session, os_user="linked", tenant=tenant, account=account)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="ext-linked",
        tenant=tenant,
        account=account,
    )
    await link_principals(db_session, cli=cli, platform=pp)

    cli_count = await count_principal_links_for_principal(
        db_session, principal_id=cli.id, kind="cli"
    )
    platform_count = await count_principal_links_for_principal(
        db_session, principal_id=pp.id, kind="platform"
    )
    assert cli_count == 1, "cli-side count must include the seeded link"
    assert platform_count == 1, "platform-side count must include the seeded link"


# ---------------------------------------------------------------------------
# Migration 0013 — active_agent_name + propagation mode columns
# ---------------------------------------------------------------------------


async def test_migration_0013_adds_active_agent_name_column(
    db_session: AsyncSession,
) -> None:
    # If the column is missing, asyncpg raises UndefinedColumnError wrapped as
    # ProgrammingError — letting the assertion fail is the point.
    await db_session.execute(text("SELECT active_agent_name FROM platform_principals LIMIT 0"))


async def test_migration_0013_adds_mode_columns_with_default(
    db_session: AsyncSession,
) -> None:
    """channel_config.mode has server_default='agent'; ORM insert must see it."""
    tenant = await make_tenant(db_session)
    cfg = ChannelConfig(tenant_id=tenant.id, channel_id="c1")
    db_session.add(cfg)
    await db_session.flush()

    row = (
        await db_session.execute(
            text("SELECT mode FROM channel_config WHERE tenant_id=:tid AND channel_id='c1'"),
            {"tid": tenant.id},
        )
    ).scalar_one()
    assert row == "agent", "server_default must populate mode='agent' on insert"


async def test_migration_0013_mode_check_constraint_rejects_invalid_value(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO channel_config "
                "(tenant_id, channel_id, mode) "
                "VALUES (:tid, 'c1', 'garbage')"
            ),
            {"tid": tenant.id},
        )
        await db_session.flush()


# ---------------------------------------------------------------------------
# set_active_agent_name — per-principal active agent
# ---------------------------------------------------------------------------


async def test_set_active_agent_name_sets_and_reads_back(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u1"
    )

    await set_active_agent_name(db_session, principal_id=principal.id, agent_name="research-bot")

    refetched = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u1"
    )
    assert refetched.active_agent_name == "research-bot", "write must be readable on refetch"


async def test_set_active_agent_name_clears_to_none(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u2"
    )
    await set_active_agent_name(db_session, principal_id=principal.id, agent_name="some-agent")
    await set_active_agent_name(db_session, principal_id=principal.id, agent_name=None)

    refetched = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u2"
    )
    assert refetched.active_agent_name is None, "passing None must clear the column"


async def test_set_active_agent_name_normalizes_empty_string(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u3"
    )
    await set_active_agent_name(db_session, principal_id=principal.id, agent_name="")

    refetched = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u3"
    )
    assert refetched.active_agent_name is None, "empty string is normalized to None"


async def test_set_active_agent_name_noop_on_missing_principal(
    db_session: AsyncSession,
) -> None:
    # No assertion on DB state — contract is "doesn't raise".
    await set_active_agent_name(db_session, principal_id=uuid.uuid4(), agent_name="whatever")


async def test_set_active_agent_name_idempotent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u4"
    )
    await set_active_agent_name(db_session, principal_id=principal.id, agent_name="same")
    await set_active_agent_name(db_session, principal_id=principal.id, agent_name="same")

    refetched = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="u4"
    )
    assert refetched.active_agent_name == "same", "second identical write must not corrupt state"


# ---------------------------------------------------------------------------
# Slack principal round-trip
# ---------------------------------------------------------------------------


async def test_get_or_create_platform_principal_slack_round_trips(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T123")
    p = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="T123"
    )
    assert p.platform == "slack", (
        "PlatformPrincipalRow.platform must validate 'slack' — "
        "this is the runtime model_validate gate"
    )
    assert p.external_id == "T123", "external_id must round-trip (same team_id as workspace_id)"
    assert p.tenant_id == tenant.id, "tenant_id must round-trip"

    # Idempotency: second call returns the same row.
    again = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="T123"
    )
    assert again.id == p.id, (
        "get_or_create must be idempotent — same (tenant, platform, external_id) returns same row"
    )


# ---------------------------------------------------------------------------
# get_slack_principal_for_account — audit-display resolver helper
# ---------------------------------------------------------------------------


async def test_get_slack_principal_for_account_returns_external_id_when_slack_principal_exists(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T456")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U07ABC123",
        tenant=tenant,
        account=account,
    )

    result = await get_slack_principal_for_account(db_session, account_id=account.id)

    assert result == "U07ABC123", (
        "helper should return the Slack external_id for the account on a hit"
    )


async def test_get_slack_principal_for_account_returns_none_when_no_slack_principal(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)

    result = await get_slack_principal_for_account(db_session, account_id=account.id)

    assert result is None, (
        "account with no Slack platform principal must return None — resolver falls back to 'account {first8}'"
    )


async def test_get_slack_principal_for_account_ignores_non_slack_principals(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    # Seed a discord principal — the WHERE filter must reject it.
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="discord-user-1",
        tenant=tenant,
        account=account,
    )

    result = await get_slack_principal_for_account(db_session, account_id=account.id)

    assert result is None, (
        "filter is platform='slack'; a discord principal for the same account must not be returned"
    )
