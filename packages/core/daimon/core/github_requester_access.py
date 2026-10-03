"""Server-side requester GitHub permission lookup and token refresh."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from cryptography.fernet import MultiFernet
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.stores.github_links import (
    bump_link_generation,
    get_user_for_update,
    rotate_user_tokens,
)
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Access = Literal["none", "read", "write"]


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
    baseline_rows: Mapping[int, Access],
    ceiling_rows: Mapping[int, Access],
    asker_permissions: Mapping[int, Access],
) -> dict[int, Literal["read", "write"]]:
    """Compute max(baseline, min(ceiling, asker)) for each granted repository."""
    result: dict[int, Literal["read", "write"]] = {}
    for repo_id in baseline_rows.keys() | ceiling_rows.keys():
        baseline = baseline_rows.get(repo_id, "none")
        ceiling = ceiling_rows.get(repo_id, baseline)
        if _RANK[baseline] > _RANK[ceiling]:
            raise ValueError("baseline exceeds ceiling")
        level = _LEVELS[
            max(_RANK[baseline], min(_RANK[ceiling], _RANK[asker_permissions.get(repo_id, "none")]))
        ]
        if level != "none":
            result[repo_id] = level
    return result


class PermissionCache:
    """Five-minute per-process cache keyed by GitHub user and installation."""

    def __init__(self, ttl_seconds: float = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[tuple[int, int], tuple[float, int, dict[int, Access]]] = {}

    def get(self, user_id: int, installation_id: int, generation: int) -> dict[int, Access] | None:
        entry = self._entries.get((user_id, installation_id))
        if entry is None or entry[0] <= time.monotonic() or entry[1] != generation:
            return None
        return dict(entry[2])

    def put(
        self, user_id: int, installation_id: int, generation: int, value: dict[int, Access]
    ) -> None:
        self._entries[(user_id, installation_id)] = (
            time.monotonic() + self.ttl_seconds,
            generation,
            dict(value),
        )

    def drop_user(self, user_id: int) -> None:
        for key in tuple(self._entries):
            if key[0] == user_id:
                del self._entries[key]


async def list_github_pages(
    client: httpx.AsyncClient, path: str, token: str, key: str
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    page = 1
    while True:
        response = await client.get(
            f"https://api.github.com{path}",
            params={"per_page": 100, "page": page},
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
            if permissions.get("push") is True or permissions.get("admin") is True
            else "read"
            if permissions.get("pull") is True
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
    """Refresh under a row lock, then use the stored user token only on the server."""
    async with sessionmaker.begin() as session:
        row = await get_user_for_update(session, github_user_id=user_id)
        if row is None or row.status != "active":
            return {}
        cached = cache.get(user_id, installation_id, row.link_generation)
        if cached is not None:
            return cached
        now = datetime.now(UTC)
        if row.access_expires_at <= now + timedelta(minutes=1):
            if row.encrypted_refresh_token is None or (
                row.refresh_expires_at is not None and row.refresh_expires_at <= now
            ):
                await bump_link_generation(session, github_user_id=user_id, broken=True)
                cache.drop_user(user_id)
                return {}
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
            try:
                body = (
                    _RefreshPayload.model_validate(response.json()) if response.is_success else None
                )
            except ValueError:
                body = None
            if body is None:
                await bump_link_generation(session, github_user_id=user_id, broken=True)
                cache.drop_user(user_id)
                return {}
            rotated = await rotate_user_tokens(
                session,
                github_user_id=user_id,
                expected_generation=row.token_generation,
                encrypted_access_token=encrypt_token(fernet, body.access_token),
                encrypted_refresh_token=encrypt_token(fernet, body.refresh_token),
                access_expires_at=now + timedelta(seconds=body.expires_in),
                refresh_expires_at=now + timedelta(seconds=body.refresh_token_expires_in),
            )
            if not rotated:
                raise _RefreshRace
            token = body.access_token
        else:
            token = decrypt_token(fernet, row.encrypted_access_token)
        try:
            permissions = await requester_permissions(
                client, token=token, installation_id=installation_id
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                await bump_link_generation(session, github_user_id=user_id, broken=True)
                cache.drop_user(user_id)
                return {}
            raise
        cache.put(user_id, installation_id, row.link_generation, permissions)
        return permissions
