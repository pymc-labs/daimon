"""Who an admin message reaches: never an account held as from another organisation."""

from __future__ import annotations

import uuid

from daimon.core.channel_admins import channel_admin_user_ids, load_stored_subject
from daimon.core.stores.accounts import (
    list_external_platform_user_ids,
    list_platform_user_ids,
    set_external,
    set_role,
)
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_external_accounts_are_filtered_from_admin_recipients(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    ids: dict[str, uuid.UUID] = {}
    for user in ("ours", "theirs", "admin"):
        account = await make_account(db_session, tenant=tenant)
        await make_platform_principal(
            db_session, platform="teams", external_id=user, tenant=tenant, account=account
        )
        ids[user] = account.id
    await set_role(db_session, ids["admin"], Role.ADMIN)
    await set_role(db_session, ids["theirs"], Role.ADMIN)
    await set_external(db_session, ids["theirs"], True)
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        channel_id="c1",
        role_ids=(),
        user_ids=("ours", "theirs", "never-spoke"),
        actor_account_id=None,
    )
    admins = await list_platform_user_ids(
        db_session, tenant_id=tenant.id, platform="teams", limit=10, admins=True
    )
    assert admins == ["admin"], "demoted when held, and filtered either way"
    held = await list_external_platform_user_ids(
        db_session, tenant_id=tenant.id, platform="teams", user_ids=("ours", "theirs")
    )
    assert held == {"theirs"}
    recipients = await channel_admin_user_ids(
        db_session_factory, tenant_id=tenant.id, platform="teams", channel_id="c1", limit=10
    )
    assert recipients == ["never-spoke", "ours"], "a granted external hears nothing"
    subject = await load_stored_subject(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        account_id=ids["theirs"],
        platform_user_id="theirs",
    )
    assert not subject.administered_channel_ids and not subject.is_admin
