"""The remembered answer to whether a node's server offers skills."""

from __future__ import annotations

import pytest
from daimon.core.server_skills.node import SERVES_TTL_S, ServesCache


async def test_serves_asks_once_within_the_ttl_and_again_after_it() -> None:
    now = [100.0]
    cache = ServesCache(clock=lambda: now[0])
    asked: list[str] = []

    async def ask() -> bool:
        asked.append("ask")
        return len(asked) == 1

    assert await cache.serves("data", ask), "the first answer"
    now[0] += SERVES_TTL_S - 1
    assert await cache.serves("data", ask), "remembered inside the window"
    now[0] += 2
    assert not await cache.serves("data", ask), "asked again once it lapsed"
    assert len(asked) == 2, "two asks in all"


async def test_serves_does_not_remember_a_failed_ask() -> None:
    cache = ServesCache()

    async def fail() -> bool:
        raise OSError("down")

    async def ok() -> bool:
        return True

    with pytest.raises(OSError, match="down"):
        await cache.serves("data", fail)
    assert await cache.serves("data", ok), "the next call asks afresh"
