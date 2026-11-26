"""Unit tests for the wizard's pure transition function and action translator."""

from __future__ import annotations

import pytest
from daimon.core.wizard.apply import apply, build_action
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.core.wizard.state import ActionKind, WizardAction, WizardState, WizardStatus

from ..shared_fakes import _state

_CHOICE_STEP = Step(
    key="color",
    question="Pick a color",
    kind=StepKind.CHOICE,
    options=[Option(label="Red", value="red"), Option(label="Blue", value="blue")],
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
    min=2,
)
_TEXT_STEP = Step(key="notes", question="Any notes?", kind=StepKind.TEXT, options=[])

_SPEC = WizardSpec(prompt="Order form", steps=[_CHOICE_STEP, _MULTI_STEP, _TEXT_STEP])


def test_select_choice_records_option_value_and_advances() -> None:
    state = _state(current_step=0)
    action = WizardAction(kind=ActionKind.SELECT_CHOICE, step_index=0, values=["red"])

    new_state = apply(state, _SPEC, action)

    assert new_state.answers["color"] == ["red"], "choice value must be recorded under step key"
    assert new_state.current_step == 1, "select_choice must auto-advance one step"


def test_set_multi_records_values_and_does_not_advance() -> None:
    state = _state(current_step=1)
    action = WizardAction(kind=ActionKind.SET_MULTI, step_index=1, values=["cheese", "olives"])

    new_state = apply(state, _SPEC, action)

    assert new_state.answers["toppings"] == ["cheese", "olives"]
    assert new_state.current_step == 1, "set_multi must not advance; Next advances"


def test_next_on_unmet_required_multi_is_a_no_op() -> None:
    state = _state(current_step=1, answers={"toppings": ["cheese"]})
    action = WizardAction(kind=ActionKind.NEXT)

    new_state = apply(state, _SPEC, action)

    assert new_state.current_step == 1, "Next must refuse to advance below a multi step's min"
    assert new_state == state, "no-op transition should return equivalent state"


def test_next_on_met_multi_advances() -> None:
    state = _state(current_step=1, answers={"toppings": ["cheese", "olives"]})
    action = WizardAction(kind=ActionKind.NEXT)

    new_state = apply(state, _SPEC, action)

    assert new_state.current_step == 2, "Next must advance once the multi's min is met"


def test_back_from_step_zero_stays_at_zero() -> None:
    state = _state(current_step=0)
    action = WizardAction(kind=ActionKind.BACK)

    new_state = apply(state, _SPEC, action)

    assert new_state.current_step == 0, "Back must clamp at the first step"


def test_next_from_last_step_lands_on_review_index() -> None:
    state = _state(current_step=2, answers={"toppings": ["cheese", "olives"]})
    action = WizardAction(kind=ActionKind.NEXT)

    new_state = apply(state, _SPEC, action)

    assert new_state.current_step == len(_SPEC.steps), "review index is one past the last step"


def test_custom_text_on_choice_step_replaces_and_advances() -> None:
    state = _state(current_step=0)
    action = WizardAction(kind=ActionKind.ADD_CUSTOM, step_index=0, text="Purple")

    new_state = apply(state, _SPEC, action)

    assert new_state.answers["color"] == ["Purple"]
    assert new_state.current_step == 1, "custom text on a choice step must advance"


def test_custom_text_on_multi_step_appends_and_stays() -> None:
    state = _state(current_step=1, answers={"toppings": ["cheese"]})
    action = WizardAction(kind=ActionKind.ADD_CUSTOM, step_index=1, text="Anchovies")

    new_state = apply(state, _SPEC, action)

    assert new_state.answers["toppings"] == ["cheese", "Anchovies"]
    assert new_state.current_step == 1, "custom text on a multi step must not advance"


def test_custom_text_on_a_multi_step_at_its_max_is_a_no_op() -> None:
    """`max` gates the select's max_values, but nothing stops a user
    reopening the custom-text modal -- the transition has to hold the line,
    or the recorded answers grow without bound."""
    capped_spec = WizardSpec(
        prompt="Order form",
        steps=[
            Step(
                key="toppings",
                question="Pick toppings",
                kind=StepKind.MULTI,
                options=[
                    Option(label="Cheese", value="cheese"),
                    Option(label="Olives", value="olives"),
                ],
                max=2,
            )
        ],
    )
    state = WizardState(short_id="abcd1234", answers={"toppings": ["cheese", "Anchovies"]})
    action = WizardAction(kind=ActionKind.ADD_CUSTOM, step_index=0, text="Pineapple")

    new_state = apply(state, capped_spec, action)

    assert new_state == state, "an append past the step's declared max must change nothing"


@pytest.mark.parametrize("raw_text", ["", "   ", "\t\n"])
def test_custom_text_empty_or_whitespace_raises(raw_text: str) -> None:
    state = _state(current_step=0)
    action = WizardAction(kind=ActionKind.ADD_CUSTOM, step_index=0, text=raw_text)

    with pytest.raises(ValueError, match="empty"):
        apply(state, _SPEC, action)


