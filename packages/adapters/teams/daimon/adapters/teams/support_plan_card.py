"""Ask a person's menu, option form and support post cards for a plan's request.

Pure: builds and reads cards only. The menu and the picked option's form are
two cards (a dialog's `continue`, or the typed form's Next), as Slack
re-renders its modal. A pending request's post carries its charge input and
Set charge / Decline. A bot cannot read a channel post back, so the buttons
carry the request id and the post's lines, enough to close it after a restart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from daimon.adapters.teams.card_actions import clip, error_text, text_lines
from daimon.core.stores.support_rows import PlanView, SupportEscalationRow
from daimon.core.support_escalation import SEALED_NOTE_HINT
from daimon.core.support_plans import (
    CHARGE_FIELD,
    CHOOSE_OPTION,
    CLIENT_NOT_NOTIFIED,
    DECLINE,
    DEFAULT_FIELD_LABEL,
    EXEMPT_OFFER,
    MAX_PARAGRAPH_CHARS,
    SET_CHARGE,
    ChoiceField,
    ParagraphField,
    RequestChoice,
    StepsPrice,
    SupportOption,
    TextField,
    balance_text,
    option_text,
    option_value,
    post_status_text,
    price_bounds,
    price_text,
    step_text,
    validate_answers,
)
from microsoft_teams.cards import (
    Action,
    ActionSet,
    AdaptiveCard,
    CardElement,
    Choice,
    ChoiceSetInput,
    ExecuteAction,
    NumberInput,
    TextBlock,
    TextInput,
)

OPTION_INPUT: Final = "option"
STEP_INPUT: Final = "step"
CHARGE_INPUT: Final = "charge"
SETTLE_VERB: Final = "support_settle"
NEXT: Final = "Next"
SEND: Final = "Send"
TITLE: Final = "Ask a person"
# Teams' form prompt, the default note's label: a typed `support` has no answer to point at.
MENU_PROMPT: Final = DEFAULT_FIELD_LABEL
# The core note limit, under Discord's 4000, so no platform takes a note the core refuses.
MAX_NOTE_CHARS: Final = min(MAX_PARAGRAPH_CHARS, 4000)


def field_input(key: str) -> str:
    return f"field_{key}"


def subtle(text: str) -> TextBlock:
    return TextBlock(text=text, is_subtle=True, size="Small", wrap=True, spacing="Medium")


def footer(view: PlanView) -> str | None:
    """What a request uses here, when it is not the default plan's request count."""
    return EXEMPT_OFFER if view.exempt else balance_text(view.balance, view.plan)


def menu_body(view: PlanView, *, sealed: bool, error: str | None = None) -> list[CardElement]:
    """The menu: each option with its price, the balance, and one pick."""
    body: list[CardElement] = [error_text(error)] if error else []
    body += text_lines(MENU_PROMPT)
    hints = (footer(view), SEALED_NOTE_HINT if sealed else None)
    body += [subtle(text) for text in hints if text]
    choices = [
        Choice(title=f"{o.label}: {price_text(o.price)}", value=option_value(o))
        for o in view.spec.options
    ]
    body.append(
        ChoiceSetInput(
            id=OPTION_INPUT,
            label=CHOOSE_OPTION,
            is_required=True,
            style="expanded",
            choices=choices,
        )
    )
    return body


def _field(spec: TextField | ParagraphField | ChoiceField, value: str) -> CardElement:
    if isinstance(spec, ChoiceField):
        return ChoiceSetInput(
            id=field_input(spec.key),
            label=spec.label,
            is_required=spec.required,
            placeholder=spec.placeholder,
            value=value or None,
            choices=[Choice(title=choice, value=choice) for choice in spec.choices],
        )
    paragraph = isinstance(spec, ParagraphField)
    return TextInput(
        id=field_input(spec.key),
        label=spec.label,
        is_required=spec.required,
        placeholder=spec.placeholder,
        is_multiline=paragraph,
        max_length=min(spec.max_chars, MAX_NOTE_CHARS) if paragraph else spec.max_chars,
        value=value or None,
    )


