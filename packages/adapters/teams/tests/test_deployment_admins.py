"""Deployment admins on Teams: the admin team's owners, and nobody else."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from dataclasses import MISSING, fields
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from daimon.adapters.teams.budget_notice import with_budget_notifier
from daimon.adapters.teams.deployment_admins import (
    STALE_OWNERS_S,
    AdminTeamReads,
    admin_team_owners,
    boot_owner_ids,
    demoting_owner_reads,
    deployment_admin_ids,
    deployment_role,
    is_deployment_admin,
    live_admin_ids,
)
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.team_owners import team_owner_lookup
from daimon.core.config import TeamsSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.native_admins import GroupLookupFailed, GroupMembersCache
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.turn.deps import TurnDeps
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AAD_OBJECT_ID, ENTRA_TENANT_ID, OTHER_AAD_OBJECT_ID, teams_settings

TEAM = "0f2c8a51-7d3e-4b9a-8c61-2e5f4a9b7c10"
REMOVED_OWNER = str(uuid.UUID(int=4))


def _settings(*, team: str | None = TEAM) -> TeamsSettings:
    return teams_settings().model_copy(update={"admin_team_id": team})


class _Owners:
    """Graph's owner read: the current owners, or a failure; counts its reads."""

    def __init__(self, *owners: str) -> None:
        self.owners = frozenset(owners)
        self.failure: str | None = None
        self.reads = 0

    async def __call__(self, group_id: str) -> frozenset[str]:
        assert group_id == TEAM, "only the admin team is read"
        self.reads += 1
        if self.failure is not None:
            raise GroupLookupFailed(self.failure)
        return self.owners


def _runtime(owners: _Owners, *, clock: list[float] | None = None) -> MagicMock:
    """The runtime as `demoting_owner_reads` wires it, demoting no one."""
    now = clock if clock is not None else [0.0]
    reads = AdminTeamReads(owners, team_id=TEAM, after_read=lambda _: None, clock=lambda: now[0])
    return MagicMock(team_owners=reads, group_members=GroupMembersCache(clock=lambda: now[0]))


async def _is_admin(
    runtime: MagicMock, settings: TeamsSettings, user_id: str, **kwargs: Any
) -> bool:
    return await is_deployment_admin(
        settings, admin_team_owners(runtime), user_id=user_id, **kwargs
    )


async def test_an_owner_of_the_admin_team_is_an_admin_and_a_member_is_not() -> None:
    runtime = _runtime(_Owners(AAD_OBJECT_ID))
    assert await _is_admin(runtime, _settings(), AAD_OBJECT_ID.upper()), "an owner administers"
    assert not await _is_admin(runtime, _settings(), OTHER_AAD_OBJECT_ID), "a non-owner does not"


async def test_a_failed_owner_read_grants_nothing_and_logs_once() -> None:
    owners = _Owners(AAD_OBJECT_ID)
    owners.failure = "http error (status 403)"
    runtime = _runtime(owners)
    settings = _settings()
    with structlog.testing.capture_logs() as logs:
        assert not await _is_admin(runtime, settings, AAD_OBJECT_ID), "a failed read fails closed"
        assert not await _is_admin(runtime, settings, AAD_OBJECT_ID), "a cached failure too"
    failed = [log for log in logs if log["event"] == "teams.admin_team.lookup_failed"]
    assert len(failed) == 1, f"one log per failed read, not per check: {logs}"
    assert failed[0]["reason"] == "http error (status 403)", "reason and status only"
    assert owners.reads == 1, "the failure is cached, not read again by every check"


async def test_boot_keeps_only_the_owners_and_without_a_team_nobody() -> None:
    runtime = _runtime(_Owners(AAD_OBJECT_ID))
    assert await boot_owner_ids(runtime, _settings()) == {AAD_OBJECT_ID}, "the owners stay"
    no_team = await boot_owner_ids(runtime, _settings(team=None))
    assert no_team == frozenset(), "no admin team: every stored admin is demoted"


async def test_with_neither_set_nobody_is_an_admin() -> None:
    owners = _Owners(AAD_OBJECT_ID)
    runtime = _runtime(owners)
    settings = _settings(team=None)
    assert not await _is_admin(runtime, settings, AAD_OBJECT_ID), "no admin team: no admin"
    assert await deployment_admin_ids(settings, admin_team_owners(runtime)) == ()
    assert owners.reads == 0, "no admin team is read"


async def test_a_removed_owner_loses_admin_when_the_cache_expires() -> None:
    owners, clock = _Owners(AAD_OBJECT_ID), [0.0]
    runtime = _runtime(owners, clock=clock)
    assert await _is_admin(runtime, _settings(), AAD_OBJECT_ID)
    owners.owners = frozenset()
    assert await _is_admin(runtime, _settings(), AAD_OBJECT_ID), "cached for a minute"
    clock[0] = 61.0
    assert not await _is_admin(runtime, _settings(), AAD_OBJECT_ID), "lapsed once it expires"


async def test_notices_to_the_admins_reach_the_owners() -> None:
    runtime = _runtime(_Owners(AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID))
    runtime.settings.teams = _settings()
    expected = tuple(sorted({AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID}))
    assert await live_admin_ids(runtime)() == expected, "every owner, sorted"
    runtime.settings.teams = _settings(team=None)
    assert await live_admin_ids(runtime)() == (), "nobody to tell, as before"


