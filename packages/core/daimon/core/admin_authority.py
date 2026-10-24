"""Fresh platform attestation for CLI and MCP authorization boundaries.

No token claim or stored account role proves platform authority. All network
failures refuse authority; delegation is read without a cache after attestation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.native_admins import is_daimon_admin
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.tenants import get_tenant
from daimon.core.teams_graph import GRAPH_SCOPE, GraphClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.config import Settings


@dataclass(frozen=True)
class AdminAuthority:
    platform_admin: bool = False
    daimon_admin: bool = False
    role_ids: tuple[str, ...] = ()


async def resolve_authority(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
) -> AdminAuthority:
    """Attest the selected registered tenant's user; unsupported/unknown fails closed."""
    async with sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.archived_at is not None or not platform_user_id:
        return AdminAuthority()
    platform_admin = False
    role_ids: tuple[str, ...] = ()
    try:
        if tenant.platform == "discord" and settings.discord is not None:
            facts = await DiscordRestMembershipResolver(
                settings.discord.bot_token.get_secret_value()
            ).resolve(tenant_id, tenant.external_id, platform_user_id, require_fresh=True)
            if facts.is_member is not True:
                return AdminAuthority()
            platform_admin, role_ids = facts.is_admin, (*facts.role_ids, tenant.external_id)
        elif tenant.platform == "slack":
            async with sessionmaker() as session:
                row = await get_slack_bot_token(session, team_id=tenant.external_id)
            if row is None:
                return AdminAuthority()
            cipher = build_multifernet(tuple(k.get_secret_value() for k in settings.crypto.keys))
            token = decrypt_token(cipher, row.encrypted_token)
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(
                    "https://slack.com/api/users.info",
                    params={"user": platform_user_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                data = response.json()
            user = data.get("user", {})
            if (
                data.get("ok") is not True
                or user.get("id") != platform_user_id
                or user.get("team_id") != tenant.external_id
                or user.get("deleted") is not False
                or user.get("is_bot") is not False
            ):
                return AdminAuthority()
            platform_admin = any(
                user.get(key) is True for key in ("is_admin", "is_owner", "is_primary_owner")
            )
        elif tenant.platform == "teams" and settings.teams is not None:
            teams = settings.teams
            if teams.tenant_id != tenant.external_id or teams.admin_team_id is None:
                return AdminAuthority()
            # Teams identities are Entra object ids, canonicalized at ingress.
            if str(uuid.UUID(platform_user_id)) != platform_user_id:
                return AdminAuthority()
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:

                async def graph_token() -> str | None:
                    response = await client.post(
                        f"https://login.microsoftonline.com/{uuid.UUID(teams.tenant_id)}/oauth2/v2.0/token",
                        data={
                            "client_id": teams.client_id,
                            "client_secret": teams.client_secret.get_secret_value(),
                            "scope": GRAPH_SCOPE,
                            "grant_type": "client_credentials",
                        },
                    )
                    response.raise_for_status()
                    value: object = response.json().get("access_token")
                    return value if isinstance(value, str) else None

                platform_admin = platform_user_id in await GraphClient(
                    client, graph_token
                ).list_team_owner_ids(teams.admin_team_id)
        else:
            return AdminAuthority()
    except Exception:
        # This is an authorization boundary: malformed responses, unavailable
        # credentials and platform outages must never become admin evidence.
        return AdminAuthority()
    async with sessionmaker() as session:
        admin = await is_daimon_admin(
            session,
            tenant_id=tenant_id,
            platform=tenant.platform,
            platform_user_id=platform_user_id,
            is_platform_admin=platform_admin,
            role_ids=role_ids,
        )
    return AdminAuthority(platform_admin=platform_admin, daimon_admin=admin, role_ids=role_ids)
