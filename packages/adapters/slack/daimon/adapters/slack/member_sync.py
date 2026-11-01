"""Each workspace's member list, which web access follows.

Loaded in full from users.list at start and on a workspace's first event after that
(a new install), then kept current by team_join and user_change. Slack never replays an
event missed while disconnected, so every list is loaded again after a Socket Mode
reconnect and every few hours. Bots, Slackbot and deactivated users are not members;
admin is a workspace admin or owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import aiohttp
import structlog
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.member_reloads import ReloadChanges, overlay
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.tenants import get_tenant, list_tenants_by_platform
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger(__name__)

_SLACKBOT = "USLACKBOT"
#: A failed load is not tried again for this long.
_RETRY_AFTER_S = 600.0
#: Nor is one for a workspace not installed (yet), whose install may be landing.
_UNINSTALLED_RETRY_S = 60.0
#: Every list is loaded again this often, for events lost without a reconnect.
RELOAD_EVERY_S = 6 * 3600.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def member_entry(user: Mapping[str, Any], *, team_id: str) -> tuple[str, bool] | None:
    """`(user id, is admin)` for a member of `team_id`; None for anyone else."""
    user_id = user.get("id")
    if not isinstance(user_id, str) or not user_id or user_id == _SLACKBOT:
        return None
    if user.get("is_bot") or user.get("deleted") or user.get("team_id") not in (None, team_id):
        return None
    admin = any(user.get(k) for k in ("is_admin", "is_owner", "is_primary_owner"))
    return user_id, admin


async def _all_users(client: AsyncWebClient) -> list[Mapping[str, Any]]:
    users: list[Mapping[str, Any]] = []
    cursor: str | None = None
    while True:
        resp = await client.users_list(limit=200, cursor=cursor)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        users.extend(cast("list[Mapping[str, Any]]", resp.get("members") or []))  # pyright: ignore[reportUnknownMemberType]
        metadata = cast("dict[str, object]", resp.get("response_metadata") or {})  # pyright: ignore[reportUnknownMemberType]
        cursor = str(metadata.get("next_cursor") or "") or None
        if cursor is None:
            return users


class SlackMemberSync:
    """Loads and updates the member lists of this process's workspaces."""

    def __init__(
        self,
        runtime: SlackRuntime,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        reload_every_s: float = RELOAD_EVERY_S,
    ) -> None:
        self._runtime = runtime
        self._clock = clock
        self._monotonic = monotonic
        self._reload_every_s = reload_every_s
        # Team id to when it may be loaded again: never once loaded or loading.
        self._next_load: dict[str, float] = {}
        self._loading: set[str] = set()
        self._changes = ReloadChanges()
        self._hellos = 0
        self._reload_wanted = asyncio.Event()

    def claim_load(self, team_id: str, *, again: bool = False) -> bool:
        """Whether the caller should load `team_id` now; at most one load at a time.

        `again` loads a workspace already loaded, or waiting to retry, as well."""
        if not team_id or team_id in self._loading:
            return False
        if not again and self._next_load.get(team_id, 0.0) > self._monotonic():
            return False
        self._loading.add(team_id)
        self._next_load[team_id] = float("inf")
        return True

    def forget(self, team_id: str) -> None:
        """The install ended: a reinstall loads the list afresh on its first event."""
        self._next_load.pop(team_id, None)

    async def keep_loaded(self) -> None:
        """Load every live install's list now, then again after each reconnect and every
        `reload_every_s`. Runs until cancelled."""
        while True:
            self._reload_wanted.clear()
            try:
                await self.load_installed(again=True)
            except (SlackApiError, aiohttp.ClientError, TimeoutError, SQLAlchemyError) as err:
                # One failed pass must not end the loop: no list would load again until
                # the process restarts.
                log.warning("slack.members.pass_failed", err_type=type(err).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._reload_wanted.wait(), self._reload_every_s)

    async def on_socket_message(
        self, _client: object, message: dict[str, Any], _raw: str | None
    ) -> None:
        """Socket Mode listener: each connection opens with a hello, so a hello after the
        first is a reconnect, and the events sent meanwhile may be lost."""
        if message.get("type") != "hello":
            return
        self._hellos += 1
        if self._hellos > 1:
            log.info("slack.members.reload_after_reconnect")
            self._reload_wanted.set()

    async def load_installed(self, *, again: bool = False) -> None:
        """Load every live install's list."""
        tenants = await list_tenants_by_platform(self._runtime.sessionmaker, platform="slack")
        await self.load_all((t.external_id for t in tenants if t.archived_at is None), again=again)

    async def load_all(self, team_ids: Iterable[str], *, again: bool = False) -> None:
        """One workspace at a time, to stay inside users.list's rate limit."""
        for team_id in team_ids:
            if self.claim_load(team_id, again=again):
                await self.load(team_id)

    async def load(self, team_id: str) -> None:
        """Replace the workspace's list with users.list; a failure is retried later."""
        try:
            if not await self._load(team_id):
                self._next_load[team_id] = self._monotonic() + _UNINSTALLED_RETRY_S
        except (SlackApiError, aiohttp.ClientError, TimeoutError, SQLAlchemyError) as err:
            self._next_load[team_id] = self._monotonic() + _RETRY_AFTER_S
            log.warning("slack.members.load_failed", team_id=team_id, err_type=type(err).__name__)
        finally:
            self._loading.discard(team_id)

    async def _load(self, team_id: str) -> bool:
        """Whether the workspace is installed, and so loaded."""
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        self._changes.start(tenant_id)
        try:
            loaded = await self._replace(team_id, tenant_id)
        finally:
            late = self._changes.finish(tenant_id)
        if loaded and late:
            async with self._runtime.sessionmaker.begin() as session:
                await members_store.apply_changes(
                    session, tenant_id=tenant_id, changes=late, now=self._clock()
                )
        return loaded

    async def _replace(self, team_id: str, tenant_id: uuid.UUID) -> bool:
        client = await resolve_web_client(self._runtime, team_id=team_id)
        if client is None:
            return False
        auth = await client.auth_test()  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
        name = str(auth.get("team") or "")  # pyright: ignore[reportUnknownMemberType]
        members = dict(
            entry
            for user in await _all_users(client)
            if (entry := member_entry(user, team_id=team_id)) is not None
        )
        async with self._runtime.sessionmaker.begin() as session:
            tenant = await get_tenant(session, tenant_id)
            if tenant is None or tenant.archived_at is not None:
                return False
            members = overlay(members, self._changes.seen(tenant_id))
            await members_store.replace_members(
                session,
                tenant_id=tenant_id,
                members=members,
                workspace_name=name,
                now=self._clock(),
            )
        log.info("slack.members.loaded", team_id=team_id, members=len(members))
        return True

    async def user_event(self, team_id: str, user: object) -> None:
        """team_join or user_change: list the user, or drop one deactivated or gone."""
        if not isinstance(user, Mapping):
            return
        fields = cast("Mapping[str, Any]", user)
        user_id = fields.get("id")
        if not isinstance(user_id, str) or fields.get("team_id") not in (None, team_id):
            return
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        entry = member_entry(fields, team_id=team_id)
        self._changes.note(tenant_id, user_id, None if entry is None else entry[1])
        async with self._runtime.sessionmaker.begin() as session:
            if entry is None:
                await members_store.delete_member(
                    session, tenant_id=tenant_id, platform_user_id=user_id
                )
            else:
                await members_store.upsert_member(
                    session,
                    tenant_id=tenant_id,
                    platform_user_id=user_id,
                    is_admin=entry[1],
                    now=self._clock(),
                )
