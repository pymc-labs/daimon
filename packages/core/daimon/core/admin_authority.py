"""Fresh platform attestation for CLI and MCP authorization boundaries.

No token claim or stored account role proves platform authority. All network
failures refuse authority; delegation is read without a cache after attestation.
An optional `AuthorityCache` reuses an answered platform lookup for up to
`AUTHORITY_TTL_S` seconds; a failed lookup is never kept and never answered
from an older one.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.native_admins import is_daimon_admin
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.tenants import TenantRow, get_tenant
from daimon.core.teams_graph import GRAPH_SCOPE, GraphClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.config import Settings


@dataclass(frozen=True)
class AdminAuthority:
    platform_admin: bool = False
    daimon_admin: bool = False
    role_ids: tuple[str, ...] = ()


#: How long an answered platform lookup stands in for a fresh one. A platform
#: role or membership removed in the meantime takes effect within this.
AUTHORITY_TTL_S = 30.0


@dataclass(frozen=True)
class _Attestation:
    platform_admin: bool = False
    role_ids: tuple[str, ...] = ()


type AuthorityKey = tuple[uuid.UUID, str, str]


class AuthorityCache:
    """Answered platform lookups by (tenant, platform, user), kept `ttl_s` from lookup start.

    A refusal is kept like a grant. A failed lookup raises and is not kept, so
    the next request asks again. Concurrent requests for one key share a lookup,
    which is abandoned after `ttl_s`; a caller that receives an answer older than
    `ttl_s` gets a failure instead. Past `max_in_flight` pending lookups a new
    key fails without asking the platform.
    """

    def __init__(
        self,
        *,
        ttl_s: float = AUTHORITY_TTL_S,
        max_entries: int = 4_096,
        max_in_flight: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._max_in_flight = max_in_flight
        self._clock = clock
        self._entries: dict[AuthorityKey, tuple[float, _Attestation | None]] = {}
        self._in_flight: dict[AuthorityKey, asyncio.Task[tuple[float, _Attestation | None]]] = {}

    async def get(
        self, key: AuthorityKey, fetch: Callable[[], Awaitable[_Attestation | None]]
    ) -> _Attestation | None:
        entry = self._entries.get(key)
        if entry is not None and self._clock() - entry[0] < self._ttl_s:
            return entry[1]
        task = self._in_flight.get(key)
        if task is None or task.get_loop() is not asyncio.get_running_loop():
            if len(self._in_flight) >= self._max_in_flight:
                raise LookupError("too many pending authority lookups")
            task = asyncio.create_task(self._fetch(key, fetch), name="admin_authority.lookup")
            self._in_flight[key] = task
            task.add_done_callback(lambda done: self._forget(key, done))
        # Shielded: one caller giving up must not cancel the lookup the others await.
        started, attestation = await asyncio.shield(task)
        if self._clock() - started >= self._ttl_s:
            raise LookupError("authority lookup answered too late")
        return attestation

    async def _fetch(
        self, key: AuthorityKey, fetch: Callable[[], Awaitable[_Attestation | None]]
    ) -> tuple[float, _Attestation | None]:
        started = self._clock()
        async with asyncio.timeout(self._ttl_s):
            attestation = await fetch()
        now = self._clock()
        if now - started >= self._ttl_s:
            return started, attestation
        self._entries.pop(key, None)
        self._entries[key] = (started, attestation)
        if len(self._entries) > self._max_entries:
            self._entries = {k: v for k, v in self._entries.items() if now - v[0] < self._ttl_s}
            while len(self._entries) > self._max_entries:
                del self._entries[next(iter(self._entries))]
        return started, attestation

    def _forget(
        self, key: AuthorityKey, done: asyncio.Task[tuple[float, _Attestation | None]]
    ) -> None:
        if self._in_flight.get(key) is done:
            del self._in_flight[key]


async def resolve_authority(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    cache: AuthorityCache | None = None,
) -> AdminAuthority:
    """Attest the selected registered tenant's user; unsupported/unknown fails closed."""
    async with sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.archived_at is not None or not platform_user_id:
        return AdminAuthority()

    async def attest() -> _Attestation | None:
        return await _attest(sessionmaker, settings, tenant, platform_user_id)

    try:
        attestation = await (
            attest()
            if cache is None
            else cache.get((tenant_id, tenant.platform, platform_user_id), attest)
        )
    except Exception:
        # This is an authorization boundary: malformed responses, unavailable
        # credentials and platform outages must never become admin evidence.
        return AdminAuthority()
    if attestation is None:
        return AdminAuthority()
    async with sessionmaker() as session:
        admin = await is_daimon_admin(
            session,
            tenant_id=tenant_id,
            platform=tenant.platform,
            platform_user_id=platform_user_id,
            is_platform_admin=attestation.platform_admin,
            role_ids=attestation.role_ids,
        )
    return AdminAuthority(
        platform_admin=attestation.platform_admin,
        daimon_admin=admin,
        role_ids=attestation.role_ids,
    )


async def _attest(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    tenant: TenantRow,
    platform_user_id: str,
) -> _Attestation | None:
    """The platform's answer for this user, None when it refuses; raises when it can't answer."""
    if tenant.platform == "discord" and settings.discord is not None:
        facts = await DiscordRestMembershipResolver(
            settings.discord.bot_token.get_secret_value()
        ).resolve(tenant.id, tenant.external_id, platform_user_id, require_fresh=True)
        if facts.is_member is None:
            raise LookupError("discord membership unavailable")
        if facts.is_member is not True:
            return None
        return _Attestation(facts.is_admin, (*facts.role_ids, tenant.external_id))
    if tenant.platform == "slack":
        async with sessionmaker() as session:
            row = await get_slack_bot_token(session, team_id=tenant.external_id)
        if row is None:
            return None
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
            return None
        return _Attestation(
            any(user.get(key) is True for key in ("is_admin", "is_owner", "is_primary_owner"))
        )
    if tenant.platform == "teams" and settings.teams is not None:
        teams = settings.teams
        if teams.tenant_id != tenant.external_id or teams.admin_team_id is None:
            return None
        # Teams identities are Entra object ids, canonicalized at ingress.
        if str(uuid.UUID(platform_user_id)) != platform_user_id:
            return None
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

            owners = await GraphClient(client, graph_token).list_team_owner_ids(teams.admin_team_id)
        return _Attestation(platform_user_id in owners)
    return None
