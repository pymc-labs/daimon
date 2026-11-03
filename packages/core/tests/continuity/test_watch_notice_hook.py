"""Watch migration notices share adapter scheduling without depending on GitHub health."""

from unittest.mock import AsyncMock

from daimon.core.github_connect_delivery import run_connect_notice_poller


async def test_existing_notice_poller_drains_watch_notices_even_when_other_notice_source_fails(
    monkeypatch,
):
    stopped = False

    async def drain():
        nonlocal stopped
        stopped = True

    github = AsyncMock(side_effect=RuntimeError("unavailable"))
    after = AsyncMock(side_effect=drain)
    monkeypatch.setattr("daimon.core.github_connect_delivery.poll_once", github)
    monkeypatch.setattr("daimon.core.github_connect_delivery.asyncio.sleep", AsyncMock())
    await run_connect_notice_poller(
        object(),
        platform="discord",
        deliver=AsyncMock(),
        should_stop=lambda: stopped,
        after_poll=after,
    )
    github.assert_awaited_once()
    after.assert_awaited_once()
