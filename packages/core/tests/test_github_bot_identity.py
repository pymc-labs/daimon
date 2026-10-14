"""resolve_bot_identity: one GitHub lookup per slug, best-effort."""

from __future__ import annotations

import httpx
import pytest
from daimon.core import github_app_session
from daimon.core.github_app_auth import BotIdentity
from daimon.core.github_app_session import resolve_bot_identity


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.setattr(github_app_session, "_BOT_IDENTITIES", {})


def _client(calls: list[httpx.Request], response: httpx.Response) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_resolve_bot_identity_asks_github_once_per_slug() -> None:
    calls: list[httpx.Request] = []
    client = _client(calls, httpx.Response(200, json={"id": 7}))

    first = await resolve_bot_identity(client, slug="daimon-pymc")
    second = await resolve_bot_identity(client, slug="daimon-pymc")

    assert (
        first
        == second
        == BotIdentity(name="daimon-pymc[bot]", email="7+daimon-pymc[bot]@users.noreply.github.com")
    )
    assert len(calls) == 1, "the bot id never changes, so the second session reuses the first"


@pytest.mark.asyncio
async def test_resolve_bot_identity_without_a_slug_makes_no_request() -> None:
    calls: list[httpx.Request] = []

    assert await resolve_bot_identity(_client(calls, httpx.Response(500)), slug=None) is None
    assert calls == [], "no slug, no lookup"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, json={})])
async def test_resolve_bot_identity_degrades_to_none_and_retries_next_time(
    response: httpx.Response,
) -> None:
    calls: list[httpx.Request] = []
    client = _client(calls, response)

    assert await resolve_bot_identity(client, slug="daimon-pymc") is None
    assert await resolve_bot_identity(client, slug="daimon-pymc") is None
    assert len(calls) == 2, "a failure is not cached"
