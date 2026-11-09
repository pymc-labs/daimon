"""Organic participation runs on Discord and Teams; Slack only observes setup lifecycle.

Discord and Teams share one gate shell (`daimon.core.participation_gates`):
the same cascade, hourly cap, billing gates, classifier and batch bounds.
Slack message subscriptions track deletion
of setup roots, and carry a typed "@<bot name>" when the workspace turned
plain-text mentions on (``test_plain_text_mentions_platforms.py``); that is a
mention, not participation. Unaddressed Slack messages are never screened.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml
from daimon.adapters.discord import bot as discord_bot
from daimon.adapters.teams import participation as teams_participation
from daimon.core.participation_gates import BATCH_MAX_MESSAGES, BATCH_MAX_QUIET_PERIODS

_MANIFEST = Path(__file__).resolve().parents[2] / "docs" / "slack-app-manifest.yaml"


def _slack_bot_events() -> list[str]:
    manifest = cast(dict[str, Any], yaml.safe_load(_MANIFEST.read_text()))
    return cast(list[str], manifest["settings"]["event_subscriptions"]["bot_events"])


def test_slack_manifest_receives_setup_lifecycle_events() -> None:
    events = _slack_bot_events()
    assert {
        "message.channels",
        "message.groups",
        "channel_archive",
        "channel_unarchive",
        "channel_deleted",
    }.issubset(events), "setup lifecycle requires root deletion and channel events"


def test_both_adapters_batch_on_the_shared_bounds() -> None:
    assert discord_bot._PARTICIPATION_BATCH_MAX_MESSAGES == BATCH_MAX_MESSAGES  # pyright: ignore[reportPrivateUsage]
    assert discord_bot._PARTICIPATION_BATCH_MAX_QUIET_PERIODS == BATCH_MAX_QUIET_PERIODS  # pyright: ignore[reportPrivateUsage]
    teams_bounds = {"BATCH_MAX_MESSAGES", "BATCH_MAX_QUIET_PERIODS", "ParticipationGates"}
    assert teams_bounds <= set(vars(teams_participation)), "Teams batches on the same shell"
