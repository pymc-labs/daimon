"""Shared fixtures for Slack adapter tests."""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from aioresponses import CallbackResult
from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.app import SlackApp
from daimon.testing.adapters.slack.notices import shown_text as shown_text
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from slack_sdk.web.async_client import AsyncWebClient

_SLACK_API_BASE = "https://slack.com/api"


# Default ok payload for the POST-JSON chat methods. Exported so tests that
# clear the defaults to stage failures can re-register the identical shape —
# the ts value is load-bearing (tests assert on it as status_ts).
CHAT_OK_PAYLOAD: dict[str, object] = {
    "ok": True,
    "ts": "1000000000.000001",
    "channel": "C_TEST",
}

# Slack API methods that use POST with JSON body (params in body, not query string).
# Exact URL match works for these since params are not in the URL.
_SLACK_POST_JSON_METHODS: tuple[str, ...] = (
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
            payload=CHAT_OK_PAYLOAD,
            repeat=True,
        )
    # auth.test carries the bot's own user id, mirroring the real API — the
    # mention gate resolves it once per workspace.
    mock.post(  # pyright: ignore[reportUnknownMemberType]
        f"{_SLACK_API_BASE}/auth.test",
        payload={"ok": True, "user_id": "U_BOT"},
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
        callback=_default_users_info,
        repeat=True,
    )


# The workspace hosting the app in the turn under test. The default users.info
# payload names it as the user's own workspace, so a turn's requester is a
# verified member unless a test overrides the payload; it is set by
# ``_track_host_team`` around each turn body.
_current_host_team_id = "T_TEST"


def _default_users_info(_url: object, **_kwargs: object) -> CallbackResult:
    return CallbackResult(
        payload={
            "ok": True,
            "user": {
                "id": getattr(_url, "query", {}).get("user", "U_TEST"),
                "deleted": False,
                "is_bot": False,
                "name": "tester",
                "team_id": _current_host_team_id,
                "is_admin": False,
                "is_owner": False,
                "is_primary_owner": False,
            },
        }
    )


def _tracking_host_team(
    method: Callable[..., Awaitable[None]],
) -> Callable[..., Awaitable[None]]:
    """Wrap a turn entry point so its ``team_id`` is the host in the users.info stub."""

    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> None:
        global _current_host_team_id
        _current_host_team_id = kwargs["team_id"]
        return await method(*args, **kwargs)

    return wrapper


@pytest.fixture(autouse=True)
def _track_host_team(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_run_thread_turn_observed_guarded", "_run_continuation_turn_observed"):
        monkeypatch.setattr(SlackApp, name, _tracking_host_team(getattr(SlackApp, name)))


@pytest.fixture(autouse=True)
def _host_member_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch tests send payloads that name no workspace; treat their user as a host member.

    The gate itself is covered in `test_unverified_interactions.py`, which overrides this.
    """

    async def verified(self: SlackApp, team_id: str, user_id: str) -> str:
        return "member"

    monkeypatch.setattr(SlackApp, "_host_check", verified)


@pytest.fixture
def fake_slack_web_client() -> Iterator[FakeSlackWebClient]:
    """Yield a real AsyncWebClient whose aiohttp calls are intercepted.

    Pre-registers canned ``ok=True`` JSON responses for the Slack Web API
    methods the listener uses.

    No method-level ``AsyncMock`` is used — this is a transport-level fake
    that exercises the real SDK request builder and response parser.
    """
    with AioResponsesMock() as mock:
        _register_slack_defaults(mock)
        yield FakeSlackWebClient(
            client=AsyncWebClient(token="xoxb-test"),
            mock=mock,
        )


@pytest.fixture(autouse=True)
def isolate_card_edits() -> Iterator[None]:
    """Card edits finish in the background; one test's stalled edit must not
    hold the next test's edit to a card with the same id."""
    from daimon.core.posted_controls.lifecycle import cancel_pending_card_edits

    yield
    cancel_pending_card_edits()


@pytest.fixture(autouse=True)
def isolate_agent_faces() -> Iterator[None]:
    """Creating an agent starts its face render in the background; one test's
    render must not write into the next test's database or back-off state."""
    from daimon.core.agent_identity import cancel_pending_agent_faces

    yield
    cancel_pending_agent_faces()


@pytest.fixture(autouse=True)
def _status_log_off() -> Iterator[None]:
    """Pin `turn.status_log` off, so the environment cannot change what a test sees."""
    from daimon.core.turn.status_log import status_log_override

    with status_log_override(False):
        yield


@pytest.fixture
def status_log_on(_status_log_off: None) -> Iterator[None]:
    """Run a test with `turn.status_log` on; every other test runs with it off."""
    from daimon.core.turn.status_log import status_log_override

    with status_log_override(True):
        yield
