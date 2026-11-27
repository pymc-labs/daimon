"""A Discord server with `audience_hold` on, and members with or without an internal role."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workspace_members import upsert_member
from daimon.testing.factories import make_platform_principal
from sqlalchemy.ext.asyncio import AsyncSession

INTERNAL_ROLE = "9001"


async def hold_audience(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Turn `audience_hold` on for the tenant, with one internal role."""
    await set_access_policy(
        session, tenant_id=tenant_id, policy=TenantAccessPolicy(internal_role_ids=(INTERNAL_ROLE,))
    )
    await set_feature_value(
        session,
        tenant_id=tenant_id,
        target=FeatureTarget("workspace"),
        key="audience_hold",
        value="on",
        set_by=None,
        surface="chat",
    )


async def link_discord_member(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    internal: bool,
    discord_id: str | None = None,
) -> str:
    """Link the account to a Discord member, with the internal role or without it; the
    member's Discord id."""
    discord_id = discord_id or str(uuid.uuid4().int % 10**17)
    tenant = await get_tenant(session, tenant_id)
    account = await get_account(session, account_id)
    if tenant is None or account is None:
        raise ValueError("no such tenant or account")
    await make_platform_principal(
        session, platform="discord", external_id=discord_id, tenant=tenant, account=account
    )
    await upsert_member(
        session,
        tenant_id=tenant_id,
        platform_user_id=discord_id,
        is_admin=False,
        now=datetime.now(UTC),
        role_ids=[INTERNAL_ROLE] if internal else ["9002"],
    )
    return discord_id
