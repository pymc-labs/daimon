"""Native server and workspace admin identity helpers.

Teams admin-team owners use the group lookup cache. Stored admin role checks
never consult legacy channel grant rows.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Annotated, Final

import httpx
from daimon.core.authz import Subject, build_subject
from daimon.core.errors import DaimonError
from daimon.core.membership import Membership
from daimon.core.stores.accounts import get_account, get_account_with_tenant
from daimon.core.stores.domain import Role
from pydantic import BaseModel, ConfigDict, StringConstraints, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.channel_backend import BackendActor
    from daimon.core.config import Settings

_USER_ID = {"discord": r"[0-9]{15,21}"}
_CHANNEL_ID = {"discord": r"[0-9]{15,21}"}
GROUP_MEMBERS_TTL_S: Final = 60.0
GROUP_LOOKUP_FAILURE_TTL_S: Final = 15.0


def is_guild_admin_by_permissions(
    *, user_id: int, owner_id: int | None, administrator: bool, manage_guild: bool
) -> bool:
    """The guild owner, administrator or holder of Manage Server."""
    return (owner_id is not None and user_id == owner_id) or administrator or manage_guild


_DiscordId = Annotated[str, StringConstraints(pattern=r"^[0-9]{15,21}$")]


class _DiscordUser(BaseModel):
    model_config = ConfigDict(strict=True)
    id: _DiscordId


class _DiscordGuild(_DiscordUser):
    owner_id: _DiscordId


class _DiscordRole(_DiscordUser):
    permissions: Annotated[str, StringConstraints(pattern=r"^[0-9]+$", max_length=20)]


class _DiscordMember(BaseModel):
    model_config = ConfigDict(strict=True)
    user: _DiscordUser
    roles: list[_DiscordId]


class AdminAttestationUnavailable(DaimonError):
    """Current platform admin authority could not be read safely."""


async def attest_discord_membership(
    *,
    guild_id: str,
    platform_user_id: str,
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
) -> Membership:
    """Read current guild authority without consulting or changing a stored role."""
    from mux.drivers import isolated_ssl_context

    if (
        settings.discord is None
        or not re.fullmatch(_CHANNEL_ID["discord"], guild_id)
        or not re.fullmatch(_USER_ID["discord"], platform_user_id)
    ):
        raise AdminAttestationUnavailable("discord identity unavailable")
    token = settings.discord.bot_token.get_secret_value()
    if not token:
        raise AdminAttestationUnavailable("discord token unavailable")

    async def attest(client: httpx.AsyncClient) -> Membership:
        async def read(path: str) -> object:
            response = await client.get(
                f"https://discord.com/api/v10/guilds/{guild_id}{path}",
                headers={"Authorization": f"Bot {token}"},
                auth=None,
                follow_redirects=False,
                timeout=5,
            )
            response.raise_for_status()
            return response.json()

        guild = _DiscordGuild.model_validate(await read(""))
        roles = TypeAdapter(list[_DiscordRole]).validate_python(await read("/roles"), strict=True)
        member = _DiscordMember.model_validate(await read(f"/members/{platform_user_id}"))
        by_id = {role.id: int(role.permissions) for role in roles}
        if (
            guild.id != guild_id
            or member.user.id != platform_user_id
            or len(by_id) != len(roles)
            or len(set(member.roles)) != len(member.roles)
            or guild_id not in by_id
            or any(role not in by_id for role in member.roles)
            or any(value >= 2**64 for value in by_id.values())
        ):
            raise ValueError("Unbound guild permission facts")
        permissions = by_id[guild_id]
        for role in member.roles:
            permissions |= by_id[role]
        return Membership(
            is_member=True,
            role_ids=tuple(r for r in member.roles if r != guild_id),
            is_admin=is_guild_admin_by_permissions(
                user_id=int(platform_user_id),
                owner_id=int(guild.owner_id),
                administrator=bool(permissions & (1 << 3)),
                manage_guild=bool(permissions & (1 << 5)),
            ),
        )

    try:
        async with asyncio.timeout(10):
            if http_client is None:
                async with httpx.AsyncClient(
                    trust_env=False, verify=isolated_ssl_context()
                ) as client:
                    return await attest(client)
            return await attest(http_client)
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        raise AdminAttestationUnavailable("discord lookup failed") from exc


async def attest_discord_admin(
    *,
    guild_id: str,
    platform_user_id: str,
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Platform-only authority, for gates that must never admit delegated admins."""
    facts = await attest_discord_membership(
        guild_id=guild_id,
        platform_user_id=platform_user_id,
        settings=settings,
        http_client=http_client,
    )
    return facts.is_admin


