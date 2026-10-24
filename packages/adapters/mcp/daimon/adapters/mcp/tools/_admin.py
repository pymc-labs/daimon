"""Recheck delegated authority after awaits, without elevating a stored account role."""

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.admin_actions import DelegatedAction
from daimon.core.stores.daimon_admins import has_grant
from sqlalchemy.ext.asyncio import AsyncSession


async def current_admin(
    session: AsyncSession, auth: AuthIdentity, *, action: DelegatedAction | None = None
) -> bool:
    """A live platform attestation or a still-present grant from this request's verified roles."""
    if not auth.admin_for(action):
        return False
    if auth.platform not in {"discord", "slack", "teams"}:
        return True  # Local operator identities have no platform delegation.
    if auth.is_platform_admin:
        return True
    if auth.platform_user_id is None:
        return False
    return await has_grant(
        session,
        tenant_id=auth.tenant_id,
        platform=auth.platform,
        user_id=auth.platform_user_id,
        role_ids=auth.verified_role_ids,
    )
