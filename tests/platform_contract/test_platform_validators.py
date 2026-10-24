"""Send every catalogue payload to the real platform validators. Opt-in; never gates CI.

    pytest -m platform_contract tests/platform_contract          (see README.md)

Slack needs `SLACK_QA_BOT_TOKEN`. Discord needs `DISCORD_QA_BOT_TOKEN` and
`DISCORD_PLATFORM_CONTRACT_CHANNEL_ID`, and that channel must be in the Daimon QA guild (the
fixture checks; `DISCORD_PLATFORM_CONTRACT_GUILD_ID` overrides the expected guild). A platform
without its variables is skipped.
Each platform also gets one known-bad canary that must be rejected, so a validator
(or a token that reaches the wrong endpoint) that accepts everything fails the run.
Run without xdist: the calls are paced to the platforms' rate limits in one process.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Iterator
from typing import Any

import pytest

from .catalogue import CATALOGUE, Built
from .platform_clients import DiscordScratchChannel, SlackValidator

# The platform calls are paced inside one process, so xdist workers would together exceed the limits.
pytestmark = [
    pytest.mark.platform_contract,
    pytest.mark.skipif(
        "PYTEST_XDIST_WORKER" in os.environ,
        reason="platform_contract paces calls to the platforms' rate limits in one process; run without -n",
    ),
]

# The Daimon QA guild. Scratch posts go only to a channel in this guild.
DEFAULT_QA_GUILD_ID = "1435062989119295640"

SLACK_NAMES = sorted(name for name in CATALOGUE if name.startswith("slack/"))
DISCORD_NAMES = sorted(name for name in CATALOGUE if name.startswith("discord/"))


@pytest.fixture(scope="module")
def slack_validator() -> Iterator[SlackValidator]:
    token = os.environ.get("SLACK_QA_BOT_TOKEN")
    if not token:
        pytest.skip("SLACK_QA_BOT_TOKEN is not set")
    validator = SlackValidator(token)
    yield validator
    validator.close()


@pytest.fixture(scope="module")
def discord_channel() -> Iterator[DiscordScratchChannel]:
    token = os.environ.get("DISCORD_QA_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_PLATFORM_CONTRACT_CHANNEL_ID")
    if not token or not channel_id:
        pytest.skip(
            "DISCORD_QA_BOT_TOKEN and DISCORD_PLATFORM_CONTRACT_CHANNEL_ID are not both set"
        )
    channel = DiscordScratchChannel(token, channel_id)
    expected_guild = os.environ.get("DISCORD_PLATFORM_CONTRACT_GUILD_ID", DEFAULT_QA_GUILD_ID)
    actual_guild = channel.guild_id()
    if actual_guild != expected_guild:
        channel.close()
        pytest.fail(
            f"scratch channel {channel_id} is in guild {actual_guild or 'none (a DM?)'}, not the QA guild "
            f"{expected_guild}; refusing to post (set DISCORD_PLATFORM_CONTRACT_GUILD_ID to override)"
        )
    yield channel
    channel.close()


@pytest.mark.parametrize("name", SLACK_NAMES)
def test_slack_accepts(name: str, slack_validator: SlackValidator) -> None:
    verdict = slack_validator.validate(CATALOGUE[name]())
    assert verdict.accepted, f"blocks.validate {verdict}"


@pytest.mark.parametrize("name", DISCORD_NAMES)
def test_discord_accepts(name: str, discord_channel: DiscordScratchChannel) -> None:
    verdict = discord_channel.validate(CATALOGUE[name]())
    assert verdict.accepted, f"Discord {verdict}"


def test_slack_rejects_the_shipped_feedback_modal_bug(slack_validator: SlackValidator) -> None:
    """The feedback modal once shipped `max_length: 4000`; Slack allows at most 3000."""
    built = CATALOGUE["slack/feedback_modal"]()
    view: dict[str, Any] = copy.deepcopy(built.payload)
    text_input = next(
        block["element"]
        for block in view["blocks"]
        if block.get("element", {}).get("type") == "plain_text_input"
    )
    text_input["max_length"] = 4000
    verdict = slack_validator.validate(Built("slack", "view", view))
    assert not verdict.accepted, (
        "blocks.validate accepted max_length 4000; the validator is not checking"
    )
    assert "max_length" in verdict.error


def test_discord_rejects_an_81_character_button_label(
    discord_channel: DiscordScratchChannel,
) -> None:
    """Discord caps a button label at 80 characters."""
    button = {"type": 2, "style": 1, "label": "x" * 81, "custom_id": "platform_contract_canary"}
    body = {"components": [{"type": 1, "components": [button]}]}
    verdict = discord_channel.validate(Built("discord", "message", body))
    assert not verdict.accepted, (
        "Discord accepted an 81-character button label; the check is not checking"
    )
    assert "label" in verdict.error
