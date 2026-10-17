"""Server-side requester GitHub permission lookup and token refresh."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import httpx
from cryptography.fernet import MultiFernet
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.stores.github_linked_pending import DETACHED_REFRESH_SETTLE
from daimon.core.stores.github_links import (
    GitHubUser,
    bump_link_generation,
    get_user,
    get_user_for_update,
    rotate_user_tokens,
)
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Access = Literal["none", "read", "write"]

# How long a cancelled caller keeps its HTTP client open for a refresh detached
# from it (`settle_detached_refreshes`): a GitHub POST (client timeout 15 to 20 s)
# and the commit that stores its answer. The pending-grant store counts this
# window in its renewal-gap bound, so the value lives there.
DETACHED_REFRESH_SETTLE_S: Final = DETACHED_REFRESH_SETTLE.total_seconds()


class _RepoPermissionPayload(BaseModel):
    id: int
    permissions: dict[str, bool] = Field(default_factory=dict)


class _RefreshPayload(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_token_expires_in: int


_RANK: Mapping[Access, int] = {"none": 0, "read": 1, "write": 2}
_LEVELS: tuple[Access, ...] = ("none", "read", "write")


def effective_access(
    agent_abilities: Mapping[int, Access],
    asker_permissions: Mapping[int, Access],
    baselines: Mapping[int, Access] | None = None,
) -> dict[int, Literal["read", "write"]]:
    """Give the agent's baseline, plus asker access up to its ceiling."""
    result: dict[int, Literal["read", "write"]] = {}
    for repo_id, ability in agent_abilities.items():
        baseline = (baselines or {}).get(repo_id, "none")
        level = _LEVELS[
            max(_RANK[baseline], min(_RANK[ability], _RANK[asker_permissions.get(repo_id, "none")]))
        ]
        if level != "none":
            result[repo_id] = level
    return result


class PermissionCache:
    """Five-minute per-process cache keyed by GitHub user and installation."""

    def __init__(self, ttl_seconds: float = 300, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[int, int], tuple[float, int, dict[int, Access]]] = (
            OrderedDict()
        )

    def get(self, user_id: int, installation_id: int, generation: int) -> dict[int, Access] | None:
        entry = self._entries.get((user_id, installation_id))
        if entry is None or entry[0] <= time.monotonic() or entry[1] != generation:
            self._entries.pop((user_id, installation_id), None)
            return None
        self._entries.move_to_end((user_id, installation_id))
        return dict(entry[2])

    def put(
        self, user_id: int, installation_id: int, generation: int, value: dict[int, Access]
    ) -> None:
        self._entries[(user_id, installation_id)] = (
            time.monotonic() + self.ttl_seconds,
            generation,
            dict(value),
        )
        self._entries.move_to_end((user_id, installation_id))
        if len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def drop_user(self, user_id: int) -> None:
        for key in tuple(self._entries):
            if key[0] == user_id:
                del self._entries[key]


