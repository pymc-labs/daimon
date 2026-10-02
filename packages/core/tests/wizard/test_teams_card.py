"""The wizard's Adaptive Card: inputs ride the buttons, and no modal opener is emitted."""

from __future__ import annotations

from typing import Any, cast

from daimon.core.wizard.render import to_screen
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.core.wizard.state import WizardState, WizardStatus
from daimon.core.wizard.teams_card import VERB, wizard_card

_SPEC = WizardSpec(
    prompt="Order form",
    steps=[
        Step(
            key="color",
            question="Pick a color",
            kind=StepKind.CHOICE,
            options=[Option(label="Red", value="red"), Option(label="Blue", value="a,b")],
        ),
        Step(
            key="toppings",
            question="Pick toppings",
            kind=StepKind.MULTI,
            options=[Option(label="Cheese", value="cheese"), Option(label="Olives", value="o")],
            allow_custom=False,
        ),
        Step(key="notes", question="Any notes?", kind=StepKind.TEXT),
    ],
)


def _card(step: int, **kwargs: Any) -> dict[str, Any]:
    return cast(
        dict[str, Any], wizard_card(to_screen(_SPEC, WizardState("abcd1234", step, **kwargs)))
    )


def _of(card: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [element for element in card["body"] if element["type"] == kind]


def _actions(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [action for row in _of(card, "ActionSet") for action in row["actions"]]


def test_a_choice_step_has_a_button_per_option_and_a_box_for_its_own_answer() -> None:
    card = _card(0)
    ops = [a["data"]["op"] for a in _actions(card)]
    assert ops == ["s0_c0", "s0_c1", "s0_custom", "back", "next"]
    assert all(a["verb"] == VERB and a["data"]["wz"] == "abcd1234" for a in _actions(card))
    [box] = _of(card, "Input.Text")
    assert box["id"] == "text" and not box["isMultiline"]
    assert card["body"][0]["text"] == "Order form"


def test_a_multi_step_submits_option_indices_with_every_button() -> None:
    card = _card(1, answers={"toppings": ["o"]})
    [choices] = _of(card, "Input.ChoiceSet")
    assert choices["isMultiSelect"] and choices["value"] == "1", "indices, never raw values"
    assert [c["value"] for c in choices["choices"]] == ["0", "1"]
    assert {a["data"]["sel"] for a in _actions(card)} == {"s1_sel"}
    assert _of(card, "Input.Text") == [], "no own-answer box when allow_custom is off"


def test_a_text_step_saves_through_the_custom_action() -> None:
    card = _card(2)
    ops = [a["data"]["op"] for a in _actions(card)]
    assert "s2_enter" not in ops and ops[0] == "s2_custom"
    assert _of(card, "Input.Text")[0]["isMultiline"]


def test_review_then_submitted_screens() -> None:
    review = _card(3, answers={"color": ["red"]})
    assert [a["data"]["op"] for a in _actions(review)] == ["back", "submit"]
    done = _card(3, answers={"color": ["red"]}, status=WizardStatus.SUBMITTED)
    assert _actions(done) == [] and done["body"][0]["color"] == "Good"
