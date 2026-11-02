"""One agent's coding keys: who sees them, who revokes them, and what a listing shows."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from daimon.core.agent_ownership import account_owns_agent
from daimon.core.coding_keys import (
    CodingKeyAccess,
    coding_key_access,
    list_coding_keys,
    revoke_coding_key,
)
from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti
from daimon.core.stores.agent_owners import set_owner
from daimon.core.stores.identity import find_platform_principal, mark_principal_foreign
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.stores.platform_names import KnownName, upsert_user_names
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

NOW = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.UTC)
SECRET = b"k" * 32


@pytest.mark.parametrize(
    ("is_admin", "owns", "expected"),
    [(True, False, "admin"), (True, True, "admin"), (False, True, "owner"), (False, False, None)],
)
def test_access_is_all_keys_for_an_admin_own_keys_for_an_owner_nothing_for_a_member(
    is_admin: bool, owns: bool, expected: CodingKeyAccess | None
) -> None:
    assert coding_key_access(is_admin=is_admin, owns_agent=owns) == expected


class _World:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> None:
        db = self.db
        self.tenant = await make_tenant(db, platform="discord")
        self.agent = uuid.uuid4()
        self.other_agent = uuid.uuid4()
        self.owner = await make_account(db, tenant=self.tenant)
        self.admin = await make_account(db, tenant=self.tenant)
        self.member = await make_account(db, tenant=self.tenant)
        for name, account in (("owner", self.owner), ("admin", self.admin)):
            await make_platform_principal(
                db, platform="discord", external_id=f"u-{name}", tenant=self.tenant, account=account
            )
        await upsert_user_names(
            db,
            tenant_id=self.tenant.id,
            platform="discord",
            names={"u-owner": KnownName(display_name="Olive Owner")},
        )
        await set_owner(
            db,
            tenant_id=self.tenant.id,
            agent_id=self.agent,
            owner_account_id=self.owner.id,
            source="admin_assigned",
        )

    async def key(
        self, account_id: uuid.UUID, *, agent: uuid.UUID | None = None, **kw: object
    ) -> uuid.UUID:
        token = await mint_agent_mcp_token(
            self.db,
            account_id=account_id,
            tenant_id=self.tenant.id,
            agent_id=agent or self.agent,
            label="a",
            secret=SECRET,
            now=NOW,
            **kw,  # pyright: ignore[reportArgumentType]
        )
        return token_jti(token)


async def test_owner_lists_only_their_own_keys_and_admin_lists_every_key(
    db_session: AsyncSession,
) -> None:
    world = _World(db_session)
    await world.build()
    mine = await world.key(world.owner.id, ttl_days=30)
    admins = await world.key(world.admin.id, ttl_days=90)
    await world.key(world.owner.id, agent=world.other_agent)  # another agent's key
    await world.key(world.owner.id, purpose="report")  # not a coding key
    common = {
        "tenant_id": world.tenant.id,
        "agent_id": world.agent,
        "platform": "discord",
        "now": NOW,
    }

    owner_view = await list_coding_keys(
        db_session, access="owner", viewer_account_id=world.owner.id, **common
    )
    admin_view = await list_coding_keys(
        db_session, access="admin", viewer_account_id=world.admin.id, **common
    )

    assert [entry.jti for entry in owner_view] == [mine], "an owner sees only their own keys"
    assert {entry.jti for entry in admin_view} == {mine, admins}, "an admin sees every key"
    by_jti = {entry.jti: entry for entry in admin_view}
    assert by_jti[mine].creator == "Olive Owner" and not by_jti[mine].mine
    assert by_jti[admins].creator == "u-admin" and by_jti[admins].mine
    assert by_jti[mine].expires_at == NOW + dt.timedelta(days=30)
    assert not by_jti[mine].bound


async def test_a_listing_never_carries_the_token_and_hides_revoked_and_expired_keys(
    db_session: AsyncSession,
) -> None:
    world = _World(db_session)
    await world.build()
    live = await world.key(world.owner.id)
    revoked = await world.key(world.owner.id)
    await world.key(world.owner.id, ttl_days=1)  # expires before the listing's clock
    assert await revoke_coding_key(
        db_session,
        jti=revoked,
        tenant_id=world.tenant.id,
        agent_id=world.agent,
        access="owner",
        viewer_account_id=world.owner.id,
        now=NOW,
    )

    entries = await list_coding_keys(
        db_session,
        tenant_id=world.tenant.id,
        agent_id=world.agent,
        access="owner",
        viewer_account_id=world.owner.id,
        platform="discord",
        now=NOW + dt.timedelta(days=2),
    )

    assert [entry.jti for entry in entries] == [live]
    assert {field for field in vars(entries[0])} == {
        "jti",
        "creator",
        "created_at",
        "expires_at",
        "bound",
        "mine",
    }, "only what tells keys apart: no token field"


async def test_an_owner_revokes_their_own_key_only_and_an_admin_any_key_of_the_agent(
    db_session: AsyncSession,
) -> None:
    world = _World(db_session)
    await world.build()
    mine = await world.key(world.owner.id)
    admins = await world.key(world.admin.id)
    elsewhere = await world.key(world.owner.id, agent=world.other_agent)
    report = await world.key(world.owner.id, purpose="report")

    async def revoke(jti: uuid.UUID, access: CodingKeyAccess, viewer: uuid.UUID) -> bool:
        return await revoke_coding_key(
            db_session,
            jti=jti,
            tenant_id=world.tenant.id,
            agent_id=world.agent,
            access=access,
            viewer_account_id=viewer,
            now=NOW,
        )

    assert not await revoke(admins, "owner", world.owner.id), "an owner cannot revoke another's"
    assert not await revoke(elsewhere, "admin", world.admin.id), "not another agent's key"
    assert not await revoke(report, "admin", world.admin.id), "not a report token"
    assert await revoke(mine, "owner", world.owner.id)
    assert not await revoke(mine, "owner", world.owner.id), "already revoked"
    assert await revoke(admins, "admin", world.admin.id)

    for jti, revoked in ((mine, True), (admins, True), (elsewhere, False), (report, False)):
        row = await get_mcp_token(db_session, jti=jti)
        assert row is not None and (row.revoked_at is not None) is revoked


async def test_account_owns_agent_follows_the_owner_row_and_never_a_foreign_principal(
    db_session: AsyncSession,
) -> None:
    world = _World(db_session)
    await world.build()
    args = {"tenant_id": world.tenant.id, "agent_id": world.agent}

    assert await account_owns_agent(db_session, account_id=world.owner.id, **args)
    assert not await account_owns_agent(db_session, account_id=world.member.id, **args)

    await set_owner(
        db_session,
        tenant_id=world.tenant.id,
        agent_id=world.agent,
        owner_account_id=world.member.id,
        source="admin_assigned",
    )
    assert not await account_owns_agent(db_session, account_id=world.owner.id, **args), (
        "removing ownership is seen on the next read"
    )
    assert await account_owns_agent(db_session, account_id=world.member.id, **args)


async def test_a_foreign_principal_never_owns_an_agent(db_session: AsyncSession) -> None:
    world = _World(db_session)
    await world.build()
    principal = await find_platform_principal(
        db_session, tenant_id=world.tenant.id, platform="discord", external_id="u-owner"
    )
    assert principal is not None
    await mark_principal_foreign(db_session, principal_id=principal.id, home_id="T_OTHER")

    assert not await account_owns_agent(
        db_session, tenant_id=world.tenant.id, agent_id=world.agent, account_id=world.owner.id
    )