async def list_github_pages(
    client: httpx.AsyncClient,
    path: str,
    token: str,
    key: str,
    *,
    params: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    page = 1
    while True:
        response = await client.get(
            f"https://api.github.com{path}",
            params={**(params or {}), "per_page": 100, "page": page},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        body: object = response.json()
        if isinstance(body, dict):
            parsed = TypeAdapter(dict[str, object]).validate_python(body)
            body = parsed[key]
        batch = TypeAdapter(list[dict[str, object]]).validate_python(body)
        rows.extend(batch)
        if len(batch) < 100:
            return rows
        page += 1


async def requester_permissions(
    client: httpx.AsyncClient,
    *,
    token: str,
    installation_id: int,
) -> dict[int, Access]:
    rows = await list_github_pages(
        client, f"/user/installations/{installation_id}/repositories", token, "repositories"
    )
    result: dict[int, Access] = {}
    for row in rows:
        parsed = _RepoPermissionPayload.model_validate(row)
        permissions = parsed.permissions
        result[parsed.id] = (
            "write"
            if any(permissions.get(key) is True for key in ("admin", "maintain", "push"))
            else "read"
            if permissions.get("triage") is True or permissions.get("pull") is True
            else "none"
        )
    return result


class _RefreshRace(Exception):
    pass


async def linked_permissions(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    user_id: int,
    installation_id: int,
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
    cache: PermissionCache,
) -> dict[int, Access]:
    for _ in range(3):
        try:
            return await _linked_permissions_once(
                sessionmaker,
                client,
                user_id=user_id,
                installation_id=installation_id,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
                cache=cache,
            )
        except _RefreshRace:
            continue
    raise RuntimeError("GitHub user token changed repeatedly during refresh")


async def _linked_permissions_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    user_id: int,
    installation_id: int,
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
    cache: PermissionCache,
) -> dict[int, Access]:
    """Read/cache without locking; serialize refresh, then list after commit."""
    async with sessionmaker() as session:
        row = await get_user(session, github_user_id=user_id)
    if row is None or row.status != "active":
        return {}
    cached = cache.get(user_id, installation_id, row.link_generation)
    if cached is not None:
        return cached

    token, used_generation, _ = await _linked_access_token_once(
        sessionmaker,
        client,
        user_id=user_id,
        row=row,
        fernet=fernet,
        client_id=client_id,
        client_secret=client_secret,
        cache=cache,
    )
    if token is None:
        return {}

    try:
        permissions = await requester_permissions(
            client, token=token, installation_id=installation_id
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            if exc.response.status_code == 403 and (
                exc.response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in exc.response.headers
                or "secondary rate limit" in exc.response.text.lower()
            ):
                raise
            permissions = {}
        elif exc.response.status_code == 401:
            async with sessionmaker.begin() as session:
                locked = await get_user_for_update(session, github_user_id=user_id)
                if locked is None or locked.status != "active":
                    return {}
                if locked.token_generation != used_generation:
                    raise _RefreshRace from exc
                await bump_link_generation(session, github_user_id=user_id, broken=True)
            cache.drop_user(user_id)
            return {}
        else:
            raise
    cache.put(user_id, installation_id, row.link_generation, permissions)
    return permissions


async def linked_access_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    user_id: int,
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
) -> str | None:
    """Get a current linked-user token, using the same serialized refresh path."""
    token, _ = await linked_access_token_with_generation(
        sessionmaker,
        client,
        user_id=user_id,
        fernet=fernet,
        client_id=client_id,
        client_secret=client_secret,
    )
    return token


async def linked_access_token_with_generation(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    user_id: int,
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
    refresh_if_refresh_expires_before: datetime | None = None,
    lock_timeout: str | None = None,
) -> tuple[str | None, int | None]:
    """Return the token and link generation from the same stored credential row.

    ``refresh_if_refresh_expires_before`` also refreshes a still-valid access token,
    but only while the row's refresh token still expires before that moment, judged
    under the refresh lock. A refresh another caller finished first is reused, never
    spent again. ``lock_timeout`` (a Postgres interval such as ``"15s"``) bounds the
    wait for that lock; hitting it raises SQLSTATE 55P03 before any call to GitHub.
    Once GitHub has answered, the new tokens are stored even if this call is
    cancelled.
    """
    for _ in range(3):
        async with sessionmaker() as session:
            row = await get_user(session, github_user_id=user_id)
        if row is None or row.status != "active":
            return None, None
        try:
            token, _, link_generation = await _linked_access_token_once(
                sessionmaker,
                client,
                user_id=user_id,
                row=row,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
                cache=PermissionCache(ttl_seconds=0),
                refresh_if_refresh_expires_before=refresh_if_refresh_expires_before,
                lock_timeout=lock_timeout,
            )
            return token, link_generation
        except _RefreshRace:
            continue
    raise RuntimeError("GitHub user token changed repeatedly during refresh")


# Refresh transactions still running after their caller was cancelled.
_detached_refreshes: set[asyncio.Task[tuple[str | None, int, int]]] = set()


def _release_detached(task: asyncio.Task[tuple[str | None, int, int]]) -> None:
    _detached_refreshes.discard(task)
    if not task.cancelled():
        # Nobody awaits a detached refresh; its failure is the next use's to retry.
        _ = task.exception()


async def drain_detached_refreshes(timeout: float) -> int:
    """Wait up to `timeout` seconds for refreshes still running after their caller was cancelled.

    GitHub retires the old token pair the moment it answers, so shutdown must
    let each of these store its answer before the HTTP client closes and the
    engine is disposed. Returns how many were still running at the deadline (0
    when all finished); those are left running, not cancelled here.
    """
    deadline = time.monotonic() + timeout
    while True:
        running = {task for task in _detached_refreshes if not task.done()}
        if not running:
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return len(running)
        await asyncio.wait(running, timeout=remaining)


@asynccontextmanager
async def settle_detached_refreshes(
    timeout: float | Callable[[], float] = DETACHED_REFRESH_SETTLE_S,
) -> AsyncGenerator[None]:
    """Hold the HTTP client of the enclosing `async with` open while its cancel settles.

    Use it inside the client's own `async with` (listed after it, so it exits
    first). If the body is cancelled while a refresh runs detached, the refresh
    still needs the client to finish its GitHub call, so the cancel waits up to
    `timeout` seconds for the refresh to store its answer before the client
    closes. Other exits do not wait. A callable `timeout` is read when the cancel
    arrives, so a shutdown can pass what remains of its shared deadline.
    """
    try:
        yield
    except asyncio.CancelledError:
        await drain_detached_refreshes(timeout() if callable(timeout) else timeout)
        raise


async def _shielded(
    work: Coroutine[object, object, tuple[str | None, int, int]],
) -> tuple[str | None, int, int]:
    """Run `work` so a cancel of the caller cannot stop it half way.

    The caller's cancellation still raises at its await, while the work runs to
    its commit in the background (kept referenced here until it ends).
    """
    task = asyncio.ensure_future(work)
    _detached_refreshes.add(task)
    task.add_done_callback(_release_detached)
    return await asyncio.shield(task)


async def _refresh_locked(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    user_id: int,
    row: GitHubUser,
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
    cache: PermissionCache,
    needs_refresh: Callable[[GitHubUser], bool],
    lock_timeout: str | None,
) -> tuple[str | None, int, int]:
    """The refresh transaction: lock, spend the refresh token, store the new pair."""
    async with sessionmaker.begin() as session:
        if lock_timeout is not None:
            # Bounds the wait for the lock below (and any row lock after it), so a
            # sweep gives up on a held refresh instead of waiting without end.
            await session.execute(
                text("SELECT set_config('lock_timeout', :value, true)"), {"value": lock_timeout}
            )
        # A transaction-scoped advisory lock serializes refreshes across workers.
        # Unlike FOR UPDATE, it never locks the token row during the GitHub call.
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('github-refresh:' || CAST(:user_id AS text), 0))"
            ),
            {"user_id": str(user_id)},
        )
        fresh = await get_user(session, github_user_id=user_id)
        if fresh is None or fresh.status != "active":
            return None, row.token_generation, row.link_generation
        row = fresh
        if needs_refresh(row):
            if row.encrypted_refresh_token is None or (
                row.refresh_expires_at is not None and row.refresh_expires_at <= datetime.now(UTC)
            ):
                broken = await bump_link_generation(
                    session,
                    github_user_id=user_id,
                    broken=True,
                    expected_token_generation=row.token_generation,
                )
                if broken is None:
                    raise _RefreshRace
                cache.drop_user(user_id)
                return None, row.token_generation, row.link_generation
            response = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": decrypt_token(fernet, row.encrypted_refresh_token),
                },
                headers={"Accept": "application/json"},
            )
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            payload: object = response.json()
            error_payload = (
                TypeAdapter(dict[str, object]).validate_python(payload)
                if isinstance(payload, dict)
                else {}
            )
            if error_payload.get("error") in (
                "bad_refresh_token",
                "invalid_grant",
            ):
                broken = await bump_link_generation(
                    session,
                    github_user_id=user_id,
                    broken=True,
                    expected_token_generation=row.token_generation,
                )
                if broken is None:
                    raise _RefreshRace
                cache.drop_user(user_id)
                return None, row.token_generation, row.link_generation
            response.raise_for_status()
            if isinstance(error_payload.get("error"), str):
                raise RuntimeError(f"GitHub refresh refused: {error_payload['error']}")
            body = _RefreshPayload.model_validate(payload)
            rotated = await rotate_user_tokens(
                session,
                github_user_id=user_id,
                expected_generation=row.token_generation,
                encrypted_access_token=encrypt_token(fernet, body.access_token),
                encrypted_refresh_token=encrypt_token(fernet, body.refresh_token),
                access_expires_at=datetime.now(UTC) + timedelta(seconds=body.expires_in),
                refresh_expires_at=datetime.now(UTC)
                + timedelta(seconds=body.refresh_token_expires_in),
            )
            if not rotated:
                raise _RefreshRace
            token = body.access_token
            used_generation = row.token_generation + 1
        else:
            token = decrypt_token(fernet, row.encrypted_access_token)
            used_generation = row.token_generation
    return token, used_generation, row.link_generation