def option_body(
    option: SupportOption,
    *,
    error: str | None = None,
    values: Mapping[str, object] | None = None,
) -> list[CardElement]:
    """A picked option's terms, its step and its fields; `values` refill a form sent back."""
    values = values or {}
    body: list[CardElement] = [error_text(error)] if error else []
    body.append(TextBlock(text=f"**{option.label}**: {price_text(option.price)}", wrap=True))
    if option.description:
        body.append(subtle(option.description))
    if isinstance(option.price, StepsPrice):
        steps = [Choice(title=step_text(s), value=str(i)) for i, s in enumerate(option.price.steps)]
        picked = str(values.get(STEP_INPUT) or "")
        body.append(
            ChoiceSetInput(
                id=STEP_INPUT,
                label=option.price.label,
                is_required=True,
                style="expanded",
                value=picked or None,
                choices=steps,
            )
        )
    body += [_field(f, str(values.get(field_input(f.key)) or "")) for f in option.fields]
    return body


def read_choice(data: Mapping[str, object], option: SupportOption) -> RequestChoice:
    """The choice a form sent; a missing step or a bad answer raises `SupportPlanError`.

    Checked here so the form comes back with the reason; `submit_request` checks again.
    """
    raw = str(data.get(STEP_INPUT) or "")
    step = int(raw) if raw.isdigit() else None
    price_bounds(option.price, step=step)
    answers = {f.key: str(data.get(field_input(f.key)) or "") for f in option.fields}
    return RequestChoice.of(option, step=step, answers=validate_answers(option, answers))


def status_line(row: SupportEscalationRow) -> str:
    return post_status_text(
        status=row.status, charged_to=row.charged_to, charge=row.charge, bounds=row.bounds
    )


def request_facts(row: SupportEscalationRow, *, menu: bool, balance: str | None) -> list[str]:
    """A plan request's lines: its terms on a configured menu, status and balance."""
    lines = [option_text(row.option)] if menu and row.option is not None else []
    lines.append(status_line(row))
    return [*lines, balance] if balance else lines


def _note_blocks(note: str) -> list[CardElement]:
    return text_lines(*(line for line in clip(note, MAX_NOTE_CHARS).splitlines() if line.strip()))


def settle_card(row: SupportEscalationRow, lines: Sequence[str]) -> AdaptiveCard:
    """The support channel's post of a pending request: `lines`, the note, the charge and buttons.

    The input starts at the request's hold; Decline sends no input.
    """
    shown = [line for line in lines if line]
    data: dict[str, object] = {"action": SETTLE_VERB, "request": row.id.hex}
    actions: list[Action] = [
        ExecuteAction(
            title=SET_CHARGE, verb=SETTLE_VERB, data=data | {"op": "set"}, style="positive"
        ),
        ExecuteAction(
            title=DECLINE, verb=SETTLE_VERB, data=data | {"op": "decline"}, associated_inputs="none"
        ),
    ]
    return AdaptiveCard(
        body=[
            *text_lines(*shown),
            subtle(CLIENT_NOT_NOTIFIED),
            *_note_blocks(row.note),
            NumberInput(id=CHARGE_INPUT, label=CHARGE_FIELD, value=float(row.hold), min=0),
            ActionSet(actions=actions),
        ],
        fallback_text=shown[0] if shown else TITLE,
    )


def closed_card(
    lines: Sequence[str],
    *,
    before: SupportEscalationRow,
    after: SupportEscalationRow,
    by: str | None,
) -> AdaptiveCard:
    """The post once settled: the final status and settler in place of the pending line."""
    pending, final = status_line(before), status_line(after)
    final = f"{final} by {by}" if by else final
    shown = [final if line == pending else line for line in lines if line]
    if final not in shown:
        shown.append(final)
    return AdaptiveCard(
        body=[*text_lines(*shown), *_note_blocks(after.note)],
        fallback_text=shown[0],
    )


__all__ = [
    "CHARGE_INPUT",
    "MAX_NOTE_CHARS",
    "MENU_PROMPT",
    "NEXT",
    "OPTION_INPUT",
    "SEND",
    "SETTLE_VERB",
    "STEP_INPUT",
    "closed_card",
    "field_input",
    "footer",
    "menu_body",
    "option_body",
    "read_choice",
    "request_facts",
    "settle_card",
    "status_line",
    "subtle",
]
