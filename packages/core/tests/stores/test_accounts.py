"""Tests for daimon.core.stores.accounts.get_account and get_account_with_tenant."""

from __future__ import annotations

import uuid

import pytest
from daimon.core._models import Account, UserConfig
from daimon.core.stores.accounts import (
    account_exists,
    count_user_config_for_account,
    delete_account,
    delete_user_config_for_account,
    get_account,
    get_account_with_tenant,
    list_platform_user_ids,
    set_platform_role_ids,
)
from daimon.core.stores.domain import AccountIdentityRow, AccountRow, Platform
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_get_account_returns_row_when_present(db_session: AsyncSession) -> None:
    account = await make_account(db_session)

    row = await get_account(db_session, account.id)

    assert isinstance(row, AccountRow), "store should map ORM -> Pydantic"
    assert row.id == account.id
    assert row.tenant_id == account.tenant_id


async def test_get_account_returns_none_when_absent(db_session: AsyncSession) -> None:
    row = await get_account(db_session, uuid.uuid4())
    assert row is None, "missing row should be None, not an exception"


# ---------------------------------------------------------------------------
# delete_account — purge primitive, idempotent
# ---------------------------------------------------------------------------


async def test_delete_account_removes_account_row_and_returns_one(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    rowcount = await delete_account(db_session, account_id=account.id)
    assert rowcount == 1, "deleting an existing account returns rowcount=1"
    assert await get_account(db_session, account.id) is None, "account row must be gone"


async def test_delete_account_returns_zero_when_account_missing(
    db_session: AsyncSession,
) -> None:
    rowcount = await delete_account(db_session, account_id=uuid.uuid4())
    assert rowcount == 0, "missing account must return 0, not raise"


# ---------------------------------------------------------------------------
# delete_user_config_for_account — purge primitive, idempotent
# ---------------------------------------------------------------------------


async def test_delete_user_config_for_account_removes_config_row(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    cfg = UserConfig(account_id=account.id, agent_name="a", environment_name="e")
    db_session.add(cfg)
    await db_session.flush()

    rowcount = await delete_user_config_for_account(db_session, account_id=account.id)
    assert rowcount == 1, "deleting existing user_config returns rowcount=1"


async def test_delete_user_config_for_account_returns_zero_when_no_config(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    rowcount = await delete_user_config_for_account(db_session, account_id=account.id)
    assert rowcount == 0, "no user_config row must return 0, not raise"


# ---------------------------------------------------------------------------
# account_exists — read-only mirror of delete_account
# ---------------------------------------------------------------------------


async def test_account_exists_returns_false_when_account_missing(
    db_session: AsyncSession,
) -> None:
    assert await account_exists(db_session, account_id=uuid.uuid4()) is False, (
        "no account row -> account_exists must be False"
    )


async def test_account_exists_returns_true_when_account_present(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    assert await account_exists(db_session, account_id=account.id) is True, (
        "seeded account row -> account_exists must be True"
    )


# ---------------------------------------------------------------------------
# count_user_config_for_account — read-only mirror of delete_user_config_for_account
# ---------------------------------------------------------------------------


async def test_count_user_config_for_account_returns_zero_when_no_config(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    count = await count_user_config_for_account(db_session, account_id=account.id)
    assert count == 0, "no user_config seeded -> count must be 0"


async def test_count_user_config_for_account_counts_row_when_present(
    db_session: AsyncSession,
) -> None:
    account = await make_account(db_session)
    db_session.add(UserConfig(account_id=account.id, agent_name="a", environment_name="e"))
    await db_session.flush()

    count = await count_user_config_for_account(db_session, account_id=account.id)
    assert count == 1, "one user_config row seeded -> count must be 1"


# ---------------------------------------------------------------------------
# get_account_with_tenant — three-table JOIN, single round-trip
# ---------------------------------------------------------------------------


async def test_get_account_with_tenant_returns_identity_for_discord_account(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="guild-999")
    account = Account(tenant_id=tenant.id, role="admin")
    db_session.add(account)
    await db_session.flush()
    await make_platform_principal(
        db_session, platform="discord", external_id="user-77", tenant=tenant, account=account
    )
    await db_session.flush()

    row = await get_account_with_tenant(db_session, account_id=account.id)

    assert row is not None, "should find the account"
    assert isinstance(row, AccountIdentityRow), "store should return AccountIdentityRow"
    assert row.account_id == account.id, "account_id must match"
    assert row.tenant_id == tenant.id, "tenant_id must match"
    assert row.platform == "discord", "platform must come from tenant"
    assert row.external_id == "guild-999", "external_id must be tenant's workspace_id snowflake"
    assert row.platform_user_id == "user-77", "platform_user_id must come from PlatformPrincipal"


@pytest.mark.parametrize(
    ("platform", "workspace_id", "user_id"),
    [
        ("slack", "T_WS_1", "U_SLACK_1"),
        # Teams: the Entra directory tenant and the user's Entra object id.
        ("teams", "0f9e8d7c-0000-4000-8000-000000000001", "a1b2c3d4-0000-4000-8000-000000000002"),
    ],
)
async def test_get_account_with_tenant_returns_identity_for_chat_accounts(
    db_session: AsyncSession, platform: Platform, workspace_id: str, user_id: str
) -> None:
    tenant = await make_tenant(db_session, platform=platform, workspace_id=workspace_id)
    account = Account(tenant_id=tenant.id, role="user")
    db_session.add(account)
    await db_session.flush()
    await make_platform_principal(
        db_session, platform=platform, external_id=user_id, tenant=tenant, account=account
    )
    await db_session.flush()

    row = await get_account_with_tenant(db_session, account_id=account.id)

    assert row is not None
    assert (row.platform, row.external_id) == (platform, workspace_id), "from the tenant"
    assert row.platform_user_id == user_id, "must resolve for every chat platform, not only discord"


async def test_get_account_with_tenant_ignores_principal_of_different_platform(
    db_session: AsyncSession,
) -> None:
    """The join is keyed on Tenant.platform, so a principal whose platform differs
    from the tenant's is not matched (guards against an unconditional join)."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="guild-222")
    account = Account(tenant_id=tenant.id, role="user")
    db_session.add(account)
    await db_session.flush()
    # A slack principal hung off a discord tenant's account — must NOT resolve.
    await make_platform_principal(
        db_session, platform="slack", external_id="U_SLACK_X", tenant=tenant, account=account
    )
    await db_session.flush()

    row = await get_account_with_tenant(db_session, account_id=account.id)

    assert row is not None, "should still find the account"
    assert row.platform_user_id is None, (
        "a principal whose platform != tenant.platform must not resolve platform_user_id"
    )


async def test_get_account_with_tenant_returns_none_platform_user_id_without_principal(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="guild-111")
    account = Account(tenant_id=tenant.id, role="user")
    db_session.add(account)
    await db_session.flush()
    # No PlatformPrincipal added

    row = await get_account_with_tenant(db_session, account_id=account.id)

    assert row is not None, "should find the account"
    assert row.platform_user_id is None, "LEFT JOIN must yield None when no principal exists"


async def test_get_account_with_tenant_returns_none_for_missing_account(
    db_session: AsyncSession,
) -> None:
    row = await get_account_with_tenant(db_session, account_id=uuid.uuid4())
    assert row is None, "must return None when account does not exist"


async def test_list_platform_user_ids_keeps_only_among_and_folds_case_on_request(
    db_session: AsyncSession,
) -> None:
    """`among` bounds a group grant's recipients to its live members, inside the limit."""
    tenant = await make_tenant(db_session, platform="teams", workspace_id="entra-among")
    for external_id in ("0-left1", "0-left2", "C-STAYED"):
        account = await make_account(db_session, tenant=tenant)
        await make_platform_principal(
            db_session, platform="teams", external_id=external_id, tenant=tenant, account=account
        )
        await set_platform_role_ids(db_session, account.id, ["team"])

    async def listed(among: set[str] | None, *, fold_case: bool, limit: int | None) -> list[str]:
        return await list_platform_user_ids(
            db_session,
            tenant_id=tenant.id,
            platform="teams",
            limit=limit,
            role_ids=["team"],
            among=among,
            fold_case=fold_case,
        )

    live = {"c-stayed"}
    assert await listed(None, fold_case=False, limit=1) == ["0-left1"], "unbounded, left first"
    assert await listed(live, fold_case=False, limit=None) == [], "exact unless folded"
    assert await listed(live, fold_case=True, limit=1) == ["C-STAYED"], (
        "case folded, and members who left do not fill the limit"
    )
    assert await listed(set(), fold_case=True, limit=None) == [], "no live member, nobody"
