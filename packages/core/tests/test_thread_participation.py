"""Pure tests for the organic-thread-participation decision core. Nothing here touches I/O."""

from __future__ import annotations

import pytest
from daimon.core.thread_participation import (
    ClassifierMessage,
    ClassifierVerdict,
    ParticipationSnapshot,
    Respond,
    Skip,
    SkipReason,
    build_classifier_prompt,
    decide_post_classifier,
    decide_pre_classifier,
    parse_classifier_response,
)


def test_open_snapshot_asks_the_classifier() -> None:
    snapshot = ParticipationSnapshot(on=True, auto_responses_in_window=0, max_per_window=20)
    assert decide_pre_classifier(snapshot) == Respond(), (
        "an on thread under its cap is worth a classifier call"
    )


def test_an_off_thread_skips_before_the_classifier() -> None:
    snapshot = ParticipationSnapshot(on=False, auto_responses_in_window=0, max_per_window=20)
    assert decide_pre_classifier(snapshot) == Skip(SkipReason.NOT_ON), (
        "an off thread must skip before any model call"
    )


def test_hourly_cap_is_inclusive() -> None:
    snapshot = ParticipationSnapshot(on=True, auto_responses_in_window=20, max_per_window=20)
    assert decide_pre_classifier(snapshot) == Skip(SkipReason.RATE_LIMITED), (
        "the hourly cap is reached at max_per_window, not one past it"
    )


def test_post_classifier_maps_verdicts() -> None:
    assert decide_post_classifier(ClassifierVerdict("respond", "asked")) == Respond(), (
        "a respond verdict runs the turn"
    )
    assert decide_post_classifier(ClassifierVerdict("silence", "chatter")) == Skip(
        SkipReason.CLASSIFIER_SILENCED
    ), "a silence verdict skips, and says why"


@pytest.mark.parametrize(
    "text",
    [
        '{"decision": "respond", "reason": "asked"}',
        '```json\n{"decision": "respond", "reason": "asked"}\n```',
    ],
)
def test_parse_accepts_plain_and_fenced_json(text: str) -> None:
    assert parse_classifier_response(text) == ClassifierVerdict("respond", "asked"), (
        "a fenced verdict parses the same as a bare one"
    )


def test_parse_defaults_missing_reason() -> None:
    assert parse_classifier_response('{"decision": "silence"}') == ClassifierVerdict(
        "silence", ""
    ), "a verdict without a reason is still a verdict"


@pytest.mark.parametrize("text", ["not json", "[1]", '{"decision": "maybe"}', '{"x": 1}'])
def test_parse_rejects_malformed_output(text: str) -> None:
    with pytest.raises(ValueError):
        parse_classifier_response(text)


def test_prompt_labels_bot_escapes_content_and_groups_the_burst() -> None:
    prompt = build_classifier_prompt(
        [ClassifierMessage("bot", "1 < 2", is_bot=True)],
        [
            ClassifierMessage('Al "A" B', "first", is_bot=False),
            ClassifierMessage("Bo", "second", is_bot=False),
        ],
        bot_display_name="daimon",
    )
    assert '<msg author="daimon">1 &lt; 2</msg>' in prompt, (
        "the bot's own messages are labelled by display name and content is escaped"
    )
    assert "<candidates>" in prompt and prompt.count("user:") == 2, (
        "the burst is grouped under <candidates> and humans are labelled user:"
    )
    assert "author='user:Al \"A\" B'" in prompt, "a quoted display name is attribute-escaped"
