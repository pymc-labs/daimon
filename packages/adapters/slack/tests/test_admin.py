"""Tests for daimon.adapters.slack.admin.

Covers:
- _is_admin_signal pure decision: each of is_admin / is_owner / is_primary_owner
  independently resolves to True; all-False resolves to False.
- resolve_is_admin shell call: admin user → True; regular member → False;
  users.info failure → False with no re-raise (fail-closed), for BOTH an
  API-level SlackApiError and a transport-level aiohttp error.

Transport-level fakes only (aioresponses, via fresh contexts per test).
No AsyncMock on client.* methods.
"""

from __future__ import annotations

import re

import pytest
from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.admin import _is_admin_signal, resolve_is_admin

_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")


# ---------------------------------------------------------------------------
# Pure decision: _is_admin_signal
# ---------------------------------------------------------------------------


def test_is_admin_signal_returns_true_when_is_admin_flag_set() -> None:
    result = _is_admin_signal({"is_admin": True, "is_owner": False, "is_primary_owner": False})
    assert result is True, "_is_admin_signal should return True when is_admin is set"


def test_is_admin_signal_returns_true_when_is_owner_flag_set() -> None:
    result = _is_admin_signal({"is_admin": False, "is_owner": True, "is_primary_owner": False})
    assert result is True, "_is_admin_signal should return True when is_owner is set"


def test_is_admin_signal_returns_true_when_is_primary_owner_flag_set() -> None:
    result = _is_admin_signal({"is_admin": False, "is_owner": False, "is_primary_owner": True})
    assert result is True, "_is_admin_signal should return True when is_primary_owner is set"


def test_is_admin_signal_returns_false_when_no_admin_flags_set() -> None:
    result = _is_admin_signal({"is_admin": False, "is_owner": False, "is_primary_owner": False})
    assert result is False, "_is_admin_signal should return False when no admin flag is set"


def test_is_admin_signal_returns_false_when_user_dict_is_empty() -> None:
    result = _is_admin_signal({})
    assert result is False, "_is_admin_signal should return False for an empty user dict"


# ---------------------------------------------------------------------------
# Shell call: resolve_is_admin
# ---------------------------------------------------------------------------

# Each test below constructs its own aioresponses context so the order of
# registered responses is fully controlled (aioresponses matches in insertion
# order — fighting a repeat=True default from a shared fixture is fragile).


@pytest.mark.asyncio
async def test_resolve_is_admin_returns_true_when_users_info_reports_admin() -> None:
    """resolve_is_admin returns True when users.info says the user is an admin."""
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload={
                "ok": True,
                "user": {
                    "id": "U_ADMIN",
                    "name": "admin_user",
                    "is_admin": True,
                    "is_owner": False,
                    "is_primary_owner": False,
                },
            },
        )
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_is_admin(client, user_id="U_ADMIN")

    assert result is True, "resolve_is_admin should return True for a workspace admin"


@pytest.mark.asyncio
async def test_resolve_is_admin_returns_false_when_users_info_reports_regular_member() -> None:
    """resolve_is_admin returns False for a regular (non-admin) member."""
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock() as mock:
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
        )
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_is_admin(client, user_id="U_TEST")

    assert result is False, "resolve_is_admin should return False for a regular member"


@pytest.mark.asyncio
async def test_resolve_is_admin_returns_false_and_does_not_raise_on_slack_api_error() -> None:
    """resolve_is_admin returns False (fail-closed) when users.info fails.

    The SlackApiError must not propagate — the adapter boundary absorbs it.
    """
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload={"ok": False, "error": "ratelimited"},
        )
        client = AsyncWebClient(token="xoxb-test")
        # Must not raise; must return False.
        result = await resolve_is_admin(client, user_id="U_TEST")

    assert result is False, "resolve_is_admin should return False (fail-closed) on SlackApiError"


@pytest.mark.asyncio
async def test_resolve_admin_status_returns_none_and_does_not_raise_on_transport_error() -> None:
    """A connection failure degrades to None instead of killing the caller.

    aiohttp's ClientConnectionError is NOT a SlackApiError subclass, so a catch
    of SlackApiError alone lets it escape. Every Slack mention-turn calls this
    once, so an escaping transport error takes the whole turn down on a
    transient network blip.
    """
    import aiohttp
    from daimon.adapters.slack.admin import resolve_admin_status
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            exception=aiohttp.ClientConnectionError("connection refused"),
        )
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_admin_status(client, user_id="U_TEST")

    assert result is None, (
        "resolve_admin_status should return None (lookup failed) on a transport error, not raise"
    )


@pytest.mark.asyncio
async def test_resolve_is_admin_returns_false_and_does_not_raise_on_transport_error() -> None:
    """The fail-closed wrapper collapses a transport failure to False."""
    import aiohttp
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            exception=aiohttp.ClientConnectionError("connection refused"),
        )
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_is_admin(client, user_id="U_TEST")

    assert result is False, (
        "resolve_is_admin should return False (fail-closed) on a transport error"
    )
