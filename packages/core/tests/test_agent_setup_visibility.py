"""Who may read an agent's setup (prompt, connector URLs): an admin, or its owner."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.agent_ownership import may_read_agent_setup, setup_hidden
from daimon.core.defaults.metadata import MA_METADATA_KEY_SETUP_HIDDEN
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_owners
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AGENT = "ag_notes"


async def _owned(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session, platform="discord")
        owner = await make_account(session, tenant=tenant)
        member = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform="discord", external_id="owner-user", tenant=tenant, account=owner
        )
        await agent_owners.set_owner(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT),
            owner_account_id=owner.id,
            source="created",
        )
    return tenant.id, owner.id, member.id


@pytest.mark.parametrize(
    ("viewer", "hidden", "expected"),
    [
        ("admin", False, True),
        ("admin", True, True),
        ("owner_account", False, True),
        ("owner_user", False, True),
        ("owner_account", True, False),  # a member's copy: inherited setup stays an admin's
        ("member", False, False),
        ("nobody", False, False),
    ],
)
async def test_only_an_admin_or_the_owner_reads_an_agents_setup(
    db_session_factory: async_sessionmaker[AsyncSession],
    viewer: str,
    hidden: bool,
    expected: bool,
) -> None:
    tenant_id, owner_id, member_id = await _owned(db_session_factory)
    metadata = {MA_METADATA_KEY_SETUP_HIDDEN: "true"} if hidden else {}
    assert setup_hidden(metadata) is hidden
    identity: dict[str, object] = {
        "admin": {"is_admin": True, "account_id": member_id},
        "owner_account": {"is_admin": False, "account_id": owner_id},
        "owner_user": {"is_admin": False, "platform": "discord", "platform_user_id": "owner-user"},
        "member": {"is_admin": False, "account_id": member_id},
        "nobody": {"is_admin": False},
    }[viewer]  # type: ignore[assignment]
    async with db_session_factory() as session:
        sees = await may_read_agent_setup(
            session, tenant_id=tenant_id, ma_agent_id=AGENT, metadata=metadata, **identity
        )
    assert sees is expected
