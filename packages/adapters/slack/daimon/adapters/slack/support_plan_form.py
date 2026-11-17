"""The Ask a person form for a configured menu: one modal, re-rendered on each pick.

Pure: builds and reads views only. Picking an option (`dispatch_action`)
re-renders the modal with that option's step and fields below; one Send
submits. Block ids carry the option key, so switching options starts its
fields empty. Each option's value, and `private_metadata`'s pick, carry the
option's terms (`option_value`); `private_metadata` never carries identity.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Final, cast

from daimon.adapters.slack.modal_limits import MAX_PLAIN_TEXT_INPUT_CHARS, finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.stores.support_rows import PlanView
from daimon.core.support_escalation import ASK_THE_TEAM, SEALED_NOTE_HINT, offer_text
from daimon.core.support_plans import (
    CHOOSE_OPTION,
    EXEMPT_OFFER,
    NOT_ENOUGH_CREDITS,
    ChoiceField,
    ParagraphField,
    RequestChoice,
    StepsPrice,
    SupportOption,
    balance_text,
    credits_text,
    option_value,
    price_text,
)

PLAN_CALLBACK_ID: Final = "support_plan_request"
OPTION_ACTION_ID: Final = "support_option_pick"
OPTION_BLOCK_ID: Final = "support_option_block"
_STEP_ACTION_ID: Final = "support_step_input"
_FIELD_ACTION_ID: Final = "support_field_input"
FORM_LOADING: Final = "Wait a moment, then press Send again."
# The note form's first line, without its count.
OFFER_PROMPT: Final = offer_text(remaining=0).split("\n", 1)[0]


def _plain(text: str) -> dict[str, Any]:
    return {"type": "plain_text", "text": text}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _step_block(key: str) -> str:
    return f"support_step:{key}"


def _field_prefix(key: str) -> str:
    return f"support_field:{key}:"


def _radio(text: str, value: str, description: str | None = None) -> dict[str, Any]:
    option: dict[str, Any] = {"text": _plain(text), "value": value}
    if description:
        option["description"] = _plain(description)
    return option


def _option_blocks(option: SupportOption) -> list[dict[str, Any]]:
    terms = f"*{escape_mrkdwn(option.label)}*: {price_text(option.price)}"
    if option.description:
        terms += f"\n{escape_mrkdwn(option.description)}"
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": terms}}]
    if isinstance(option.price, StepsPrice):
        steps = [
            _radio(step.label, str(index), credits_text(step.amount))
            for index, step in enumerate(option.price.steps)
        ]
        blocks.append(
            {
                "type": "input",
                "block_id": _step_block(option.key),
                "label": _plain(option.price.label),
                "element": {
                    "type": "radio_buttons",
                    "action_id": _STEP_ACTION_ID,
                    "options": steps,
                },
            }
        )
    for field in option.fields:
        element: dict[str, Any]
        if isinstance(field, ChoiceField):
            element = {
                "type": "static_select",
                "action_id": _FIELD_ACTION_ID,
                "options": [_radio(choice, choice) for choice in field.choices],
            }
        else:
            element = {
                "type": "plain_text_input",
                "action_id": _FIELD_ACTION_ID,
                "multiline": isinstance(field, ParagraphField),
                "max_length": min(field.max_chars, MAX_PLAIN_TEXT_INPUT_CHARS),
            }
        if field.placeholder:
            element["placeholder"] = _plain(field.placeholder)
        blocks.append(
            {
                "type": "input",
                "block_id": _field_prefix(option.key) + field.key,
                "label": _plain(field.label),
                "optional": not field.required,
                "element": element,
            }
        )
    return blocks


def build_plan_menu(
    *,
    channel_id: str,
    message_ts: str,
    thread_ts: str,
    view: PlanView,
    sealed: bool,
    picked: str | None = None,
    refused: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """The menu with each option's price and the balance; a picked option adds its form.

    `refused` is a pick that opens no form (already asked, or the balance cannot
    cover it), with the reason: Slack keeps it selected, so the menu says why
    and a Send on it is refused the same way.
    """
    options = view.spec.options
    choices = [_radio(o.label, option_value(o), price_text(o.price)) for o in options]
    option = view.spec.option(picked) if picked else None
    radio: dict[str, Any] = {
        "type": "radio_buttons",
        "action_id": OPTION_ACTION_ID,
        "options": choices,
    }
    if option is not None:
        radio["initial_option"] = choices[options.index(option)]
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": OFFER_PROMPT}}
    ]
    footer = EXEMPT_OFFER if view.exempt else balance_text(view.balance, view.plan)
    problem = refused[1] if refused else None
    hints = (footer, SEALED_NOTE_HINT if sealed else None, problem)
    blocks.extend(_context(text) for text in hints if text)
    blocks.append(
        {
            "type": "input",
            "block_id": OPTION_BLOCK_ID,
            "dispatch_action": True,
            "label": _plain(CHOOSE_OPTION),
            "element": radio,
        }
    )
    if option is not None:
        blocks.extend(_option_blocks(option))
    meta: dict[str, Any] = {
        "channel_id": channel_id,
        "message_ts": message_ts,
        "thread_ts": thread_ts,
    }
    if option is not None:
        meta["option"] = option_value(option)
    if refused and (refused_option := view.spec.option(refused[0])) is not None:
        meta["refused"] = option_value(refused_option)
        meta["refusal"] = refused[1]
    return finish_modal(
        title=ASK_THE_TEAM,
        blocks=blocks,
        private_metadata=json.dumps(meta, separators=(",", ":")),
        callback_id=PLAN_CALLBACK_ID,
        close="Cancel",
        submit="Send",
    )


@dataclasses.dataclass(frozen=True)
class PlanForm:
    """A read menu form: its routing handles, and the choice or the field errors."""

    channel_id: str
    message_ts: str
    thread_ts: str
    choice: RequestChoice | None
    errors: dict[str, str] | None


def _dict(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _selected(element: dict[str, Any]) -> str:
    return str(_dict(element.get("selected_option")).get("value") or "")


def form_meta(view: dict[str, Any]) -> dict[str, Any]:
    """The view's routing handles; anything unreadable is empty."""
    try:
        return _dict(json.loads(str(view.get("private_metadata") or "") or "{}"))
    except json.JSONDecodeError:
        return {}


