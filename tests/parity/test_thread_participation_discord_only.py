"""Executable record that organic thread participation is Discord-only for now.

The store and the tool are keyed by platform, but the Slack app subscribes to
``app_mention`` alone: seeing unmentioned thread messages needs the
``message.channels`` and ``message.groups`` events, and adding either forces
every installed workspace through re-authorization. Asserting both halves
here, rather than merely documenting them in the PR, makes drift fail loudly:
an event quietly growing without the Slack responder behind it, or the tool
starting to accept Slack callers before the adapter reads the rows.

No platform parametrization, no database -- a scope-and-surface check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml
from daimon.adapters.mcp.tools import thread_participation

_MANIFEST = Path(__file__).resolve().parents[2] / "docs" / "slack-app-manifest.yaml"


def _slack_bot_events() -> list[str]:
    manifest = cast(dict[str, Any], yaml.safe_load(_MANIFEST.read_text()))
    return cast(list[str], manifest["settings"]["event_subscriptions"]["bot_events"])


def test_slack_manifest_still_lacks_the_message_events() -> None:
    events = _slack_bot_events()
    assert "message.channels" not in events and "message.groups" not in events, (
        "the Slack app deliberately sees mentions only; message.channels / message.groups "
        "force every installed workspace to re-authorize, so they land together with a Slack "
        "auto-responder -- if that is what this change is, update this record"
    )


def test_the_tool_refuses_every_platform_but_discord() -> None:
    assert thread_participation._PLATFORM == "discord", (  # pyright: ignore[reportPrivateUsage]
        "set_thread_participation / get_thread_participation must keep refusing non-Discord "
        "callers until the Slack adapter reads thread_participation_scopes"
    )
    assert "Discord" in thread_participation._WRONG_PLATFORM  # pyright: ignore[reportPrivateUsage]
