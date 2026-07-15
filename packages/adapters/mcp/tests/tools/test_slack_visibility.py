"""Policy tests for tools/slack/_visibility.py."""

from __future__ import annotations

import re

import pytest
from aioresponses import aioresponses
from daimon.adapters.mcp.tools.slack._visibility import (  # pyright: ignore[reportPrivateUsage]
    _is_guest,
    _is_user_in_channel,
    check_channel_access,
    is_channel_visible,
    map_slack_api_error,
)
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

_MEMBERS_PATTERN = re.compile(r"https://slack\.com/api/conversations\.members.*")
_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")


@pytest.mark.parametrize(
    ("is_im_or_mpim", "is_private", "is_guest", "is_member", "expected"),
    [
        (True, False, False, True, False),  # im/mpim always denied
        (False, False, False, False, True),  # public + full member: no membership needed
        (False, False, True, False, False),  # public + guest + not in channel: denied
        (False, False, True, True, True),  # public + guest + in channel: allowed
        (False, True, False, False, False),  # private + not in channel: denied
        (False, True, False, True, True),  # private + in channel: allowed
        (False, True, True, True, True),  # private + guest + in channel: allowed
    ],
)
def test_is_channel_visible_matrix(
    is_im_or_mpim: bool, is_private: bool, is_guest: bool, is_member: bool, expected: bool
) -> None:
    assert (
        is_channel_visible(
            is_im_or_mpim=is_im_or_mpim,
            is_private=is_private,
            is_guest=is_guest,
            is_member=is_member,
        )
        == expected
    ), "visibility decision must mirror Slack's own channel visibility semantics"


async def test_is_user_in_channel_paginates_to_second_page() -> None:
    client = AsyncWebClient(token="xoxb-test")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _MEMBERS_PATTERN,
            payload={
                "ok": True,
                "members": ["U_OTHER1"],
                "response_metadata": {"next_cursor": "PAGE2"},
            },
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _MEMBERS_PATTERN,
            payload={"ok": True, "members": ["U_CALLER"], "response_metadata": {"next_cursor": ""}},
        )
        found = await _is_user_in_channel(client, channel_id="C1", user_id="U_CALLER")
    assert found, "membership check must follow next_cursor pagination"


async def test_is_guest_true_for_restricted_user() -> None:
    client = AsyncWebClient(token="xoxb-test")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload={"ok": True, "user": {"id": "U_G", "is_restricted": True}},
        )
        assert await _is_guest(client, user_id="U_G"), "is_restricted must classify as guest"


async def test_check_channel_access_public_full_member_makes_no_membership_call() -> None:
    client = AsyncWebClient(token="xoxb-test")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload={"ok": True, "user": {"id": "U_CALLER", "is_restricted": False}},
        )
        # No conversations.members registered — a members call would error the test.
        await check_channel_access(
            client, channel={"id": "C1", "is_private": False}, user_id="U_CALLER"
        )


async def test_check_channel_access_private_nonmember_denied() -> None:
    client = AsyncWebClient(token="xoxb-test")
    with aioresponses() as m:
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload={"ok": True, "user": {"id": "U_CALLER", "is_restricted": False}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _MEMBERS_PATTERN,
            payload={"ok": True, "members": ["U_SOMEONE_ELSE"]},
        )
        with pytest.raises(ToolError, match="missing channel access"):
            await check_channel_access(
                client, channel={"id": "C_PRIV", "is_private": True}, user_id="U_CALLER"
            )


async def test_check_channel_access_im_denied_without_any_api_call() -> None:
    client = AsyncWebClient(token="xoxb-test")
    with pytest.raises(ToolError, match="missing channel access"):
        await check_channel_access(client, channel={"id": "D1", "is_im": True}, user_id="U_CALLER")


def _slack_api_error(code: str) -> SlackApiError:
    return SlackApiError(message=code, response={"ok": False, "error": code})


def test_map_slack_api_error_missing_scope_names_the_fix() -> None:
    mapped = map_slack_api_error(_slack_api_error("missing_scope"))
    assert mapped is not None and "re-install daimon" in str(mapped), (
        "missing_scope must produce actionable reinstall guidance"
    )


def test_map_slack_api_error_missing_scope_as_user_names_reconnect() -> None:
    mapped = map_slack_api_error(_slack_api_error("missing_scope"), as_user=True)
    assert mapped is not None and "reconnect via /privacy" in str(mapped), (
        "missing_scope on the user token means the caller's own grant is stale — "
        "reinstalling the app cannot fix it; the caller must reconnect"
    )
    assert "re-install" not in str(mapped), (
        "user-token missing_scope must not blame the workspace install"
    )


@pytest.mark.parametrize("code", ["channel_not_found", "not_in_channel"])
def test_map_slack_api_error_not_found_collapses_to_missing_access(code: str) -> None:
    mapped = map_slack_api_error(_slack_api_error(code))
    assert mapped is not None and str(mapped) == "missing channel access", (
        "unknown and forbidden channels must be indistinguishable (no existence leak)"
    )


def test_map_slack_api_error_unknown_returns_none() -> None:
    assert map_slack_api_error(_slack_api_error("ratelimited")) is None, (
        "unknown API errors must propagate, not be swallowed"
    )
