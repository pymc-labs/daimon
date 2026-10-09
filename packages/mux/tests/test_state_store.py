"""The protocol suite (C02, C03, C05, C07, C13, leases, isolation) on the memory store."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from mux.state.memory import MemoryStateData, MemoryStateStore
from mux.state.suite import CHECKS, StoreMaker


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_memory_store(check: Callable[[StoreMaker], Awaitable[None]]) -> None:
    data = MemoryStateData()
    await check(lambda: MemoryStateStore(data))