def test_submit_at_review_index_flips_status_to_submitted() -> None:
    state = _state(current_step=len(_SPEC.steps))
    action = WizardAction(kind=ActionKind.SUBMIT)

    new_state = apply(state, _SPEC, action)

    assert new_state.status is WizardStatus.SUBMITTED


@pytest.mark.parametrize("current_step", [0, 1, 2])
def test_submit_anywhere_else_is_a_no_op(current_step: int) -> None:
    state = _state(current_step=current_step)
    action = WizardAction(kind=ActionKind.SUBMIT)

    new_state = apply(state, _SPEC, action)

    assert new_state.status is WizardStatus.OPEN
    assert new_state == state


def test_input_answers_dict_is_not_mutated() -> None:
    original_answers = {"toppings": ["cheese"]}
    state = _state(current_step=1, answers=original_answers)
    action = WizardAction(kind=ActionKind.ADD_CUSTOM, step_index=1, text="Anchovies")

    apply(state, _SPEC, action)

    assert original_answers == {"toppings": ["cheese"]}, (
        "apply must never mutate the caller's answers dict in place"
    )
    assert state.answers is original_answers, "the input state object itself must be untouched"


def test_out_of_range_step_index_raises() -> None:
    state = _state(current_step=0)
    action = WizardAction(kind=ActionKind.SELECT_CHOICE, step_index=99, values=["red"])

    with pytest.raises(ValueError, match="out of range"):
        apply(state, _SPEC, action)


def test_none_step_index_raises_for_value_recording_action() -> None:
    state = _state(current_step=0)
    action = WizardAction(kind=ActionKind.SET_MULTI, step_index=None, values=["cheese"])

    with pytest.raises(ValueError, match="out of range"):
        apply(state, _SPEC, action)


def test_build_action_maps_next_back_submit() -> None:
    assert build_action("next", spec=_SPEC, values=[], text=None) == WizardAction(
        kind=ActionKind.NEXT
    )
    assert build_action("back", spec=_SPEC, values=[], text=None) == WizardAction(
        kind=ActionKind.BACK
    )
    assert build_action("submit", spec=_SPEC, values=[], text=None) == WizardAction(
        kind=ActionKind.SUBMIT
    )


def test_build_action_maps_select_to_set_multi() -> None:
    action = build_action("s1_sel", spec=_SPEC, values=["cheese", "olives"], text=None)

    assert action == WizardAction(
        kind=ActionKind.SET_MULTI, step_index=1, values=["cheese", "olives"]
    )


def test_build_action_rejects_a_select_value_the_step_does_not_declare() -> None:
    """Interaction payloads are untrusted on this path: the button branch
    validates its option index against the spec, so the select branch must
    validate its values the same way."""
    with pytest.raises(ValueError, match="does not declare"):
        build_action("s1_sel", spec=_SPEC, values=["cheese", "anchovies"], text=None)


def test_build_action_rejects_a_select_against_a_non_multi_step() -> None:
    with pytest.raises(ValueError, match="choice step"):
        build_action("s0_sel", spec=_SPEC, values=["red"], text=None)


def test_build_action_rejects_more_select_values_than_the_step_allows() -> None:
    capped_spec = WizardSpec(
        prompt="Order form",
        steps=[
            Step(
                key="toppings",
                question="Pick toppings",
                kind=StepKind.MULTI,
                options=[
                    Option(label="Cheese", value="cheese"),
                    Option(label="Olives", value="olives"),
                ],
                max=1,
            )
        ],
    )

    with pytest.raises(ValueError, match="above step"):
        build_action("s0_sel", spec=capped_spec, values=["cheese", "olives"], text=None)


def test_build_action_maps_choice_button_to_select_choice_with_looked_up_value() -> None:
    action = build_action("s0_c1", spec=_SPEC, values=[], text=None)

    assert action == WizardAction(kind=ActionKind.SELECT_CHOICE, step_index=0, values=["blue"])


def test_build_action_maps_custom_to_add_custom() -> None:
    action = build_action("s0_custom", spec=_SPEC, values=[], text="Purple")

    assert action == WizardAction(kind=ActionKind.ADD_CUSTOM, step_index=0, text="Purple")


def test_build_action_raises_for_enter() -> None:
    with pytest.raises(ValueError, match="modal"):
        build_action("s0_enter", spec=_SPEC, values=[], text=None)


def test_build_action_raises_for_unknown_string() -> None:
    with pytest.raises(ValueError, match="does not match"):
        build_action("totally_unknown", spec=_SPEC, values=[], text=None)


def test_build_action_raises_for_out_of_range_step() -> None:
    with pytest.raises(ValueError, match="out of range"):
        build_action("s9_sel", spec=_SPEC, values=["cheese"], text=None)


def test_build_action_raises_for_out_of_range_option() -> None:
    with pytest.raises(ValueError, match="out of range"):
        build_action("s0_c9", spec=_SPEC, values=[], text=None)
