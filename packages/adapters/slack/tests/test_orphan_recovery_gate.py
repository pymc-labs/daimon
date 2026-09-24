"""The first Slack turn waits until boot recovery has captured old markers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.app import SlackApp


async def test_turn_admission_waits_for_single_orphan_recovery_task() -> None:
    runtime = MagicMock()
    app = SlackApp(runtime=runtime)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _recover(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    sweep = AsyncMock(side_effect=_recover)
    with patch("daimon.adapters.slack.app.retire_orphaned_turns", new=sweep):
        first = app.start_orphan_recovery()
        second = app.start_orphan_recovery()
        waiter = asyncio.create_task(app._wait_for_orphan_recovery())  # pyright: ignore[reportPrivateUsage]
        await entered.wait()

        assert first is second, "repeated startup calls must not take a second marker snapshot"
        assert not waiter.done(), "turn admission must stay behind recovery"

        release.set()
        await waiter
        await first

    assert sweep.await_count == 1
