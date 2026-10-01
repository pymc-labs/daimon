"""Pure tests for the organic-thread-participation decision core. Nothing here touches I/O."""

from __future__ import annotations

import pytest
from daimon.core.thread_participation import (
    ClassifierMessage,
    ClassifierVerdict,
    ParticipationMode,
    ParticipationScope,
    ParticipationSnapshot,
    ResolvedParticipation,
    Respond,
    Skip,
    SkipReason,
    build_classifier_prompt,
    build_participation_explanation,
    build_participation_note,
    decide_post_classifier,
    decide_pre_classifier,
    parse_classifier_response,
    resolve_participation,
)

ON, OFF, DISABLED = ParticipationMode.ON, ParticipationMode.OFF, ParticipationMode.DISABLED


@pytest.mark.parametrize(
    ("deployment", "workspace", "channel", "thread", "expected"),
    [
        (OFF, None, None, None, ResolvedParticipation(OFF, "deployment")),
        (OFF, None, None, ON, ResolvedParticipation(ON, "thread")),
        (OFF, None, ON, None, ResolvedParticipation(ON, "channel")),
        (OFF, ON, None, None, ResolvedParticipation(ON, "workspace")),
        (ON, None, None, None, ResolvedParticipation(ON, "deployment")),
        # The most specific explicit value wins.
        (OFF, ON, OFF, None, ResolvedParticipation(OFF, "channel")),
        (OFF, None, ON, OFF, ResolvedParticipation(OFF, "thread")),
        (ON, OFF, None, ON, ResolvedParticipation(ON, "thread")),
        # DISABLED is final for everything below it.
        (DISABLED, ON, ON, ON, ResolvedParticipation(DISABLED, "deployment")),
        (OFF, DISABLED, ON, ON, ResolvedParticipation(DISABLED, "workspace")),
        (OFF, None, DISABLED, ON, ResolvedParticipation(DISABLED, "channel")),
        # ...but a higher tier can still be OFF while a lower one is ON.
        (OFF, OFF, None, ON, ResolvedParticipation(ON, "thread")),
    ],
)
def test_resolve_participation_cascade(
    deployment: ParticipationMode,
    workspace: ParticipationMode | None,
    channel: ParticipationMode | None,
    thread: ParticipationMode | None,
    expected: ResolvedParticipation,
) -> None:
    resolved = resolve_participation(
        deployment=deployment, workspace=workspace, channel=channel, thread=thread
    )
    assert resolved == expected, (
        f"cascade deployment={deployment} workspace={workspace} channel={channel} "
        f"thread={thread} should resolve to {expected}, got {resolved}"
    )


def test_open_snapshot_asks_the_classifier() -> None:
    snapshot = ParticipationSnapshot(mode=ON, auto_responses_in_window=0, max_per_window=20)
    assert decide_pre_classifier(snapshot) == Respond(), (
        "an on thread under its cap is worth a classifier call"
    )


@pytest.mark.parametrize("mode", [OFF, DISABLED])
def test_anything_but_on_skips_before_the_classifier(mode: ParticipationMode) -> None:
    snapshot = ParticipationSnapshot(mode=mode, auto_responses_in_window=0, max_per_window=20)
    assert decide_pre_classifier(snapshot) == Skip(SkipReason.NOT_ON), (
        f"mode {mode} must skip before any model call"
    )


def test_hourly_cap_is_inclusive() -> None:
    snapshot = ParticipationSnapshot(mode=ON, auto_responses_in_window=20, max_per_window=20)
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


def test_participation_note_reports_the_effective_tier() -> None:
    note = build_participation_note(
        scope=ParticipationScope.THREAD,
        scope_id="t1",
        requested=ON,
        effective=ResolvedParticipation(DISABLED, "channel"),
    )
    assert "thread t1 is set to on" in note and "disabled at the channel level" in note, (
        "the note reports the write and the wider scope that still silences it"
    )
    note = build_participation_note(
        scope=ParticipationScope.WORKSPACE,
        scope_id=None,
        requested=None,
        effective=ResolvedParticipation(OFF, "deployment"),
    )
    assert note.startswith("the whole workspace now inherits") and "@mentioned" in note, (
        "clearing a scope reports the inheritance and what now happens there"
    )


def test_participation_explanation_names_the_winning_tier() -> None:
    on = build_participation_explanation(ResolvedParticipation(ON, "thread"), thread_id="t1")
    assert "I follow thread t1" in on and "thread tier is set to on" in on, (
        "an on cascade explains which tier turned it on"
    )
    disabled = build_participation_explanation(
        ResolvedParticipation(DISABLED, "workspace"), thread_id=None
    )
    assert "only reply in here when @mentioned" in disabled and "workspace tier" in disabled, (
        "a disabled cascade says so and names the tier that cannot be overridden"
    )
    off = build_participation_explanation(ResolvedParticipation(OFF, "deployment"), thread_id="t1")
    assert "deployment tier is the" in off and "it is off" in off, (
        "an off cascade names the narrowest tier with a setting"
    )