def _required(cls: Any, **given: object) -> dict[str, object]:
    needed = [f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING]
    return {name: MagicMock() for name in needed} | given


async def test_the_budget_notice_reads_the_live_admins() -> None:
    settings = MagicMock()
    settings.teams = _settings()
    runtime = TeamsRuntime(
        **_required(
            TeamsRuntime,
            settings=settings,
            turn_deps=TurnDeps(**_required(TurnDeps)),
            team_owners=_Owners(AAD_OBJECT_ID),
            group_members=GroupMembersCache(),
        )
    )
    server_admins = with_budget_notifier(runtime, MagicMock()).turn_deps.server_admins
    assert server_admins is not None, "a notifier comes with the live admins"
    assert tuple(await server_admins()) == (AAD_OBJECT_ID,), "the admin team's owners"


class _Background:
    """`spawn`: runs each demotion as a task the test can wait for."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []

    def __call__(self, coro: Coroutine[Any, Any, None]) -> None:
        self.tasks.append(asyncio.create_task(coro))


async def _stored_admins(
    db_factory: async_sessionmaker[AsyncSession], *user_ids: str
) -> dict[str, uuid.UUID]:
    tenant = await provision_tenant(db_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    accounts: dict[str, uuid.UUID] = {}
    async with db_factory.begin() as session:
        for user_id in user_ids:
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant.tenant_id, platform="teams", external_id=user_id
            )
            await set_role(session, principal.account_id, Role.ADMIN)
            accounts[user_id] = principal.account_id
    return accounts


async def _roles(
    db_factory: async_sessionmaker[AsyncSession], accounts: dict[str, uuid.UUID]
) -> dict[str, Role | None]:
    async with db_factory() as session:
        return {
            user: getattr(await get_account(session, account), "role", None)
            for user, account in accounts.items()
        }


async def test_each_owner_read_demotes_a_removed_owners_stored_role_in_the_background(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Routines and tools read the stored role, so it follows the team within the minute."""
    accounts = await _stored_admins(
        db_session_factory, AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID, REMOVED_OWNER
    )
    graph, clock, background = _Owners(AAD_OBJECT_ID), [0.0], _Background()
    runtime = _runtime(graph, clock=clock)
    runtime.settings.teams = settings = _settings()
    runtime.sessionmaker = db_session_factory
    runtime.team_owners = demoting_owner_reads(runtime, graph, in_background=background)

    assert await _is_admin(runtime, settings, AAD_OBJECT_ID), "an owner administers"
    assert await _is_admin(runtime, settings, AAD_OBJECT_ID), "a cached read, no new demotion"
    assert len(background.tasks) == 1, "one demotion per read, off the caller's path"
    await asyncio.gather(*background.tasks)
    assert await _roles(db_session_factory, accounts) == {
        AAD_OBJECT_ID: Role.ADMIN,
        OTHER_AAD_OBJECT_ID: Role.USER,
        REMOVED_OWNER: Role.USER,
    }, "everyone not an owner is demoted"

    graph.failure, clock[0] = "http error (status 503)", 61.0
    await _is_admin(runtime, settings, AAD_OBJECT_ID)
    assert len(background.tasks) == 1, "a failed read demotes no one"


async def test_an_outage_keeps_the_last_owners_for_an_hour_then_nobody_until_a_good_read() -> None:
    """Graph failing, or the app removed from the team, is not an instant lockout."""
    graph, clock = _Owners(AAD_OBJECT_ID), [0.0]
    runtime = _runtime(graph, clock=clock)
    settings = _settings()
    assert await _is_admin(runtime, settings, AAD_OBJECT_ID), "read while Graph answers"
    graph.failure, clock[0] = "http error (status 403)", 61.0
    assert await _is_admin(runtime, settings, AAD_OBJECT_ID), "the last read stands in"
    clock[0] = STALE_OWNERS_S
    owners = admin_team_owners(runtime)
    role = await deployment_role(settings, owners, user_id=AAD_OBJECT_ID)
    assert role is None, "past the hour, ownership is unknown: no role to record"
    assert await deployment_admin_ids(settings, owners) == (), "nobody is an admin"
    graph.failure, clock[0] = None, STALE_OWNERS_S + 61.0
    assert await _is_admin(runtime, settings, AAD_OBJECT_ID), "the next good read restores them"


async def test_boot_demotes_no_one_when_the_owners_cannot_be_read() -> None:
    graph = _Owners(AAD_OBJECT_ID)
    graph.failure = "http error (status 503)"
    boot = await boot_owner_ids(_runtime(graph), _settings())
    assert boot is None, "a failed read must not take every owner's stored role"


async def test_a_failed_read_is_logged_even_when_a_channel_grant_made_it() -> None:
    """The grant path shares the cache key, so it may be the one that reads the admin team."""
    graph = _Owners(AAD_OBJECT_ID)
    graph.failure = "http error (status 403)"
    runtime = _runtime(graph)
    grant = team_owner_lookup(runtime)
    assert grant is not None
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(GroupLookupFailed):
            await grant(TEAM)  # a native team-owner lookup
        assert not await _is_admin(runtime, _settings(), AAD_OBJECT_ID), "fails closed"
    failed = [log for log in logs if log["event"] == "teams.admin_team.lookup_failed"]
    assert len(failed) == 1 and graph.reads == 1, f"one read, logged once: {logs}"
