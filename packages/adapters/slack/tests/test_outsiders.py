"""Only fresh sharing evidence establishes Slack outsiders; guests stay unchecked."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientConnectionError
from daimon.adapters.slack.outsiders import SLACK_GUESTS_UNCHECKED, channel_outsiders
from daimon.core.outsiders import OUTSIDER_CHECK_SKIPPED, OUTSIDERS_WARNING
from slack_sdk.errors import SlackApiError


@pytest.mark.parametrize(
    "field",
    [
        "is_shared",
        "is_ext_shared",
        "is_org_shared",
        "is_ext_ws_shared",
        "is_pending_ext_shared",
        "is_global_shared",
        "is_org_default",
        "is_org_mandatory",
        "pending_shared",
        "pending_connected_team_ids",
        "connected_team_ids",
        "shared_team_ids",
        "internal_team_ids",
    ],
)
async def test_each_sharing_indicator_is_outside(field: str) -> None:
    client = MagicMock()
    value: object = True if field.startswith("is_") else ["T_OTHER"]
    client.conversations_info = AsyncMock(
        return_value={"channel": {"is_shared": False, field: value}}
    )
    result = await channel_outsiders(client, team_id="T1", channel_id="C1")
    assert result.outsiders_can_view is True
    assert result.note == SLACK_GUESTS_UNCHECKED
    assert result.lines(readers="any") == (OUTSIDERS_WARNING, SLACK_GUESTS_UNCHECKED)
    assert result.lines(readers="own") == ()
    client.conversations_info.assert_awaited_once_with(channel="C1")
    assert [call[0] for call in client.mock_calls] == ["conversations_info"]


async def test_only_own_workspace_does_not_prove_outsiders_or_check_guests() -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        return_value={
            "channel": {
                "is_shared": False,
                "is_member": True,
                "shared_team_ids": ["T1"],
                "internal_team_ids": ["T1"],
                "connected_team_ids": [],
                "pending_shared": [],
            }
        }
    )
    result = await channel_outsiders(client, team_id="T1", channel_id="C1")
    assert result.outsiders_can_view is None, "unshared is not proof that no guests exist"
    assert result.bot_can_view is True
    assert result.lines(readers="any") == (SLACK_GUESTS_UNCHECKED,)


async def test_repeated_requests_use_fresh_info_with_no_cached_result() -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        side_effect=[
            {"channel": {"is_shared": False}},
            {"channel": {"is_org_shared": True}},
            {"channel": {"is_shared": False}},
        ]
    )
    results = [await channel_outsiders(client, team_id="T1", channel_id="C1") for _ in range(3)]
    assert [result.outsiders_can_view for result in results] == [None, True, None]
    assert client.conversations_info.await_count == 3
    assert [call[0] for call in client.mock_calls] == ["conversations_info"] * 3


@pytest.mark.parametrize("error", ["channel_not_found", "not_in_channel", "ratelimited"])
async def test_failed_info_has_skip_note_and_recognizes_no_view(error: str) -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        side_effect=SlackApiError("failed", MagicMock(data={"error": error}))
    )
    result = await channel_outsiders(client, team_id="T1", channel_id="C1")
    assert result.outsiders_can_view is None
    assert result.bot_can_view is (None if error == "ratelimited" else False)
    assert result.lines(readers="any") == (f"{OUTSIDER_CHECK_SKIPPED}\n{SLACK_GUESTS_UNCHECKED}",)
    assert result.lines(readers="own") == ()


@pytest.mark.parametrize("error", [ClientConnectionError(), TimeoutError()])
async def test_transport_failure_does_not_claim_checked(error: Exception) -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(side_effect=error)
    result = await channel_outsiders(client, team_id="T1", channel_id="C1")
    assert result.outsiders_can_view is None
    assert result.note is not None and OUTSIDER_CHECK_SKIPPED in result.note
    assert SLACK_GUESTS_UNCHECKED in result.note


@pytest.mark.parametrize(
    "channel",
    [
        None,
        "invalid",
        {},
        {"is_shared": "false"},
        {"is_shared": False, "pending_shared": "invalid"},
    ],
)
async def test_malformed_or_missing_sharing_information_is_skipped(channel: object) -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(return_value={"channel": channel})
    result = await channel_outsiders(client, team_id="T1", channel_id="C1")
    assert result.outsiders_can_view is None
    assert result.note is not None and OUTSIDER_CHECK_SKIPPED in result.note


async def test_positive_evidence_survives_other_missing_or_invalid_fields() -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        return_value={
            "channel": {
                "is_shared": None,
                "is_pending_ext_shared": True,
            }
        }
    )
    result = await channel_outsiders(client, team_id="T1", channel_id="C1")
    assert result.outsiders_can_view is True
    assert result.note == SLACK_GUESTS_UNCHECKED
