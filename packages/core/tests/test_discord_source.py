"""Routine source verification uses guild-bound Discord REST data."""

import httpx
import pytest
from daimon.core.discord_membership import verify_discord_source
from daimon.core.errors import StoreError


@pytest.mark.parametrize("kind", [0, 5, 10, 11, 12])
async def test_supported_source_shapes_normalize_thread_parent(kind):
    calls = []

    def respond(request):
        key = request.url.path.rsplit("/", 1)[1]
        calls.append(key)
        return httpx.Response(
            200,
            json={
                "id": key,
                "guild_id": "123",
                "type": kind if key == "789" else 15,
                "parent_id": "456",
            },
        )

    result = await verify_discord_source(
        "offline", guild_id="123", channel_id="789", transport=httpx.MockTransport(respond)
    )
    assert result == (("789", None) if kind in (0, 5) else ("456", "789"))
    assert calls == (["789"] if kind in (0, 5) else ["789", "456"])


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "forbidden",
        "unavailable",
        "foreign",
        "unsupported",
        "no_parent",
        "parent_missing",
        "parent_foreign",
        "parent_unsupported",
    ],
)
async def test_unverified_sources_fail_closed(case):
    def respond(request):
        key = request.url.path.rsplit("/", 1)[1]
        status = {"missing": 404, "forbidden": 403, "unavailable": 503}.get(case, 200)
        if key == "456" and case == "parent_missing":
            status = 404
        return httpx.Response(
            status,
            json={
                "id": key,
                "guild_id": "999"
                if case == "foreign" or (key == "456" and case == "parent_foreign")
                else "123",
                "type": 2
                if case == "unsupported" or (key == "456" and case == "parent_unsupported")
                else (11 if key == "789" else 0),
                "parent_id": None if case == "no_parent" else "456",
            },
        )

    with pytest.raises(StoreError):
        await verify_discord_source(
            "offline", guild_id="123", channel_id="789", transport=httpx.MockTransport(respond)
        )
