"""Unit tests for the platform-neutral Screen intermediate."""

from __future__ import annotations

from daimon.core.wizard.render import ScreenButton, to_screen
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.core.wizard.state import (
    NAV_CUSTOM_ID_PATTERN,
    SELECT_CUSTOM_ID_PATTERN,
    SUBMIT_CUSTOM_ID_PATTERN,
    WizardStatus,
    build_custom_id,
)

from ..shared_fakes import _state

_CHOICE_STEP = Step(
    key="color",
    question="Pick a color",
    kind=StepKind.CHOICE,
    options=[Option(label="Red", value="red"), Option(label="Blue", value="blue")],
    allow_custom=True,
)
_MULTI_STEP = Step(
    key="toppings",
    question="Pick toppings",
    kind=StepKind.MULTI,
    options=[
        Option(label="Cheese", value="cheese"),
        Option(label="Olives", value="olives"),
        Option(label="Pepperoni", value="pepperoni"),
    ],
    min=1,
    max=2,
    allow_custom=True,
)
_TEXT_STEP = Step(
    key="notes", question="Any notes?", kind=StepKind.TEXT, options=[], allow_custom=True
)
_NO_CUSTOM_STEP = Step(
    key="size",
    question="Pick a size",
    kind=StepKind.CHOICE,
    options=[Option(label="Small", value="small")],
    allow_custom=False,
)
_IMAGE_STEP = Step(
    key="pet",
    question="Pick a pet",
    kind=StepKind.CHOICE,
    options=[Option(label="Cat", value="cat")],
    image_handle="handle-123",
)

_SPEC = WizardSpec(
    prompt="Order form",
    steps=[_CHOICE_STEP, _MULTI_STEP, _TEXT_STEP, _NO_CUSTOM_STEP, _IMAGE_STEP],
)

_SEVEN_OPTION_STEP = Step(
    key="flavor",
    question="Pick a flavor",
    kind=StepKind.CHOICE,
    options=[Option(label=f"Flavor {i}", value=f"f{i}") for i in range(7)],
)
_SEVEN_OPTION_SPEC = WizardSpec(prompt="Flavor form", steps=[_SEVEN_OPTION_STEP])


def _all_actions(screen_button_rows: tuple[tuple[ScreenButton, ...], ...]) -> list[str]:
    return [button.action for row in screen_button_rows for button in row]


def test_choice_step_screen_shape() -> None:
    screen = to_screen(_SPEC, _state(current_step=0))

    assert screen.accent == "blurple"
    assert "Pick a color" in screen.head_text
    assert "Step 1 of 5" in screen.head_text
    assert screen.select is None
    assert screen.button_rows[0] == (
        ScreenButton(action="s0_c0", label="Red", style="secondary"),
        ScreenButton(action="s0_c1", label="Blue", style="secondary"),
    )
    nav_row = screen.button_rows[-1]
    assert [b.action for b in nav_row] == ["s0_custom", "back", "next"]


def test_choice_step_seven_options_chunks_into_five_and_two_plus_nav() -> None:
    screen = to_screen(_SEVEN_OPTION_SPEC, _state(current_step=0))

    assert len(screen.button_rows[0]) == 5
    assert len(screen.button_rows[1]) == 2
    assert len(screen.button_rows) == 3, "one row of 5, one row of 2, plus the nav row"


def test_step_counter_reflects_current_step() -> None:
    screen = to_screen(_SPEC, _state(current_step=2))

    assert "Step 3 of 5" in screen.head_text
    assert "Any notes?" in screen.head_text


def test_multi_step_select_action_matches_step_index() -> None:
    screen = to_screen(_SPEC, _state(current_step=1))

    assert screen.select is not None
    assert screen.select.action == "s1_sel"
    assert screen.select.placeholder == "Pick toppings"


def test_multi_step_screen_marks_recorded_values_as_default() -> None:
    screen = to_screen(_SPEC, _state(current_step=1, answers={"toppings": ["cheese"]}))

    assert screen.select is not None
    defaults = {option.value: option.default for option in screen.select.options}
    assert defaults == {"cheese": True, "olives": False, "pepperoni": False}
    assert screen.select.min_values == 1
    assert screen.select.max_values == 2
    nav_row = screen.button_rows[-1]
    assert [b.action for b in nav_row] == ["s1_custom", "back", "next"]


def test_text_step_never_has_custom_button_even_with_allow_custom_true() -> None:
    screen = to_screen(_SPEC, _state(current_step=2))

    assert screen.select is None
    enter_row = screen.button_rows[0]
    assert enter_row == (ScreenButton(action="s2_enter", label="Enter…", style="primary"),)
    nav_row = screen.button_rows[-1]
    assert [b.action for b in nav_row] == ["back", "next"], (
        "a text step must never show the custom-entry button even when allow_custom is set"
    )


def test_step_with_allow_custom_false_has_no_custom_button() -> None:
    screen = to_screen(_SPEC, _state(current_step=3))

    nav_row = screen.button_rows[-1]
    assert [b.action for b in nav_row] == ["back", "next"]


def test_step_with_image_handle_produces_screen_image() -> None:
    screen = to_screen(_SPEC, _state(current_step=4))

    assert screen.image is not None
    assert screen.image.handle == "handle-123"


def test_review_screen_shape() -> None:
    screen = to_screen(_SPEC, _state(current_step=len(_SPEC.steps), answers={"color": ["red"]}))

    assert screen.accent == "blurple"
    assert "Review your answers" in screen.head_text
    assert screen.select is None
    assert screen.image is None
    assert screen.body_text is not None
    assert screen.button_rows == (
        (
            ScreenButton(action="back", label="Back", style="secondary"),
            ScreenButton(action="submit", label="Submit", style="success"),
        ),
    )


def test_collapsed_screen_has_zero_button_rows() -> None:
    screen = to_screen(_SPEC, _state(status=WizardStatus.SUBMITTED, answers={"color": ["red"]}))

    assert screen.button_rows == (), "a submitted form must not be re-tappable"
    assert screen.accent == "green"
    assert screen.image is None


def test_collapsed_and_review_screens_carry_no_image() -> None:
    review = to_screen(_SPEC, _state(current_step=len(_SPEC.steps)))
    collapsed = to_screen(_SPEC, _state(status=WizardStatus.SUBMITTED))

    assert review.image is None
    assert collapsed.image is None


def test_every_emitted_action_round_trips_through_build_custom_id_and_matches_one_pattern() -> None:
    states = [
        _state(current_step=0),
        _state(current_step=1, answers={"toppings": ["cheese"]}),
        _state(current_step=2),
        _state(current_step=3),
        _state(current_step=4),
        _state(current_step=len(_SPEC.steps)),
        _state(status=WizardStatus.SUBMITTED),
    ]
    for state in states:
        screen = to_screen(_SPEC, state)
        actions = _all_actions(screen.button_rows)
        if screen.select is not None:
            actions.append(screen.select.action)
        for action in actions:
            custom_id = build_custom_id(screen.short_id, action)
            patterns_matched = sum(
                1
                for pattern in (
                    NAV_CUSTOM_ID_PATTERN,
                    SELECT_CUSTOM_ID_PATTERN,
                    SUBMIT_CUSTOM_ID_PATTERN,
                )
                if pattern.fullmatch(custom_id)
            )
            assert patterns_matched == 1, (
                f"action {action!r} must fullmatch exactly one custom_id grammar"
            )
