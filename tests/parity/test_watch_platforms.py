"""Executable record: where each kind of watch can be made.

A page watch runs on Discord, Slack, Teams and the web: each runs its report turns and
rechecks the owner's access on every report. Polled and callback watches need Discord's
runner and access checks, except a web poll of a page's own data. Only Discord and the
web report into a new thread per firing. If a platform gains a kind, update this record.
"""

from __future__ import annotations

import pytest
from daimon.core.stores import watches


@pytest.mark.parametrize("platform", ["discord", "slack", "teams", "web"])
def test_every_platform_takes_a_page_watch(platform: str) -> None:
    watches.require_watch_platform(platform, "page_event")


@pytest.mark.parametrize("platform", ["slack", "teams", "web"])
@pytest.mark.parametrize(
    ("source", "tool"), [("poll", "read_thread"), ("mcp_poll", None), ("callback", None)]
)
def test_other_watches_stay_on_discord(platform: str, source: str, tool: str | None) -> None:
    watches.require_watch_platform("discord", source, tool)
    with pytest.raises(ValueError, match="Discord"):
        watches.require_watch_platform(platform, source, tool)


def test_the_web_polls_only_page_data() -> None:
    watches.require_watch_platform("web", "poll", watches.WEB_POLL_TOOL)
    with pytest.raises(ValueError, match="Discord"):
        watches.require_watch_platform("slack", "poll", watches.WEB_POLL_TOOL)


@pytest.mark.parametrize("platform", ["slack", "teams"])
def test_only_discord_and_the_web_report_in_a_new_thread(platform: str) -> None:
    for place in ("discord", "web"):
        assert watches.require_delivery_place(place, "T1", "C1", "new_thread") == "new_thread"
    with pytest.raises(ValueError, match="thread"):
        watches.require_delivery_place(platform, "T1", "C1", "new_thread")
