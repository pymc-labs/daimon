"""A person from another organisation is never sent to a 1:1 chat with Daimon.

Teams opens no 1:1 chat across organisations, so for such a sender setup and billing
are for someone in the organisation that added Daimon. Every text the model reads on
their turn, every refusal and the help card say so, and name no other chat app.
"""

from __future__ import annotations

import json
import re
from typing import Any, get_args

import pytest
from daimon.adapters.teams import app as teams_app
from daimon.adapters.teams.commands import NO_CHAT_ACROSS_ORGS
from daimon.adapters.teams.help import COMMAND_HELP, help_card
from daimon.adapters.teams.private_replies import NOT_YET_PRIVATE, NOT_YET_PRIVATE_FOREIGN
from daimon.core.access_policy import ChannelRule
from daimon.core.here_card import HereCard, render_here_card
from daimon.core.platform_words import Place, turn_words
from daimon.core.turn import TerminationReason, render_termination_notice
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn_origin import render_turn_origin

from .test_no_foreign_platform_names import (
    _assert_only_own,  # pyright: ignore[reportPrivateUsage]
    _origin,  # pyright: ignore[reportPrivateUsage]
)

#: "1:1" as the chat, not inside a time such as "21:15".
_ONE_TO_ONE = re.compile(r"(?<![\d:])1:1(?![\d:])")


_HOST = "someone in the organisation that added Daimon"
_CAPITALISED_HOST = _HOST[0].upper() + _HOST[1:]
_PLACES: tuple[Place, ...] = ("channel", "group")
_THREADS: dict[Place, str] = {
    "channel": "19:abc123@thread.tacv2;messageid=1700000000000",
    "group": "19:abc123@thread.v2",
}
_FOREIGN = RefusalNouns.for_platform("teams", foreign=True)


def _no_private_chat(text: str) -> None:
    assert _ONE_TO_ONE.search(text) is None, text
    assert "private chat" not in text.lower(), text
    _assert_only_own("teams", text)


@pytest.mark.parametrize("place", _PLACES)
def test_the_words_send_a_foreign_senders_setup_and_billing_to_the_host_organisation(
    place: Place,
) -> None:
    words = turn_words("teams", place=place, foreign=True)

    assert words["setup"] == f"ask {_HOST} to send `setup`"
    assert words["billing"] == f"ask {_HOST} to send `billing`"
    assert words["admin"].startswith(_HOST)
    assert words["here"] == "send `here`" and words["help"] == "send `help`"
    _no_private_chat(json.dumps(words))


@pytest.mark.parametrize("place", _PLACES)
def test_the_words_for_a_local_sender_still_name_the_chat(place: Place) -> None:
    words = turn_words("teams", place=place)

    assert "1:1 chat" in words["setup"] and "1:1 chat" in words["billing"]
    assert words["private_chat"] == "1:1 chat"


@pytest.mark.parametrize("place", _PLACES)
def test_a_foreign_senders_turn_controls_carry_those_words(place: Place) -> None:
    channel = {"parent_channel_id": "19:abc123@thread.tacv2", "thread_id": _THREADS[place]}
    guest = _origin("teams", sender_foreign=True, **channel)

    foreign = json.loads(render_turn_origin(guest, effective_is_admin=False).split("\n")[1])
    local = json.loads(
        render_turn_origin(_origin("teams", **channel), effective_is_admin=False).split("\n")[1]
    )

    assert foreign["words"]["setup"] == f"ask {_HOST} to send `setup`"
    _no_private_chat(json.dumps(foreign["words"]))
    assert "1:1 chat" in local["words"]["setup"], "a local sender is told as before"


@pytest.mark.parametrize("reason", get_args(AdmissionDenialReason))
@pytest.mark.parametrize("in_dm", [False, True])
def test_a_foreign_senders_admission_refusals_name_no_private_chat(
    reason: AdmissionDenialReason, in_dm: bool
) -> None:
    _no_private_chat(admission_refusal_text(reason, _FOREIGN, in_dm=in_dm))


@pytest.mark.parametrize("reason", list(TerminationReason))
def test_a_foreign_senders_termination_notices_name_no_private_chat(
    reason: TerminationReason,
) -> None:
    notice = render_termination_notice(
        reason,
        in_dm=False,
        setup_command=_FOREIGN.setup,
        billing_command=_FOREIGN.billing,
        setup_verb=_FOREIGN.verb,
        admin=_FOREIGN.who,
    )

    if notice is not None:
        _no_private_chat(notice.plain_text())


def test_the_refusals_that_name_setup_and_billing_ask_the_host_organisation() -> None:
    teams: Any = teams_app
    not_set_up = teams._not_set_up(_FOREIGN)  # pyright: ignore[reportUnknownMemberType]
    resolver_miss = teams._resolver_miss(_FOREIGN)  # pyright: ignore[reportUnknownMemberType]
    balance = admission_refusal_text("balance_depleted", _FOREIGN)
    missing = render_termination_notice(
        TerminationReason.MISSING_CONFIG,
        setup_command=_FOREIGN.setup,
        setup_verb=_FOREIGN.verb,
        admin=_FOREIGN.who,
    )

    assert not_set_up.endswith(f"Ask {_HOST} to send `setup`.")
    assert resolver_miss.endswith(f"Ask {_HOST} to check `setup`.")
    assert balance.endswith(f"{_CAPITALISED_HOST} can top up with `billing`.")
    assert missing is not None and missing.next_step == f"Ask {_HOST} to send `setup`."
    for text in (not_set_up, resolver_miss, balance):
        _no_private_chat(text)


def test_a_local_senders_refusals_are_unchanged() -> None:
    local = RefusalNouns.for_platform("teams")

    assert "`setup` in a 1:1 chat with me" in local.setup
    assert "An admin can top up with `billing` in a 1:1 chat with me." in admission_refusal_text(
        "balance_depleted", local
    )


@pytest.mark.parametrize("place", _PLACES)
def test_the_help_card_for_a_foreign_sender_sends_chat_commands_to_the_host_organisation(
    place: Place,
) -> None:
    card = help_card([*COMMAND_HELP], bot="Daimon", place=place, foreign=True)
    text = json.dumps(card.model_dump(by_alias=True))

    assert f"For setup, support, ask {_HOST}." in text
    _no_private_chat(text)
    local = json.dumps(help_card([*COMMAND_HELP], bot="Daimon", place=place).model_dump())
    assert "In our 1:1 chat: setup, support" in local, "a local sender is told as before"


def test_the_notices_and_pointers_a_foreign_sender_meets_name_no_private_chat() -> None:
    _no_private_chat(NO_CHAT_ACROSS_ORGS.format(name="setup"))
    assert "Ask someone in the organisation that added Daimon." in NO_CHAT_ACROSS_ORGS
    _no_private_chat(NOT_YET_PRIVATE_FOREIGN.format(bot="Daimon"))
    assert "1:1 chat" in NOT_YET_PRIVATE, "a local sender is still offered the chat"


@pytest.mark.parametrize("state", ["no_view", "no_replies", "no_agent", "blocked"])
def test_the_here_card_for_a_foreign_sender_names_no_private_chat(state: str) -> None:
    def card(*, foreign: bool) -> HereCard:
        return HereCard(
            platform="teams",
            foreign=foreign,
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

    shown = render_here_card(card(foreign=True))
    local = render_here_card(card(foreign=False))

    _no_private_chat(f"{shown.title} {shown.subline}")
    assert _CAPITALISED_HOST in (shown.subline or "")
    if state != "no_view":
        assert "1:1 chat" in (local.subline or ""), "a local sender is told as before"
