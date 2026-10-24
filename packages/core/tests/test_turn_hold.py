"""`TurnHold.contains`: what lies inside a held channel on each platform."""

from __future__ import annotations

import pytest
from daimon.core.turn_hold import TurnHold


@pytest.mark.parametrize(
    ("platform", "channel_id", "parent", "inside"),
    [
        ("slack", "C_SHARED", None, True),
        ("slack", "C_SHARED:1700000000.000100", "C_SHARED", True),
        ("slack", "C_SHARED:1700000000.000100", None, True),
        ("slack", "C_OTHER", None, False),
        ("slack", "C_OTHER:1700000000.000100", "C_OTHER", False),
        ("slack", "C_SHAREDX", None, False),
        ("teams", "19:shared@thread.tacv2", None, True),
        ("teams", "19:shared@thread.tacv2;messageid=1700", None, True),
        ("teams", "19:shared@thread.tacv2x", None, False),
        ("discord", "222", "111", True),
        ("discord", "333", "999", False),
        ("discord", "111:222", None, False),
    ],
)
def test_a_hold_contains_its_channel_and_threads_under_it(
    platform: str, channel_id: str, parent: str | None, inside: bool
) -> None:
    held = {
        "slack": "C_SHARED",
        "teams": "19:shared@thread.tacv2",
        "discord": "111",
    }[platform]
    assert (
        TurnHold(platform=platform, channel_id=held, client=False).contains(channel_id, parent)
        is inside
    )


def test_no_target_is_outside_every_hold() -> None:
    assert not TurnHold(platform="slack", channel_id="C_SHARED", client=True).contains(None)
