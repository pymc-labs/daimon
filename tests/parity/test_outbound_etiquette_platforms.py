"""Outbound etiquette on Discord, Slack and Teams, with each deliberate asymmetry.

- Teams tool posts carry no mention entities, so nothing in them pings and no
  person counts toward the people budget there.
- Discord names the requester as ``<@id>`` (sends allow only tagged people to
  ping); Slack and Teams have no silent mention, so they use the name.
- Discord role tags stay as text: every send turns role pings off. Slack and
  Teams group pings become plain words.
"""

import pytest
from daimon.core.outbound.mentions import (
    attribution_line,
    mentioned_user_ids,
    neutralise_broadcasts,
    render_post,
)

PLATFORMS = ("discord", "slack", "teams")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_broadcast_when_any_platform_then_never_live(platform):
    live = {"discord": "@everyone", "slack": "<!channel>", "teams": "<at>Everyone</at>"}
    out = neutralise_broadcasts(f"hi {live[platform]}", platform)
    assert live[platform] not in out, f"{platform}: a broadcast never posts live"


def test_people_count_when_teams_then_zero_because_nothing_pings():
    counts = {
        "discord": mentioned_user_ids("<@1>", "discord"),
        "slack": mentioned_user_ids("<@U1>", "slack"),
        "teams": mentioned_user_ids("<at>Ann</at>", "teams"),
    }
    assert counts == {"discord": ("1",), "slack": ("U1",), "teams": ()}, "Teams counts nobody"


def test_attribution_when_requester_known_then_discord_id_else_name():
    lines = {
        p: attribution_line(p, requester_id="42" if p == "discord" else None, requester_name="Ann")
        for p in PLATFORMS
    }
    assert lines == {
        "discord": "-# requested by <@42>",
        "slack": "requested by Ann",
        "teams": "requested by Ann",
    }, "only Discord has a mention that can be sent without a ping"


def test_discord_role_tag_when_posted_then_kept_as_text():
    assert render_post("<@&9> ready", "discord").text == "<@&9> ready", "role pings are off"
