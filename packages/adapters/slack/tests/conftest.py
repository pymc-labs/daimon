"""Shared fixtures for Slack adapter tests."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator

import pytest
from aioresponses import aioresponses as AioResponsesMock
from daimon.testing.db import db_engine as db_engine  # noqa: F401
from daimon.testing.db import db_session as db_session  # noqa: F401
from daimon.testing.db import db_session_factory as db_session_factory  # noqa: F401
from slack_sdk.web.async_client import AsyncWebClient

_SLACK_API_BASE = "https://slack.com/api"

# Slack API methods that use POST with JSON body (params in body, not query string).
# Exact URL match works for these since params are not in the URL.
_SLACK_POST_JSON_METHODS: tuple[str, ...] = (
    "auth.test",
    "chat.postMessage",
    "chat.update",
    "chat.postEphemeral",
)

# views.open / views.update / views.push use POST with JSON body but need a
# view-bearing payload so handlers can read resp["view"]["id"].
_SLACK_VIEWS_METHODS: tuple[str, ...] = (
    "views.open",
    "views.update",
    "views.push",
)

# reactions.add uses POST but sends params as query string (params=kwargs in the SDK),
# so the URL has ?channel=...&name=...&timestamp=... and an exact-URL match fails.
_REACTIONS_ADD_PATTERN = re.compile(r"https://slack\.com/api/reactions\.add.*")

# conversations.replies uses GET (http_verb="GET" in the SDK).
# Pattern matches regardless of query params (aioresponses GET requests carry
# params in the URL, so exact-string matching fails with dynamic ts/oldest values).
_CONVERSATIONS_REPLIES_PATTERN = re.compile(r"https://slack\.com/api/conversations\.replies.*")

# users.info uses GET with user= as a query param.
_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")


@dataclasses.dataclass(frozen=True)
class FakeSlackWebClient:
    """Transport-level AsyncWebClient fake backed by aioresponses.

    A real ``AsyncWebClient`` instance whose underlying aiohttp calls are
    intercepted by an active ``aioresponses`` context — no method-level
    ``AsyncMock`` on ``client.*`` is used.  This mirrors the
    ``build_stub_anthropic`` / ``httpx.MockTransport`` discipline for the
    Anthropic SDK, adapted for Slack's aiohttp transport.

    Attributes:
        client: Real ``AsyncWebClient(token="xoxb-test")``.  Use for API
                calls in tests; the SDK's own request building and response
                parsing run for every call.
        mock:   Active ``aioresponses`` context.  Inspect ``mock.requests``
                (a dict keyed by ``(method, yarl.URL)``) to assert what was
                sent.  Register per-test responses with ``mock.post(...)``.
    """

    client: AsyncWebClient
    mock: AioResponsesMock  # aioresponses.core.aioresponses — ships py.typed


def _register_slack_defaults(mock: AioResponsesMock) -> None:
    """Register canned ok=True responses for the Slack API methods.

    POST methods are registered with exact URLs; GET / query-param endpoints
    use re.compile patterns to handle dynamic query-string variation.

    views.open / views.update / views.push get a view-bearing payload
    (``{"ok": True, "view": {"id": "V_TEST", "hash": "H_TEST"}}``) so
    handlers can read ``resp["view"]["id"]`` in tests.

    users.info gets a default non-admin payload; per-test overrides can
    re-register with ``mock.get(_USERS_INFO_PATTERN, payload=...)``.
    """
    for method in _SLACK_POST_JSON_METHODS:
        mock.post(  # pyright: ignore[reportUnknownMemberType]  # aioresponses url param is Pattern[Unknown]
            f"{_SLACK_API_BASE}/{method}",
            payload={"ok": True, "ts": "1000000000.000001", "channel": "C_TEST"},
            repeat=True,
        )
    # views methods return a view object so handlers can read resp["view"]["id"].
    for method in _SLACK_VIEWS_METHODS:
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/{method}",
            payload={"ok": True, "view": {"id": "V_TEST", "hash": "H_TEST"}},
            repeat=True,
        )
    # reactions.add sends params as query string (params=kwargs in SDK), not JSON body.
    mock.post(  # pyright: ignore[reportUnknownMemberType]
        _REACTIONS_ADD_PATTERN,
        payload={"ok": True},
        repeat=True,
    )
    # conversations.replies is GET with params in the query string.
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        _CONVERSATIONS_REPLIES_PATTERN,
        payload={"ok": True, "messages": [], "has_more": False},
        repeat=True,
    )
    # users.info is GET with user= in the query string; default payload is a
    # non-admin member so tests that don't override get the fail-closed baseline.
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        _USERS_INFO_PATTERN,
        payload={
            "ok": True,
            "user": {
                "id": "U_TEST",
                "name": "tester",
                "is_admin": False,
                "is_owner": False,
                "is_primary_owner": False,
            },
        },
        repeat=True,
    )


@pytest.fixture
def fake_slack_web_client() -> Iterator[FakeSlackWebClient]:
    """Yield a real AsyncWebClient whose aiohttp calls are intercepted.

    Pre-registers canned ``ok=True`` JSON responses for the Slack Web API
    methods the Phase 80 listener uses (see ``_SLACK_PHASE80_METHODS``).

    No method-level ``AsyncMock`` is used — this is a transport-level fake
    that exercises the real SDK request builder and response parser.
    """
    with AioResponsesMock() as mock:
        _register_slack_defaults(mock)
        yield FakeSlackWebClient(
            client=AsyncWebClient(token="xoxb-test"),
            mock=mock,
        )
