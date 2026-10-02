"""A wizard `Screen` as a Teams Adaptive Card.

In core because the MCP server posts the first screen and the Teams adapter
redraws each later one. A card has no modal and no live select, so inputs
ride the buttons: every Action.Execute submits the card's inputs with it. A
multi step's choices are an `Input.ChoiceSet` (`values`, option indices, not
values, since a value may hold a comma) that each button saves first, under
the `sel` action. A text step, and a step that takes the person's own answer,
get an `Input.Text` (`text`) whose button is the `s{n}_custom` action, so
`s{n}_enter` never leaves this module. Plain JSON, as Bot Framework takes it.
"""

from __future__ import annotations

import re
from typing import Final

from daimon.core.wizard.render import Screen, ScreenButton, ScreenSelect

VERB: Final[str] = "wizard"
VALUES_INPUT: Final[str] = "values"
TEXT_INPUT: Final[str] = "text"
# What a typed answer may hold, as Discord's modal allows.
MAX_TEXT_CHARS: Final[int] = 200
_ENTER = re.compile(r"s(\d{1,2})_enter")
_CUSTOM = re.compile(r"s\d{1,2}_custom")
_STYLE: Final[dict[str, str]] = {"primary": "positive", "success": "positive"}


def _text(text: str, **style: object) -> dict[str, object]:
    return {"type": "TextBlock", "text": text, "wrap": True, **style}


def _button(screen: Screen, button: ScreenButton, select: ScreenSelect | None) -> dict[str, object]:
    action, title = button.action, button.label
    if match := _ENTER.fullmatch(action):
        action, title = f"s{match.group(1)}_custom", "Save answer"
    data: dict[str, object] = {"action": VERB, "op": action, "wz": screen.short_id}
    if select is not None:
        data["sel"] = select.action
    rendered: dict[str, object] = {"type": "Action.Execute", "title": title, "verb": VERB}
    if style := _STYLE.get(button.style):
        rendered["style"] = style
    return rendered | {"data": data}


def _select(select: ScreenSelect) -> dict[str, object]:
    chosen = [str(i) for i, option in enumerate(select.options) if option.default]
    return {
        "type": "Input.ChoiceSet",
        "id": VALUES_INPUT,
        "isMultiSelect": True,
        "style": "expanded",
        "choices": [
            {"title": option.label, "value": str(i)} for i, option in enumerate(select.options)
        ],
        "value": ",".join(chosen),
    }


def wizard_card(screen: Screen) -> dict[str, object]:
    """The Adaptive Card for `screen`; a submitted one has no inputs or buttons."""
    head, *rest = screen.head_text.split("\n")
    color = "Good" if screen.accent == "green" else "Default"
    body: list[dict[str, object]] = [_text(head, weight="Bolder", size="Medium", color=color)]
    body += [_text(line, isSubtle=index == 0, spacing="Small") for index, line in enumerate(rest)]
    if screen.body_text:
        body.append(_text(screen.body_text))
    if screen.select is not None:
        body.append(_select(screen.select))
    buttons = [button for row in screen.button_rows for button in row]
    if any(_ENTER.fullmatch(b.action) or _CUSTOM.fullmatch(b.action) for b in buttons):
        typed = any(_ENTER.fullmatch(b.action) for b in buttons)
        body.append(
            {
                "type": "Input.Text",
                "id": TEXT_INPUT,
                "isMultiline": typed,
                "maxLength": MAX_TEXT_CHARS,
                "placeholder": "Your answer" if typed else "Or type your own",
            }
        )
    for row in screen.button_rows:
        actions = [_button(screen, button, screen.select) for button in row]
        body.append({"type": "ActionSet", "actions": actions})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "fallbackText": head,
    }
