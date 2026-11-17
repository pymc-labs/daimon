"""Support requests use the configured escalation channel."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.config import SupportSettings
from daimon.core.support_routing import deployment_channel, support_recipient_tiers


async def test_support_request_has_no_direct_message_recipients() -> None:
    assert (
        await support_recipient_tiers(
            None,  # type: ignore[arg-type]
            tenant_id=uuid.uuid4(),
            platform="discord",
            channel_id="channel",
            requester_id="requester",
        )
        == ()
    )


@pytest.mark.parametrize(
    ("platform", "expected"), [("discord", "123"), ("slack", "C1"), ("teams", None), ("web", None)]
)
def test_deployment_channel_when_another_platforms_id_then_unset(
    platform: str, expected: str | None
) -> None:
    support = SupportSettings(
        escalation_channel_id="123",
        slack_escalation_channel_id="C1",
    )
    assert deployment_channel(support, platform) == expected, "a Slack id never serves Teams"
    teams = SupportSettings(
        escalation_channel_id="19:a@thread.tacv2",
        slack_escalation_channel_id="19:b@thread.tacv2",
        teams_escalation_channel_id="19:c@thread.tacv2",
    )
    want = "19:c@thread.tacv2" if platform == "teams" else None
    assert deployment_channel(teams, platform) == want, "a Teams id serves Teams only"
