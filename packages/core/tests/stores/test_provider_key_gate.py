"""The gate's shared state: rate-limit waits and running-turn leases per provider key."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.config import ProviderLimitsSettings
from daimon.core.provider_key_gate import ProviderKey, ProviderKeyGate, lease_ttl
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = ProviderKey("openai", "digest")
START = datetime(2026, 11, 1, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def gate_for(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, **limits: float
) -> ProviderKeyGate:
    async def sleep(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)

    return ProviderKeyGate(
        sessions, ProviderLimitsSettings(wait_s=0, **limits), clock=clock, sleep=sleep
    )


async def test_a_rate_limit_cools_one_key_and_model_until_its_wait_passes(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    clock = Clock()
    gate = gate_for(db_session_factory, clock)
    await gate.record_cooldown(KEY, "gpt", 30)
    await gate.record_cooldown(KEY, "gpt", 5)
    assert await gate.cooling_until(KEY, "gpt") == START + timedelta(seconds=30), (
        "a shorter wait never shortens the one recorded"
    )
    assert await gate.cooling_until(KEY, "other-model") is None
    assert await gate.cooling_until(ProviderKey("openai", "other-key"), "gpt") is None
    clock.now += timedelta(seconds=31)
    assert await gate.cooling_until(KEY, "gpt") is None, "the key serves again once it passes"


async def test_the_key_and_each_workspace_are_capped_and_a_lease_expires(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    clock = Clock()
    gate = gate_for(db_session_factory, clock, key_turns=3, tenant_turns=2)
    first, second = (await make_tenant(db_session)).id, (await make_tenant(db_session)).id
    held = [await gate.acquire(KEY, first), await gate.acquire(KEY, first)]
    assert None not in held
    assert await gate.acquire(KEY, first) is None, "a workspace holds at most its share"
    assert await gate.acquire(KEY, second) is not None
    assert await gate.acquire(KEY, second) is None, "the key holds at most its cap"
    assert await gate.acquire(ProviderKey("openai", "other-key"), second) is not None
    assert held[0] is not None
    await gate.release(held[0])
    assert await gate.acquire(KEY, second) is not None, "a released slot is free at once"
    clock.now += lease_ttl() + timedelta(seconds=1)
    assert await gate.acquire(KEY, first) is not None, "a lease nobody released expires"


async def test_a_turn_at_the_cap_waits_for_a_slot_then_refuses(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    clock, slept = Clock(), []
    tenant = (await make_tenant(db_session)).id
    gate = gate_for(db_session_factory, clock, key_turns=1, tenant_turns=1)
    lease = await gate.acquire(KEY, tenant)
    assert lease is not None

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            await gate.release(lease)

    waiting = ProviderKeyGate(
        db_session_factory,
        ProviderLimitsSettings(key_turns=1, tenant_turns=1, wait_s=20),
        clock=clock,
        sleep=sleep,
    )
    assert await waiting.acquire(KEY, tenant) is not None, "a slot freed while it waited"
    slept.clear()
    assert await waiting.acquire(KEY, tenant) is None, "no slot freed within the wait"
    assert sum(slept) == 20
