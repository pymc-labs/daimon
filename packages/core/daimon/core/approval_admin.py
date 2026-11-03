"""Fresh native administrator evidence for approval-gate changes across entry points."""

from __future__ import annotations

import uuid
from typing import cast

import httpx
from cryptography.fernet import InvalidToken
from daimon.core.authz import Subject
from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.native_admins import AdminAttestationUnavailable, attest_discord_admin
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.tenants import get_tenant
from daimon.core.teams_graph import GRAPH_SCOPE, GraphClient, GraphUnavailable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def approval_admin(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    http_client: httpx.AsyncClient | None = None,
) -> Subject:
    """Never trust persisted roles, token admin claims, or stale owner evidence.

    A supplied client is for adapter transport reuse and tests. The registered
    tenant selects the platform and workspace; request fields cannot select them.
    """
    denied = Subject(platform_user_id=user_id)
    if not user_id:
        return denied
    async with factory() as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return denied
        slack = (
            await get_slack_bot_token(session, team_id=tenant.external_id)
            if tenant.platform == "slack"
            else None
        )
    if http_client is None:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10) as http:
            return await approval_admin(
                factory, settings, tenant_id=tenant_id, user_id=user_id, http_client=http
            )
    try:
        admin = False
        if tenant.platform == "discord":
            admin = await attest_discord_admin(
                guild_id=tenant.external_id,
                platform_user_id=user_id,
                settings=settings,
                http_client=http_client,
            )
        elif tenant.platform == "slack" and slack is not None and settings.crypto.keys:
            token = decrypt_token(
                build_multifernet(tuple(key.get_secret_value() for key in settings.crypto.keys)),
                slack.encrypted_token,
            )
            response = await http_client.get(
                "https://slack.com/api/users.info",
                params={"user": user_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload: object = response.json()
            if not isinstance(payload, dict):
                return denied
            body = cast("dict[str, object]", payload)
            user = body.get("user")
            if body.get("ok") is True and isinstance(user, dict):
                user = cast("dict[str, object]", user)
                admin = (
                    user.get("id") == user_id
                    and user.get("team_id") == tenant.external_id
                    and user.get("deleted") is False
                    and any(
                        user.get(flag) is True
                        for flag in ("is_admin", "is_owner", "is_primary_owner")
                    )
                )
        elif tenant.platform == "teams":
            teams = settings.teams
            if (
                teams is None
                or teams.tenant_id != tenant.external_id
                or teams.admin_team_id is None
            ):
                return denied
            response = await http_client.post(
                f"https://login.microsoftonline.com/{teams.tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": teams.client_id,
                    "client_secret": teams.client_secret.get_secret_value(),
                    "grant_type": "client_credentials",
                    "scope": GRAPH_SCOPE,
                },
            )
            response.raise_for_status()
            payload: object = response.json()
            if not isinstance(payload, dict):
                return denied
            body = cast("dict[str, object]", payload)
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                return denied

            async def graph_token() -> str:
                return token

            owners = await GraphClient(http_client, graph_token).list_team_owner_ids(
                teams.admin_team_id
            )
            admin = user_id.lower() in owners
        return Subject(is_admin=admin, platform_user_id=user_id)
    except (
        AdminAttestationUnavailable,
        httpx.HTTPError,
        InvalidToken,
        ValueError,
        GraphUnavailable,
    ):
        return denied
