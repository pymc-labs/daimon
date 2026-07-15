"""Tests for daimon.adapters.slack.admin.

Covers:
- _is_admin_signal pure decision: each of is_admin / is_owner / is_primary_owner
  independently resolves to True; all-False resolves to False.
- resolve_is_admin shell call: admin user → True; regular member → False;
  users.info failure (SlackApiError) → False with no re-raise (D-02 fail-closed).

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
    """resolve_is_admin returns False (fail-closed, D-02) when users.info fails.

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
async def test_resolve_is_admin_returns_true_when_dev_allow_all_set_without_calling_users_info() -> (
    None
):
    """dev_allow_all short-circuits to True before any users.info I/O.

    Testing-only escape hatch (DAIMON_SLACK__DEV_ALLOW_ALL_ADMIN). No users.info
    response is registered, so if the function attempted the call it would raise —
    reaching True proves the short-circuit skips the network entirely (and works
    even when the bot lacks the users:read scope).
    """
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock():
        client = AsyncWebClient(token="xoxb-test")
        result = await resolve_is_admin(client, user_id="U_NOBODY", dev_allow_all=True)

    assert result is True, "dev_allow_all=True should grant admin without consulting users.info"
