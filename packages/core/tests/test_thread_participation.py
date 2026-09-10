"""Pure tests for the organic-thread-participation decision core. Nothing here touches I/O."""

from __future__ import annotations

import pytest
from daimon.core.thread_participation import (
    AutoRespondSnapshot,
    ClassifierMessage,
    ClassifierVerdict,
    ParticipationMode,
    ParticipationScope,
    ResolvedParticipation,
    Respond,
    Skip,
    SkipReason,
    build_classifier_prompt,
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
    assert (
        resolve_participation(
            deployment=deployment, workspace=workspace, channel=channel, thread=thread
        )
        == expected
    )


def test_open_snapshot_asks_the_classifier() -> None:
    snap = AutoRespondSnapshot(mode=ON, auto_responses_in_window=0, max_per_window=20)
    assert decide_pre_classifier(snap) == Respond()


@pytest.mark.parametrize("mode", [OFF, DISABLED])
def test_anything_but_on_skips_before_the_classifier(mode: ParticipationMode) -> None:
    snap = AutoRespondSnapshot(mode=mode, auto_responses_in_window=0, max_per_window=20)
    assert decide_pre_classifier(snap) == Skip(SkipReason.NOT_ON)


def test_hourly_cap_is_inclusive() -> None:
    snap = AutoRespondSnapshot(mode=ON, auto_responses_in_window=20, max_per_window=20)
    assert decide_pre_classifier(snap) == Skip(SkipReason.RATE_LIMITED)


def test_post_classifier_maps_verdicts() -> None:
    assert decide_post_classifier(ClassifierVerdict("respond", "asked", 0.9)) == Respond()
    assert decide_post_classifier(ClassifierVerdict("silence", "chatter", 0.9)) == Skip(
        SkipReason.CLASSIFIER_SILENCED
    )


@pytest.mark.parametrize(
    "text",
    [
        '{"decision": "respond", "reason": "asked", "confidence": 0.8}',
        '```json\n{"decision": "respond", "reason": "asked", "confidence": 0.8}\n```',
    ],
)
def test_parse_accepts_plain_and_fenced_json(text: str) -> None:
    assert parse_classifier_response(text) == ClassifierVerdict("respond", "asked", 0.8)


def test_parse_defaults_missing_reason_and_confidence() -> None:
    assert parse_classifier_response('{"decision": "silence"}') == ClassifierVerdict(
        "silence", "", 0.0
    )


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
    assert '<msg author="daimon">1 &lt; 2</msg>' in prompt
    assert "<candidates>" in prompt and prompt.count("user:") == 2
    assert "author='user:Al \"A\" B'" in prompt


def test_participation_note_reports_the_effective_tier() -> None:
    note = build_participation_note(
        scope=ParticipationScope.THREAD,
        scope_id="t1",
        requested=ON,
        effective=ResolvedParticipation(DISABLED, "channel"),
    )
    assert "thread t1 is set to on" in note and "disabled at the channel level" in note
    note = build_participation_note(
        scope=ParticipationScope.WORKSPACE,
        scope_id=None,
        requested=None,
        effective=ResolvedParticipation(OFF, "deployment"),
    )
    assert note.startswith("the whole workspace now inherits") and "@mentioned" in note
