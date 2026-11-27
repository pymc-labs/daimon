"""Every Slack watch report rechecks that its owner may still post where it reports."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.slack.watch_access import require_watch_post_access
from daimon.core.turn.errors import AdmissionDenied
from slack_sdk.errors import SlackApiError

ROW = cast(Any, SimpleNamespace(requester_external_user_id="U1", parent_channel_id="C1"))


def _client(*, private: bool, members: list[str]) -> Any:
    return SimpleNamespace(
        conversations_info=AsyncMock(
            return_value={"channel": {"is_member": True, "is_private": private}}
        ),
        users_info=AsyncMock(return_value={"user": {}}),
        conversations_members=AsyncMock(return_value={"members": members}),
    )


@pytest.mark.parametrize(
    ("private", "members", "kept"), [(False, [], True), (True, ["U1"], True), (True, [], False)]
)
async def test_an_owner_who_left_a_private_channel_pauses_the_watch(
    private: bool, members: list[str], kept: bool
) -> None:
    pause = AsyncMock(return_value=AdmissionDenied(reason="invoker_not_allowed"))
    with patch("daimon.adapters.slack.watch_access.pause_for_lost_access", pause):
        if kept:
            await require_watch_post_access(
                cast(Any, None), ROW, _client(private=private, members=members)
            )
        else:
            with pytest.raises(AdmissionDenied):
                await require_watch_post_access(
                    cast(Any, None), ROW, _client(private=private, members=members)
                )
    assert pause.await_count == (0 if kept else 1)


async def test_a_slack_outage_refuses_the_report_without_pausing() -> None:
    client = _client(private=False, members=[])
    client.conversations_info.side_effect = SlackApiError(
        "down", cast(Any, SimpleNamespace(data={"error": "ratelimited"}))
    )
    pause = AsyncMock()
    with (
        patch("daimon.adapters.slack.watch_access.pause_for_lost_access", pause),
        pytest.raises(SlackApiError),
    ):
        await require_watch_post_access(cast(Any, None), ROW, client)
    pause.assert_not_awaited()
