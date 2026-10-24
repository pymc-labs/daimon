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
from daimon.adapters.slack.admin import (
    SlackRequester,
    _is_admin_signal,  # pyright: ignore[reportPrivateUsage]
    resolve_is_admin,
    resolve_requester,
)

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
                    "team_id": "T_HOST",
                    "is_admin": True,
                    "is_owner": False,
                    "is_primary_owner": False,
                },
            },
        )
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_is_admin(client, user_id="U_ADMIN", host_team_id="T_HOST")

    assert result is True, "resolve_is_admin should return True for a workspace admin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        pytest.param({"team_id": "T_OTHER", "is_admin": True}, id="admin-of-another-workspace"),
        pytest.param({"team_id": "T_OTHER", "is_primary_owner": True}, id="owner-of-another"),
        pytest.param({"team_id": "T_HOST", "is_stranger": True, "is_admin": True}, id="stranger"),
        pytest.param({"is_admin": True}, id="no-team-id"),
    ],
)
async def test_resolve_is_admin_is_false_for_an_external_admin(user: dict[str, object]) -> None:
    """An admin signal from another workspace never makes someone an admin here."""
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload={"ok": True, "user": {"id": "U_EXTERNAL", "name": "visitor", **user}},
        )
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_is_admin(client, user_id="U_EXTERNAL", host_team_id="T_HOST")

    assert result is False


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
                    "team_id": "T_HOST",
                    "is_admin": False,
                    "is_owner": False,
                    "is_primary_owner": False,
                },
            },
        )
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_is_admin(client, user_id="U_TEST", host_team_id="T_HOST")

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
        result = await resolve_is_admin(client, user_id="U_TEST", host_team_id="T_HOST")

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
        result = await resolve_admin_status(client, user_id="U_TEST", host_team_id="T_HOST")

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
        result = await resolve_is_admin(client, user_id="U_TEST", host_team_id="T_HOST")

    assert result is False, (
        "resolve_is_admin should return False (fail-closed) on a transport error"
    )


async def _requester_for(
    user: dict[str, object], *, host_team_id: str = "T_HOST"
) -> SlackRequester | None:
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN, payload={"ok": True, "user": user}
        )
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_requester(client, user_id="U_TEST", host_team_id=host_team_id)
        requests = sum(len(r) for r in mock.requests.values())  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportUnknownArgumentType]
    assert requests == 1, "resolve_requester must make exactly one users.info call"
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (
            {"name": "tester", "profile": {"display_name": "Dee", "real_name": "Dee Real"}},
            "Dee",
        ),
        (
            {"name": "tester", "profile": {"display_name": "  ", "real_name": "Dee Real"}},
            "Dee Real",
        ),
        ({"name": "tester", "profile": {"display_name": "", "real_name": ""}}, "tester"),
        ({"name": "tester"}, "tester"),
        ({"profile": {}}, None),
        ({}, None),
        ({"profile": {"display_name": "x" * 200}}, "x" * 80),
    ],
)
async def test_resolve_requester_picks_display_name_then_real_name_then_name(
    user: dict[str, object], expected: str | None
) -> None:
    result = await _requester_for({**user, "team_id": "T_HOST", "is_admin": True})
    assert result == SlackRequester(
        is_admin=True,
        display_name=expected,
        team_id="T_HOST",
        is_stranger=False,
        host_team_id="T_HOST",
    )


@pytest.mark.asyncio
async def test_resolve_requester_returns_none_when_the_lookup_fails() -> None:
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN, payload={"ok": False, "error": "ratelimited"}
        )
        result = await resolve_requester(
            AsyncWebClient(token="xoxb-test"), user_id="U_TEST", host_team_id="T_HOST"
        )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["is_admin", "is_owner", "is_primary_owner"])
async def test_resolve_requester_external_team_is_never_admin_or_host_member(flag: str) -> None:
    result = await _requester_for({"team_id": "T_OTHER", flag: True})
    assert result is not None
    assert result.is_admin is False
    assert result.is_host_member is False


@pytest.mark.asyncio
async def test_resolve_requester_stranger_is_not_a_host_member_even_with_host_team_id() -> None:
    result = await _requester_for({"team_id": "T_HOST", "is_stranger": True, "is_admin": True})
    assert result is not None
    assert result.is_admin is False
    assert result.is_host_member is False


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [{"is_admin": True}, {"team_id": "", "is_admin": True}])
async def test_resolve_requester_missing_team_id_is_not_verified(user: dict[str, object]) -> None:
    result = await _requester_for(user)
    assert result is not None
    assert result.team_id is None
    assert result.is_admin is False
    assert result.is_host_member is False


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["is_admin", "is_owner", "is_primary_owner"])
async def test_resolve_requester_host_admin_is_admin_and_host_member(flag: str) -> None:
    result = await _requester_for({"team_id": "T_HOST", flag: True})
    assert result is not None
    assert result.is_admin is True
    assert result.is_host_member is True


@pytest.mark.asyncio
async def test_resolve_requester_host_plain_member_is_member_not_admin() -> None:
    result = await _requester_for({"team_id": "T_HOST"})
    assert result is not None
    assert (result.is_admin, result.is_host_member) == (False, True)
