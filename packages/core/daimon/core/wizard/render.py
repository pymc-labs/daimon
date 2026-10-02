"""The platform-neutral `Screen` description every wizard renderer consumes.

`to_screen` is the one place that decides what a wizard run currently looks
like — head text, an optional image, an optional select, and button rows —
without ever mentioning a Discord type. The Discord and Teams renderers
read a `Screen` and translate it into their own component tree; neither
re-derives navigation, review, or submitted-screen behaviour, because that
behaviour lives here and in
`daimon.core.wizard.apply`.

Every action string this module emits is validated through
`state.build_custom_id` at construction time — an unroutable or oversized
action can never leave `to_screen`; the `ValueError` `build_custom_id`
raises is allowed to propagate rather than being swallowed, because a
renderer emitting a control nobody can dispatch is a bug in this module,
not a runtime condition a caller should recover from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from daimon.core.wizard.answers import summarize_answers
from daimon.core.wizard.spec import MAX_BUTTONS_PER_ROW, Step, StepKind, WizardSpec
from daimon.core.wizard.state import WizardState, WizardStatus, build_custom_id

ScreenStyle = Literal["primary", "secondary", "success"]


@dataclass(frozen=True)
class ScreenButton:
    """One tappable button on a `Screen`."""

    action: str
    label: str
    style: ScreenStyle
    disabled: bool = False


@dataclass(frozen=True)
class ScreenSelectOption:
    """One option of a `ScreenSelect`."""

    label: str
    value: str
    description: str | None
    default: bool


@dataclass(frozen=True)
class ScreenSelect:
    """The multi-select control on a `multi` step's screen."""

    action: str
    placeholder: str
    min_values: int
    max_values: int
    options: tuple[ScreenSelectOption, ...]


@dataclass(frozen=True)
class ScreenImage:
    """The image a step's screen carries, by handle and/or persisted URL.

    A renderer prefers a matching attachment (`handle`) when it has one and
    falls back to `url`; when both are `None` the image is omitted.
    """

    handle: str | None
    url: str | None


@dataclass(frozen=True)
class Screen:
    """One platform-neutral description of what a wizard run currently shows."""

    short_id: str
    accent: Literal["blurple", "green"]
    head_text: str
    image: ScreenImage | None
    body_text: str | None
    select: ScreenSelect | None
    button_rows: tuple[tuple[ScreenButton, ...], ...]


def _nav_row(short_id: str, step: Step, step_index: int) -> tuple[ScreenButton, ...]:
    buttons: list[ScreenButton] = []
    show_custom_button = step.allow_custom and step.kind is not StepKind.TEXT
    if show_custom_button:
        custom_action = f"s{step_index}_custom"
        build_custom_id(short_id, custom_action)
        buttons.append(ScreenButton(action=custom_action, label="Add your own…", style="secondary"))
    build_custom_id(short_id, "back")
    buttons.append(ScreenButton(action="back", label="Back", style="secondary"))
    build_custom_id(short_id, "next")
    buttons.append(ScreenButton(action="next", label="Next", style="primary"))
    return tuple(buttons)


def _chunk_buttons(buttons: list[ScreenButton], size: int) -> list[tuple[ScreenButton, ...]]:
    return [tuple(buttons[i : i + size]) for i in range(0, len(buttons), size)]


def _step_screen(spec: WizardSpec, state: WizardState) -> Screen:
    step_index = state.current_step
    step = spec.steps[step_index]
    step_count = len(spec.steps)
    head_text = f"{spec.prompt}\nStep {step_index + 1} of {step_count}\n{step.question}"

    image: ScreenImage | None = None
    if step.image_handle is not None or step.image_url is not None:
        image = ScreenImage(handle=step.image_handle, url=step.image_url)

    select: ScreenSelect | None = None
    button_rows: list[tuple[ScreenButton, ...]] = []

    if step.kind is StepKind.CHOICE:
        option_buttons: list[ScreenButton] = []
        for option_index, option in enumerate(step.options):
            action = f"s{step_index}_c{option_index}"
            build_custom_id(state.short_id, action)
            option_buttons.append(
                ScreenButton(action=action, label=option.label, style="secondary")
            )
        button_rows.extend(_chunk_buttons(option_buttons, MAX_BUTTONS_PER_ROW))
    elif step.kind is StepKind.MULTI:
        select_action = f"s{step_index}_sel"
        build_custom_id(state.short_id, select_action)
        recorded = set(state.answers.get(step.key, []))
        max_values = step.max if step.max is not None else len(step.options)
        select = ScreenSelect(
            action=select_action,
            placeholder=step.question,
            min_values=step.min if step.min is not None else 0,
            max_values=max_values,
            options=tuple(
                ScreenSelectOption(
                    label=option.label,
                    value=option.value,
                    description=option.description,
                    default=option.value in recorded,
                )
                for option in step.options
            ),
        )
    else:  # StepKind.TEXT
        enter_action = f"s{step_index}_enter"
        build_custom_id(state.short_id, enter_action)
        button_rows.append((ScreenButton(action=enter_action, label="Enter…", style="primary"),))

    button_rows.append(_nav_row(state.short_id, step, step_index))

    return Screen(
        short_id=state.short_id,
        accent="blurple",
        head_text=head_text,
        image=image,
        body_text=None,
        select=select,
        button_rows=tuple(button_rows),
    )


def _review_screen(spec: WizardSpec, state: WizardState) -> Screen:
    build_custom_id(state.short_id, "back")
    build_custom_id(state.short_id, "submit")
    return Screen(
        short_id=state.short_id,
        accent="blurple",
        head_text=f"{spec.prompt}\nReview your answers",
        image=None,
        body_text=summarize_answers(spec, state),
        select=None,
        button_rows=(
            (
                ScreenButton(action="back", label="Back", style="secondary"),
                ScreenButton(action="submit", label="Submit", style="success"),
            ),
        ),
    )


def _collapsed_screen(spec: WizardSpec, state: WizardState) -> Screen:
    return Screen(
        short_id=state.short_id,
        accent="green",
        head_text=f"{spec.prompt}\nSubmitted",
        image=None,
        body_text=summarize_answers(spec, state),
        select=None,
        button_rows=(),
    )


def to_screen(spec: WizardSpec, state: WizardState) -> Screen:
    """Return the `Screen` a wizard run currently shows.

    One of three screens: collapsed (submitted, no controls), review (the
    step after the last real step), or the step screen for
    `spec.steps[state.current_step]`.
    """
    if state.status is WizardStatus.SUBMITTED:
        return _collapsed_screen(spec, state)
    if state.current_step >= len(spec.steps):
        return _review_screen(spec, state)
    return _step_screen(spec, state)