async def verify_backend_actor(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
) -> BackendActor:
    """Read fresh guild authority for an existing principal; never create or promote it."""
    from daimon.core.channel_backend import AgentBackendRefused, BackendActor
    from daimon.core.stores.identity import find_platform_principal
    from daimon.core.stores.tenants import get_tenant

    if settings.discord is None or not re.fullmatch(_USER_ID["discord"], platform_user_id):
        raise AgentBackendRefused("admin_attestation_unavailable")
    if not settings.discord.bot_token.get_secret_value():
        raise AgentBackendRefused("admin_attestation_unavailable")

    async def identity() -> tuple[str, uuid.UUID]:
        async with sessionmaker() as session:
            tenant = await get_tenant(session, tenant_id)
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform="discord", external_id=platform_user_id
            )
            account = await get_account(session, principal.account_id) if principal else None
        if (
            tenant is None
            or tenant.platform != "discord"
            or not re.fullmatch(_CHANNEL_ID["discord"], tenant.external_id)
            or principal is None
            or account is None
            or account.tenant_id != tenant_id
        ):
            raise AgentBackendRefused("actor_not_found")
        return tenant.external_id, account.id

    guild_id, account_id = await identity()

    try:
        facts = await attest_discord_membership(
            guild_id=guild_id,
            platform_user_id=platform_user_id,
            settings=settings,
            http_client=http_client,
        )
    except AdminAttestationUnavailable:
        raise AgentBackendRefused("admin_attestation_unavailable") from None
    async with sessionmaker() as session:
        admin = await is_daimon_admin(
            session,
            tenant_id=tenant_id,
            platform="discord",
            platform_user_id=platform_user_id,
            is_platform_admin=facts.is_admin,
            role_ids=(*facts.role_ids, guild_id),
        )
    if not admin:
        raise AgentBackendRefused("admin_required")
    if await identity() != (guild_id, account_id):
        raise AgentBackendRefused("actor_not_found")
    return BackendActor(
        tenant_id=tenant_id,
        account_id=account_id,
        subject=build_subject(is_admin=admin, platform_user_id=platform_user_id),
    )


class GroupLookupFailed(DaimonError):
    """A native Teams owner lookup could not be completed."""


GroupMembers = Callable[[str], Awaitable[frozenset[str]]]
"""A group id to the user ids it admits; raises `GroupLookupFailed`.

On Discord it goes the other way, a user id to the role ids they hold now
(none once they left): Discord lists a role's members only to a bot with a
privileged intent.
"""

ServerAdminIds = Callable[[], Awaitable[Sequence[str]]]
"""A platform's live server admins, where the stored admin role is not the source (Teams)."""


