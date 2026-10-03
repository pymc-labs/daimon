"""Panel writes leave a `panel:<op>` audit row, naming only clickers with an account."""

import uuid

from daimon.core.panel_audit import record_panel_write
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_panel_write_is_audited_with_the_clickers_account(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    principal = await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant
    )
    await db_session.commit()
    jti = uuid.uuid4()

    await record_panel_write(
        db_session_factory,
        tenant_id=tenant.id,
        platform="slack",
        platform_user_id="U1",
        op="coding_token_revoke",
        outcome="denied",
        reason="not_minter",
        token_kind="agent",
        token_jti=jti,
    )

    (row,) = await list_events(db_session, tenant_id=tenant.id)
    assert (row.tool_name, row.operation, row.outcome, row.reason) == (
        "panel:coding_token_revoke",
        "coding_token_revoke",
        "denied",
        "not_minter",
    )
    assert (row.account_id, row.platform_user_id) == (principal.account_id, "U1")
    assert (row.token_kind, row.token_jti) == ("agent", jti)


async def test_a_clicker_with_no_account_is_not_named(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    await record_panel_write(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        platform_user_id="42",
        op="isolation",
        outcome="denied",
        reason="needs_admin",
    )

    (row,) = await list_events(db_session, tenant_id=tenant.id)
    assert row.tool_name == "panel:isolation"
    assert (row.account_id, row.platform_user_id) == (None, None), (
        "a row erasure can never reach must not name anyone"
    )
