"""Panel operator tokens: tenant scopes only, and revoke stays inside the tenant."""

import datetime as dt
import uuid

import pytest
from daimon.core.mcp_auth import mint_agent_mcp_token, mint_operator_mcp_token, token_jti
from daimon.core.operator_tokens import OperatorTokenError
from daimon.core.panel_operator_tokens import (
    PANEL_SCOPES,
    list_panel_operator_tokens,
    mint_panel_operator_token,
    revoke_panel_operator_token,
)
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import Role
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

NOW = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


async def _mint(session: AsyncSession, tenant_id: uuid.UUID, scopes: list[str]):
    return await mint_panel_operator_token(
        session,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id="U1",
        scopes=scopes,
        label=" ci ",
        secret=b"s" * 48,
        now=NOW,
    )


def test_the_panel_never_offers_the_deployment_scope() -> None:
    assert PANEL_SCOPES == ("tenant:read", "channels:write", "agents:archive", "promo:redeem")


async def test_mint_stores_the_admin_role_and_refuses_promo_create(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(OperatorTokenError, match="mint-operator-token"):
        await _mint(db_session, tenant.id, ["tenant:read", "promo:create"])
    minted = await _mint(db_session, tenant.id, ["tenant:read"])
    (row,) = await list_panel_operator_tokens(db_session, tenant_id=tenant.id, now=NOW)
    identity = await get_account_with_tenant(db_session, account_id=row.account_id)
    assert row.jti == minted.jti and row.label == "ci" and row.scopes == ("tenant:read",)
    assert identity is not None and identity.role is Role.ADMIN
    assert minted.expires_at == NOW + dt.timedelta(days=30)


async def test_revoke_touches_only_this_tenants_operator_tokens(db_session: AsyncSession) -> None:
    tenant, other = await make_tenant(db_session), await make_tenant(db_session)
    minted = await _mint(db_session, tenant.id, ["tenant:read"])
    account = await make_account(db_session, tenant=tenant)
    agent_jti = token_jti(
        await mint_agent_mcp_token(
            db_session,
            account_id=account.id,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            label="a",
            secret=b"s" * 48,
            now=NOW,
        )
    )
    assert not await revoke_panel_operator_token(
        db_session, tenant_id=other.id, jti=minted.jti, now=NOW
    ), "another tenant's admin cannot revoke it"
    assert not await revoke_panel_operator_token(
        db_session, tenant_id=tenant.id, jti=agent_jti, now=NOW
    ), "a coding-tool key is not revoked from here"
    assert await revoke_panel_operator_token(
        db_session, tenant_id=tenant.id, jti=minted.jti, now=NOW
    )
    assert await list_panel_operator_tokens(db_session, tenant_id=tenant.id, now=NOW) == []


async def test_the_panel_neither_lists_nor_revokes_a_deployment_scoped_token(
    db_session: AsyncSession,
) -> None:
    """A CLI-minted `promo:create` token acts for the deployment, not the tenant."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli_jti = token_jti(
        await mint_operator_mcp_token(
            db_session,
            account_id=account.id,
            tenant_id=tenant.id,
            scopes=frozenset({"promo:create"}),
            label="issuer",
            secret=b"s" * 48,
            now=NOW,
            ttl_days=30,
        )
    )
    minted = await _mint(db_session, tenant.id, ["tenant:read"])

    listed = await list_panel_operator_tokens(db_session, tenant_id=tenant.id, now=NOW)
    assert [row.jti for row in listed] == [minted.jti], "only tenant-scoped tokens are listed"
    assert not await revoke_panel_operator_token(
        db_session, tenant_id=tenant.id, jti=cli_jti, now=NOW
    ), "the deployment's token is revoked only with the CLI"
    row = await get_mcp_token(db_session, jti=cli_jti)
    assert row is not None and row.revoked_at is None, "it keeps working"