class GroupMembersCache:
    """Group members by key, kept `ttl_s` seconds, so a busy channel costs one lookup a minute.

    A failed lookup is kept `failure_ttl_s` seconds and callers asking for one
    key at once share a single lookup, so a group the platform refuses (a
    missing scope, a rate limit) is not asked again by every request, and a
    burst of requests can't spend the app's rate limit for every other group
    admin. ``clock`` is injected for tests.
    """

    def __init__(
        self,
        *,
        ttl_s: float = GROUP_MEMBERS_TTL_S,
        failure_ttl_s: float = GROUP_LOOKUP_FAILURE_TTL_S,
        max_entries: int = 1_024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._failure_ttl_s = failure_ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[tuple[str, ...], tuple[float, frozenset[str]]] = {}
        self._failures: dict[tuple[str, ...], tuple[float, str]] = {}
        self._in_flight: dict[tuple[str, ...], asyncio.Task[frozenset[str]]] = {}

    async def members(
        self,
        key: tuple[str, ...],
        fetch: Callable[[], Awaitable[frozenset[str]]],
        *,
        fresh: bool = False,
    ) -> frozenset[str]:
        if fresh:
            # Authorization at an Always-allow click must observe a revoked
            # membership even when an earlier turn populated the short cache.
            return await fetch()
        now = self._clock()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < self._ttl_s:
            return cached[1]
        failed = self._failures.get(key)
        if failed is not None and now - failed[0] < self._failure_ttl_s:
            raise GroupLookupFailed(f"{failed[1]} (recent failure)")
        task = self._in_flight.get(key)
        if task is None or task.get_loop() is not asyncio.get_running_loop():
            task = asyncio.create_task(self._fetch(key, fetch), name="native_admins.group_lookup")
            self._in_flight[key] = task
            task.add_done_callback(lambda done: self._forget(key, done))
        # Shielded: one caller giving up must not cancel the lookup the others await.
        return await asyncio.shield(task)

    async def _fetch(
        self, key: tuple[str, ...], fetch: Callable[[], Awaitable[frozenset[str]]]
    ) -> frozenset[str]:
        try:
            members = await fetch()
        except GroupLookupFailed as exc:
            self._put(self._failures, key, str(exc), ttl_s=self._failure_ttl_s)
            raise
        self._failures.pop(key, None)
        self._put(self._entries, key, members, ttl_s=self._ttl_s)
        return members

    def _put[V](
        self,
        store: dict[tuple[str, ...], tuple[float, V]],
        key: tuple[str, ...],
        value: V,
        *,
        ttl_s: float,
    ) -> None:
        now = self._clock()
        if len(store) >= self._max_entries:
            live = {k: v for k, v in store.items() if now - v[0] < ttl_s}
            store.clear()
            if len(live) < self._max_entries:
                store.update(live)
        store[key] = (now, value)

    def _forget(self, key: tuple[str, ...], done: asyncio.Task[frozenset[str]]) -> None:
        if self._in_flight.get(key) is done:
            del self._in_flight[key]
        if not done.cancelled():
            done.exception()  # retrieved, so a lookup nobody awaits any more logs nothing


class AdminCaller(BaseModel):
    """The live platform admin signal for one user."""

    model_config = ConfigDict(frozen=True)
    platform_user_id: str | None
    is_server_admin: bool = False
    role_ids: tuple[str, ...] = ()


async def load_live_subject(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, caller: AdminCaller
) -> Subject:
    """Build a subject from current platform evidence and uncached tenant grants."""
    admin = await is_daimon_admin(
        session,
        tenant_id=tenant_id,
        platform=platform,
        platform_user_id=caller.platform_user_id,
        is_platform_admin=caller.is_server_admin,
        role_ids=caller.role_ids,
    )
    return build_subject(is_admin=admin, platform_user_id=caller.platform_user_id)


class StoredAdmin(BaseModel):
    """A platform-admin account snapshot; delegation never enters stored authority."""

    model_config = ConfigDict(frozen=True)
    is_admin: bool
    platform_user_id: str | None

    def subject(self) -> Subject:
        return build_subject(is_admin=self.is_admin, platform_user_id=self.platform_user_id)


async def read_stored_admin(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str | None,
    account_id: uuid.UUID,
    platform_user_id: str | None,
) -> StoredAdmin:
    """Read a bound platform-admin snapshot. Delegates never gain transcript exceptions."""
    account = await get_account(session, account_id)
    identity = await get_account_with_tenant(session, account_id=account_id)
    bound = (
        identity is not None
        and identity.tenant_id == tenant_id
        and (platform is None or identity.platform == platform)
        and identity.platform_user_id == platform_user_id
    )
    return StoredAdmin(
        is_admin=bound
        and account is not None
        and account.tenant_id == tenant_id
        and account.role is Role.ADMIN,
        platform_user_id=platform_user_id,
    )


async def load_stored_subject(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str | None,
    account_id: uuid.UUID,
    platform_user_id: str | None,
) -> Subject:
    stored = await read_stored_admin(
        session,
        tenant_id=tenant_id,
        platform=platform,
        account_id=account_id,
        platform_user_id=platform_user_id,
    )
    return stored.subject()


async def is_daimon_admin(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str | None,
    is_platform_admin: bool,
    role_ids: Sequence[str] = (),
) -> bool:
    """Live platform authority or an uncached grant scoped to this registered tenant."""
    from daimon.core.stores.daimon_admins import has_grant
    from daimon.core.stores.tenants import get_tenant

    tenant = await get_tenant(session, tenant_id)
    if (
        tenant is None
        or tenant.platform != platform
        or tenant.archived_at is not None
        or not platform_user_id
    ):
        return False
    return is_platform_admin is True or await has_grant(
        session,
        tenant_id=tenant_id,
        platform=platform,
        user_id=platform_user_id,
        role_ids=role_ids,
    )