def read_plan_form(view: dict[str, Any]) -> PlanForm:
    """Pure read of a submitted menu form; a pick not rendered yet asks for a moment."""
    meta = form_meta(view)
    values = _dict(_dict(view.get("state")).get("values"))
    message_ts = str(meta.get("message_ts") or "")
    form = PlanForm(
        channel_id=str(meta.get("channel_id") or ""),
        message_ts=message_ts,
        thread_ts=str(meta.get("thread_ts") or "") or message_ts,
        choice=None,
        errors=None,
    )
    rendered = str(meta.get("option") or "")
    picked = _selected(_dict(_dict(values.get(OPTION_BLOCK_ID)).get(OPTION_ACTION_ID)))
    if picked and picked == meta.get("refused"):
        refusal = str(meta.get("refusal") or NOT_ENOUGH_CREDITS)
        return dataclasses.replace(form, errors={OPTION_BLOCK_ID: refusal})
    if not rendered or picked != rendered:
        return dataclasses.replace(form, errors={OPTION_BLOCK_ID: FORM_LOADING})
    key, _, terms = rendered.partition(":")
    step_raw = _selected(_dict(_dict(values.get(_step_block(key))).get(_STEP_ACTION_ID)))
    answers: dict[str, str] = {}
    prefix = _field_prefix(key)
    for block_id, block in values.items():
        if not block_id.startswith(prefix):
            continue
        element = _dict(_dict(block).get(_FIELD_ACTION_ID))
        value = element.get("value")
        answers[block_id.removeprefix(prefix)] = (
            str(value) if isinstance(value, str) else _selected(element)
        )
    step = int(step_raw) if step_raw.isdigit() else None
    choice = RequestChoice(key, terms, step=step, answers=answers)
    return dataclasses.replace(form, choice=choice)


__all__ = [
    "FORM_LOADING",
    "OFFER_PROMPT",
    "OPTION_ACTION_ID",
    "OPTION_BLOCK_ID",
    "PLAN_CALLBACK_ID",
    "PlanForm",
    "build_plan_menu",
    "form_meta",
    "read_plan_form",
]
