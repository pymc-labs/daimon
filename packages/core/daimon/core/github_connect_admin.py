"""Live platform admin attestation for the GitHub repo connect flow."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import cast

import httpx
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.config import Settings, TeamsSettings
from daimon.core.github_credentials import decrypt_token
from daimon.core.native_admins import AdminAttestationUnavailable, attest_discord_membership
from daimon.core.stores.domain import AccountIdentityRow
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.teams_graph import GraphClient, GraphUnavailable, app_token
from mux.drivers import isolated_ssl_context
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# The owner ids of a Teams team: (client, teams settings, team id). Injected so
# tests can stand in for Graph; the pages pass `teams_admin_owner_ids`.
TeamsOwnerLookup = Callable[[httpx.AsyncClient, TeamsSettings, str], Awaitable[frozenset[str]]]


async def teams_admin_owner_ids(
    client: httpx.AsyncClient, teams: TeamsSettings, team_id: str
) -> frozenset[str]:
    """The owners of a Teams team, read from Graph with the bot's app token."""
    token = app_token(
        client,
        tenant_id=teams.tenant_id,
        client_id=teams.client_id,
        client_secret=teams.client_secret.get_secret_value(),
    )
    return await GraphClient(client, token).list_team_owner_ids(team_id)


async def attest_native_admin(
    *,
    requester: AccountIdentityRow,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    http_client: httpx.AsyncClient | None = None,
    db_session: AsyncSession | None = None,
    teams_owner_ids: TeamsOwnerLookup | None = None,
) -> bool:
    """Attest live platform authority; GitHub never accepts delegated authority."""
    user_id = requester.platform_user_id
    if user_id is None:
        raise AdminAttestationUnavailable("platform principal unavailable")
    if requester.platform == "discord":
        facts = await attest_discord_membership(
            guild_id=requester.external_id,
            platform_user_id=user_id,
            settings=settings,
            http_client=http_client,
        )
        return facts.is_admin
    if requester.platform == "slack":
        try:
            async with (
                nullcontext(db_session) if db_session is not None else sessionmaker()
            ) as session:
                row = await get_slack_bot_token(session, team_id=requester.external_id)
        except SQLAlchemyError as exc:
            raise AdminAttestationUnavailable("workspace bot token lookup failed") from exc
        if row is None:
            raise AdminAttestationUnavailable("workspace bot token unavailable")
        try:
            token = decrypt_token(fernet, row.encrypted_token)
        except InvalidToken as exc:
            raise AdminAttestationUnavailable("workspace bot token unavailable") from exc

        async def slack_lookup(client: httpx.AsyncClient) -> object:
            response = await client.get(
                "https://slack.com/api/users.info",
                params={"user": user_id},
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
                timeout=5,
            )
            response.raise_for_status()
            return cast(object, response.json())

        try:
            async with asyncio.timeout(10):
                if http_client is None:
                    async with httpx.AsyncClient(
                        trust_env=False, verify=isolated_ssl_context()
                    ) as client:
                        payload = await slack_lookup(client)
                else:
                    payload = await slack_lookup(http_client)
            if (
                not isinstance(payload, dict)
                or cast(dict[str, object], payload).get("ok") is not True
            ):
                raise ValueError("Slack lookup did not succeed")
            user = cast(dict[str, object], payload).get("user")
            if not isinstance(user, dict) or cast(dict[str, object], user).get("id") != user_id:
                raise ValueError("Unbound Slack user")
            user = cast(dict[str, object], user)
            if user.get("deleted"):
                return False
            flags = ("is_admin", "is_owner", "is_primary_owner")
            if any(type(user.get(flag)) is not bool for flag in flags):
                raise ValueError("Missing Slack admin facts")
            return any(user[flag] for flag in flags)
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise AdminAttestationUnavailable("Slack lookup failed") from exc
    if requester.platform == "teams":
        teams = settings.teams
        if teams is None or teams.admin_team_id is None or teams_owner_ids is None:
            raise AdminAttestationUnavailable("Teams admin team unavailable")
        if requester.external_id.lower() != teams.tenant_id.lower():
            return False
        admin_team_id = teams.admin_team_id
        try:
            async with asyncio.timeout(15):
                if http_client is None:
                    async with httpx.AsyncClient(
                        trust_env=False, verify=isolated_ssl_context()
                    ) as client:
                        owners = await teams_owner_ids(client, teams, admin_team_id)
                else:
                    owners = await teams_owner_ids(http_client, teams, admin_team_id)
            return user_id.lower() in owners
        except (GraphUnavailable, httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise AdminAttestationUnavailable("Teams lookup failed") from exc
    raise AdminAttestationUnavailable("unsupported platform")
