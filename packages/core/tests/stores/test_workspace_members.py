"""The member list of a chat workspace, and a person's standing against it."""

from __future__ import annotations

from datetime import UTC, datetime

from daimon.core.stores import workspace_members as members_store
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 5, 1, tzinfo=UTC)


async def test_replace_members_swaps_the_whole_list_and_marks_it_loaded(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="111111111111111111")
    await members_store.upsert_member(
        db_session, tenant_id=tenant.id, platform_user_id="gone", is_admin=False, now=_NOW
    )
    assert await members_store.list_synced(db_session, tenant_ids=[tenant.id]) == {}, (
        "an event-only list is not a full one"
    )

    await members_store.replace_members(
        db_session,
        tenant_id=tenant.id,
        members={"a": True, "b": False},
        workspace_name="Guild",
        now=_NOW,
    )

    for user_id, expected in (("a", True), ("b", True), ("gone", False)):
        found = await members_store.list_member_tenant_ids(
            db_session, platform_user_id=user_id, tenant_ids=[tenant.id]
        )
        assert (tenant.id in found) is expected, f"{user_id} membership after the reload"
    assert await members_store.list_synced(db_session, tenant_ids=[tenant.id]) == {
        tenant.id: "Guild"
    }, "the reload marks the list loaded, with the workspace's name"


async def test_standing_follows_join_leave_and_admin_flags(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, workspace_id="222222222222222222")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant, account=account
    )
    key = (tenant.id, account.id)

    unsynced = (await members_store.list_chat_standings(db_session, people=[key]))[key]
    assert unsynced.admitted and not unsynced.synced, "an unloaded list admits on the sign-in"

    await members_store.replace_members(
        db_session, tenant_id=tenant.id, members={}, workspace_name="G", now=_NOW
    )
    gone = (await members_store.list_chat_standings(db_session, people=[key]))[key]
    assert not gone.admitted, "a loaded list without the account refuses it"

    await members_store.upsert_member(
        db_session, tenant_id=tenant.id, platform_user_id="u1", is_admin=False, now=_NOW
    )
    await members_store.update_admin_flags(
        db_session, tenant_id=tenant.id, flags={"u1": True, "stranger": True}, now=_NOW
    )
    back = (await members_store.list_chat_standings(db_session, people=[key]))[key]
    assert back.admitted and back.is_admin, "a join admits, and a role update sets admin"
    assert not await members_store.list_member_tenant_ids(
        db_session, platform_user_id="stranger", tenant_ids=[tenant.id]
    ), "a flag update never adds a member"

    await members_store.delete_member(db_session, tenant_id=tenant.id, platform_user_id="u1")
    left = (await members_store.list_chat_standings(db_session, people=[key]))[key]
    assert not left.admitted, "leaving refuses at once"

    await members_store.clear_members(db_session, tenant_id=tenant.id)
    cleared = (await members_store.list_chat_standings(db_session, people=[key]))[key]
    assert not cleared.synced, "an ended install forgets its list"


async def test_standing_skips_accounts_with_no_chat_account_there(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, workspace_id="333333333333333333")
    web_only = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="web", external_id="p1", tenant=tenant, account=web_only
    )

    standings = await members_store.list_chat_standings(
        db_session, people=[(tenant.id, web_only.id)]
    )

    assert standings == {}, "a web-only person has no chat standing to check"
