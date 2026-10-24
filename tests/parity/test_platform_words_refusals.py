"""No platform's refusal, notice or here card shows another platform's words.

The nouns and commands come from `daimon.core.platform_words`; this renders
every core refusal and notice per platform and fails on a foreign word.
"""

from __future__ import annotations

from typing import get_args

import pytest
from daimon.adapters.discord.bot import DISCORD_REFUSAL_NOUNS
from daimon.adapters.slack.runtime import SLACK_REFUSAL_NOUNS
from daimon.adapters.teams.app import TEAMS_REFUSAL_NOUNS
from daimon.core.access_policy import ChannelRule
from daimon.core.here_card import HereCard, render_here_card
from daimon.core.platform_words import Place
from daimon.core.routine_delivery import failure_notice_text
from daimon.core.stores.domain import ChatPlatform
from daimon.core.turn import TerminationReason, render_termination_notice
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text

_TEAMS_FOREIGN = (
    "/agent-setup",
    "/billing",
    "/github",
    "/dm-me",
    "/here",
    "server admin",
    "workspace admin",
    "Manage Server",
    "run `setup`",
)
_FOREIGN: dict[ChatPlatform, tuple[str, ...]] = {
    "teams": _TEAMS_FOREIGN,
    "slack": ("server admin", "Manage Server"),
    "discord": ("workspace admin",),
}
_PLACES: tuple[Place, ...] = ("channel", "dm")


def _assert_native(platform: ChatPlatform, text: str) -> None:
    for word in _FOREIGN[platform]:
        assert word not in text, f"{platform} text shows {word!r}: {text!r}"


@pytest.mark.parametrize("platform", get_args(ChatPlatform))
@pytest.mark.parametrize("place", _PLACES)
def test_every_admission_refusal_uses_the_platforms_words(
    platform: ChatPlatform, place: Place
) -> None:
    nouns = RefusalNouns.for_platform(platform, place=place)
    for reason in get_args(AdmissionDenialReason):
        for in_dm in (False, True):
            _assert_native(platform, admission_refusal_text(reason, nouns, in_dm=in_dm))


@pytest.mark.parametrize("platform", get_args(ChatPlatform))
@pytest.mark.parametrize("place", _PLACES)
def test_every_termination_notice_uses_the_platforms_words(
    platform: ChatPlatform, place: Place
) -> None:
    nouns = RefusalNouns.for_platform(platform, place=place)
    for reason in TerminationReason:
        for in_dm in (False, True):
            notice = render_termination_notice(
                reason,
                in_dm=in_dm,
                setup_command=nouns.setup,
                billing_command=nouns.billing,
                setup_verb=nouns.verb,
            )
            if notice is not None:
                _assert_native(platform, notice.plain_text())


@pytest.mark.parametrize("platform", get_args(ChatPlatform))
def test_every_routine_failure_notice_uses_the_platforms_words(platform: ChatPlatform) -> None:
    for reason in TerminationReason:
        _assert_native(platform, failure_notice_text(reason, platform=platform))


def test_the_adapters_build_their_refusals_from_the_table() -> None:
    assert RefusalNouns.for_platform("discord") == DISCORD_REFUSAL_NOUNS
    assert RefusalNouns.for_platform("slack") == SLACK_REFUSAL_NOUNS
    assert RefusalNouns.for_platform("teams") == TEAMS_REFUSAL_NOUNS


def test_nouns_keep_each_platforms_current_words() -> None:
    assert RefusalNouns.for_platform("discord") == RefusalNouns(
        scope="server", admin="a server admin", billing="`/billing`", setup="`/agent-setup`"
    )
    assert RefusalNouns.for_platform("slack") == RefusalNouns(
        scope="workspace", admin="a workspace admin", billing="`/billing`", setup="`/agent-setup`"
    )
    assert RefusalNouns.for_platform("teams").billing == "`billing` in a 1:1 chat with me"
    assert RefusalNouns.for_platform("teams").admin == "an admin"


@pytest.mark.parametrize("platform", get_args(ChatPlatform))
@pytest.mark.parametrize("state", ["no_view", "no_replies", "no_agent", "blocked"])
def test_here_card_sublines_use_the_platforms_words(platform: ChatPlatform, state: str) -> None:
    card = HereCard(
        platform=platform,
        channel_rule=ChannelRule(),
        who_may_answer="",
        reads_kept_inside=False,
        agent_name=None if state == "no_agent" else "ResearchBot",
        tier="channel",
        bot_can_view=state != "no_view",
        effective_writers="none" if state == "no_replies" else "any",
        agent_can_answer_here=state != "blocked",
        effective_readers="any",
        text="",
    )
    shown = render_here_card(card)
    assert shown.state == state
    _assert_native(platform, f"{shown.title} {shown.subline}")


@pytest.mark.parametrize("platform", get_args(ChatPlatform))
def test_an_unknown_caller_gets_nouns_that_name_no_command(platform: ChatPlatform) -> None:
    nouns = RefusalNouns.for_caller(None)
    assert nouns.admin == "an admin"
    assert "/" not in nouns.setup + nouns.billing
    assert RefusalNouns.for_caller(platform) == RefusalNouns.for_platform(platform)
