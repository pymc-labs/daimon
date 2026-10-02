"""One retry of a Bot Framework call Teams throttled, honouring Retry-After."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from daimon.core.teams_bot_framework import retry_throttled, throttle_delay

_REQUEST = httpx.Request("POST", "https://smba.trafficmanager.net/teams/v3/conversations/a")


def _status(code: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    response = httpx.Response(code, headers=headers, request=_REQUEST)
    return httpx.HTTPStatusError(str(code), request=_REQUEST, response=response)


@pytest.mark.parametrize(
    ("exc", "delay"),
    [
        (_status(429, {"Retry-After": "2"}), 2.0),
        (_status(429), 1.0),
        (_status(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 1.0),
        (_status(429, {"Retry-After": "60"}), None),
        (_status(503, {"Retry-After": "2"}), None),
        (httpx.ConnectError("down"), None),
    ],
    ids=["seconds", "missing", "http-date", "too-long", "not-429", "not-http"],
)
def test_throttle_delay(exc: BaseException, delay: float | None) -> None:
    assert throttle_delay(exc) == delay


async def test_a_throttled_call_runs_once_more_after_the_wait() -> None:
    call = AsyncMock(side_effect=[_status(429, {"Retry-After": "3"}), "sent"])
    with patch("daimon.core.teams_bot_framework.asyncio.sleep", AsyncMock()) as sleep:
        assert await retry_throttled(call) == "sent"
    sleep.assert_awaited_once_with(3.0)


async def test_a_second_429_or_any_other_error_is_raised() -> None:
    twice = AsyncMock(side_effect=[_status(429, {"Retry-After": "0"}), _status(429)])
    with pytest.raises(httpx.HTTPStatusError):
        await retry_throttled(twice)
    assert twice.await_count == 2, "one retry only"

    other = AsyncMock(side_effect=_status(500))
    with pytest.raises(httpx.HTTPStatusError):
        await retry_throttled(other)
    assert other.await_count == 1, "only a 429 is retried"