async def _linked_access_token_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    user_id: int,
    row: GitHubUser,
    fernet: MultiFernet,
    client_id: str,
    client_secret: str,
    cache: PermissionCache,
    refresh_if_refresh_expires_before: datetime | None = None,
    lock_timeout: str | None = None,
) -> tuple[str | None, int, int]:
    def needs_refresh(candidate: GitHubUser) -> bool:
        now = datetime.now(UTC)
        if candidate.access_expires_at <= now + timedelta(minutes=1):
            return True
        # Keep-alive: a live refresh token that is close to its own expiry.
        return (
            refresh_if_refresh_expires_before is not None
            and candidate.encrypted_refresh_token is not None
            and candidate.refresh_expires_at is not None
            and now < candidate.refresh_expires_at <= refresh_if_refresh_expires_before
        )

    if needs_refresh(row):
        # GitHub retires the old pair the moment it answers, so the answer must be
        # stored: a cancel between the response and the commit must not roll it back.
        return await _shielded(
            _refresh_locked(
                sessionmaker,
                client,
                user_id=user_id,
                row=row,
                fernet=fernet,
                client_id=client_id,
                client_secret=client_secret,
                cache=cache,
                needs_refresh=needs_refresh,
                lock_timeout=lock_timeout,
            )
        )
    token = decrypt_token(fernet, row.encrypted_access_token)
    return token, row.token_generation, row.link_generation
