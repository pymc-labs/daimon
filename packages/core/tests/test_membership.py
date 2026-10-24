"""Membership cache and REST transport use no live Discord calls."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.membership import CachedMembershipResolver, Membership


async def test_cache_expiry_live_and_tenant_isolation() -> None:
    now = [0.0]
    calls: list[str] = []
    facts = [Membership(is_member=True, role_ids=("123",))]

    async def fetch(tenant, guild, user):  # type: ignore[no-untyped-def]
        calls.append(guild)
        return facts[0]

    resolver = CachedMembershipResolver(fetch, clock=lambda: now[0])
    tenant = uuid4()
    assert (await resolver.resolve(tenant, "1", "2")).role_ids == ("123",)
    facts[0] = Membership(is_member=True)
    now[0] = 29.9
    assert (await resolver.resolve(tenant, "1", "2")).role_ids == ("123",)
    now[0] = 30
    assert (await resolver.resolve(tenant, "1", "2")).role_ids == ()
    await resolver.resolve(tenant, "1", "2", require_fresh=True)
    await resolver.resolve(uuid4(), "1", "2")
    await resolver.resolve(tenant, "3", "2")
    assert len(calls) == 5
    facts[0] = Membership(is_member=False)
    resolver.invalidate(tenant, "1", "2")
    await resolver.resolve(tenant, "1", "2")
    facts[0] = Membership(is_member=True)
    now[0] += 4.9
    assert (await resolver.resolve(tenant, "1", "2")).is_member is False
    now[0] += 0.1
    assert (await resolver.resolve(tenant, "1", "2")).is_member is True


async def test_single_flight_and_event_invalidates_pending_grant() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetch(tenant, guild, user):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return Membership(is_member=True, role_ids=("123",))

    tenant = uuid4()
    resolver = CachedMembershipResolver(fetch)
    first = asyncio.create_task(resolver.resolve(tenant, "1", "2", require_fresh=True))
    await entered.wait()
    second = asyncio.create_task(resolver.resolve(tenant, "1", "2", require_fresh=True))
    await asyncio.sleep(0)
    resolver.observe(tenant, "1", "2", Membership(is_member=False))
    release.set()
    assert (await first).is_member is None
    assert (await second).is_member is None
    assert calls == 1
    assert (await resolver.resolve(tenant, "1", "2")).is_member is False
    resolver.invalidate_guild("1")
    assert (await resolver.resolve(tenant, "1", "2")).is_member is True


async def test_lookup_errors_never_reuse_stale_evidence() -> None:
    now = [0.0]
    fail = [False]

    async def fetch(tenant, guild, user):  # type: ignore[no-untyped-def]
        if fail[0]:
            raise TimeoutError
        return Membership(is_member=True, is_admin=True)

    resolver = CachedMembershipResolver(fetch, clock=lambda: now[0])
    tenant = uuid4()
    assert (await resolver.resolve(tenant, "1", "2")).is_admin
    fail[0] = True
    now[0] = 30
    assert (await resolver.resolve(tenant, "1", "2")).is_member is None
    now[0] = 31
    assert not (await resolver.resolve(tenant, "1", "2")).is_admin


@pytest.mark.parametrize("permission", ["0", "8", "32"])
async def test_rest_reads_member_roles_and_live_permissions(permission: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bot test-only"
        if request.url.path.endswith("members/2"):
            return httpx.Response(200, json={"user": {"id": "2"}, "roles": ["1", "123"]})
        if request.url.path.endswith("/roles"):
            return httpx.Response(
                200,
                json=[{"id": "1", "permissions": "0"}, {"id": "123", "permissions": permission}],
            )
        return httpx.Response(200, json={"id": "1", "owner_id": "3"})

    resolver = DiscordRestMembershipResolver("test-only", transport=httpx.MockTransport(handle))
    facts = await resolver.resolve(uuid4(), "1", "2")
    assert facts.is_member is True
    assert facts.role_ids == ("123",)
    assert facts.is_admin == (permission != "0")


@pytest.mark.parametrize("status", [403, 404, 429, 500])
async def test_rest_absence_errors_and_bounded_rate_limits(status: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"retry_after": 0})

    resolver = DiscordRestMembershipResolver("test-only", transport=httpx.MockTransport(handle))
    facts = await resolver.resolve(uuid4(), "1", "2")
    assert facts.is_member is (False if status == 404 else None)
    assert not facts.is_admin
    assert not facts.role_ids


async def test_rest_rate_limit_waits_and_expired_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    now = [0.0]
    attempts = [0]
    fail = [False]
    pause = AsyncMock()
    monkeypatch.setattr("daimon.core.discord_membership.asyncio.sleep", pause)

    def handle(request: httpx.Request) -> httpx.Response:
        if fail[0]:
            raise httpx.ReadTimeout("offline timeout")
        if request.url.path.endswith("members/2"):
            attempts[0] += 1
            if attempts[0] == 1:
                return httpx.Response(429, json={"retry_after": 0.25})
            return httpx.Response(200, json={"user": {"id": "2"}, "roles": ["123"]})
        if request.url.path.endswith("/roles"):
            return httpx.Response(
                200, json=[{"id": "1", "permissions": "0"}, {"id": "123", "permissions": "0"}]
            )
        return httpx.Response(200, json={"id": "1", "owner_id": "3"})

    resolver = DiscordRestMembershipResolver(
        "test", transport=httpx.MockTransport(handle), clock=lambda: now[0]
    )
    tenant = uuid4()
    assert (await resolver.resolve(tenant, "1", "2")).role_ids == ("123",)
    pause.assert_awaited_once_with(0.25)
    fail[0] = True
    now[0] = 29
    assert (await resolver.resolve(tenant, "1", "2")).is_member is True
    now[0] = 30
    assert (await resolver.resolve(tenant, "1", "2")).is_member is None
    assert not (await resolver.resolve(tenant, "1", "2", require_fresh=True)).role_ids
