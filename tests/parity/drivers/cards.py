"""One reader for a posted control card, whichever platform drew it.

The lifecycle scenarios assert on what a person would actually see in the
channel, so they need the card back as data after it has gone through a
platform's own serializer: Discord's components-v2 payload
(`LayoutView.to_components()`, which is byte-identical to the JSON the REST
POST carries) or Slack's Block Kit list. Both renderers draw the same four
slots in the same order — headline, facts, buttons, footer — so one reader
per platform, returning one shared `CapturedCard`, is enough, and the
scenarios themselves stay platform-agnostic. Teams draws the same slots as an
Adaptive Card.

State comes back from `classify_card_state`, which reads the headline's
leading emoji and is deliberately lossy in two places (a `received` card
repeats the `requested` headline; `superseded` shares ⚠️ with `partial`).
These are recoverable from the rest of the card, so this module refines them:
`RECEIVED_FOOTER` separates received from requested, and the one line only
`_superseded_content` writes separates superseded from partial. The third
collision, `replaced` against `expired` on ⌛, needs no refinement here:
`replaced` writes one fixed headline, so core matches it exactly and this
reader gets the right state back already. A retryable failure also uses ⚠️;
its private-input button distinguishes it from a terminal partial receipt.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from daimon.core.posted_controls import RECEIVED_FOOTER, CardState, classify_card_state

__all__ = [
    "CapturedCard",
    "read_discord_card",
    "read_slack_card",
    "read_teams_card",
    "walk_components",
]

#: The second line of `cards._superseded_content`, and the only line no other
#: ⚠️ card writes — a `partial` card's facts come from
#: `render_change_confirmation` instead. Duplicated rather than imported
#: because core exports the state's copy only through `build_posted_card`,
#: which needs a whole request to build one.
_SUPERSEDED_MARKER = "Someone else changed it while this form was open."

#: discord.py's component type ids for the two things this reader pulls out.
_DISCORD_BUTTON_TYPE = 2


@dataclass(frozen=True)
class CapturedCard:
    """One posted or edited card, as the channel shows it."""

    state: CardState
    headline: str
    facts: tuple[str, ...]
    button_labels: tuple[str, ...]


def _refine_state(
    headline: str, facts: tuple[str, ...], footer: str | None, button_labels: tuple[str, ...]
) -> CardState:
    """The card's real state, undoing `classify_card_state`'s two collisions."""
    state = classify_card_state(headline)
    if state is None:
        raise ValueError(f"no state emoji on the rendered headline {headline!r}")
    if state == "requested" and footer == RECEIVED_FOOTER:
        return "received"
    if state == "partial" and _SUPERSEDED_MARKER in facts:
        return "superseded"
    if state == "partial" and button_labels:
        return "requested"
    return state


def _build(
    *, headline: str, facts: tuple[str, ...], footer: str | None, button_labels: tuple[str, ...]
) -> CapturedCard:
    return CapturedCard(
        state=_refine_state(headline, facts, footer, button_labels),
        headline=headline,
        facts=facts,
        button_labels=button_labels,
    )


def walk_components(node: object) -> Iterator[dict[str, Any]]:
    """Every component dict in a components-v2 payload, in render order."""
    if isinstance(node, dict):
        yield node  # pyright: ignore[reportUnknownArgumentType]
        for value in node.values():  # pyright: ignore[reportUnknownVariableType]
            yield from walk_components(value)
    elif isinstance(node, list):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            yield from walk_components(item)


def read_discord_card(components: object) -> CapturedCard:
    """Read a card off a `LayoutView.to_components()` payload.

    The view is a container holding, in order, the bolded headline, the facts
    as one `-# `-prefixed text display, then — only when the card has
    buttons — a separator and an action row, then the footer as its own text
    display. So the text displays are positional: headline, facts, footer.
    Every state that carries a footer also carries facts (`build_posted_card`
    only footers the two states built from `_requested_content`), which is
    what makes the third slot unambiguous.
    """
    texts: list[str] = []
    labels: list[str] = []
    for component in walk_components(components):
        content = component.get("content")
        if isinstance(content, str):
            texts.append(content)
        label = component.get("label")
        if component.get("type") == _DISCORD_BUTTON_TYPE and isinstance(label, str):
            labels.append(label)
    if not texts:
        raise ValueError("a posted card always renders at least its headline")
    headline = texts[0].removeprefix("**").removesuffix("**")
    facts = (
        tuple(line.removeprefix("-# ") for line in texts[1].split("\n")) if len(texts) > 1 else ()
    )
    footer = texts[2].removeprefix("-# ") if len(texts) > 2 else None
    return _build(headline=headline, facts=facts, footer=footer, button_labels=tuple(labels))


def _slack_context_lines(block: dict[str, Any]) -> tuple[str, ...]:
    """The lines of one context block, whether or not they were folded.

    `build_card_blocks` folds facts into a single element once there are more
    than Slack allows, so a line is an element or a newline inside one.
    """
    elements: list[dict[str, Any]] = block.get("elements") or []
    lines: list[str] = []
    for element in elements:
        text = element.get("text")
        if isinstance(text, str):
            lines.extend(text.split("\n"))
    return tuple(lines)


def read_slack_card(blocks: object) -> CapturedCard:
    """Read a card off the Block Kit list `build_card_blocks` produced.

    Same positional contract as the Discord reader: one section (the bolded
    headline), then the facts context, then the actions block, then the
    footer context.
    """
    if not isinstance(blocks, list):
        raise ValueError(f"expected a Block Kit list, got {type(blocks).__name__}")
    typed: list[dict[str, Any]] = [b for b in blocks if isinstance(b, dict)]  # pyright: ignore[reportUnknownVariableType]
    sections = [b for b in typed if b.get("type") == "section"]
    contexts = [b for b in typed if b.get("type") == "context"]
    actions = [b for b in typed if b.get("type") == "actions"]
    if not sections:
        raise ValueError("a posted card always renders its headline as a section")
    headline_text: dict[str, Any] = sections[0].get("text") or {}
    headline = str(headline_text.get("text") or "").removeprefix("*").removesuffix("*")
    facts = _slack_context_lines(contexts[0]) if contexts else ()
    footer = _slack_context_lines(contexts[1])[0] if len(contexts) > 1 else None
    labels: list[str] = []
    for block in actions:
        elements: list[dict[str, Any]] = block.get("elements") or []
        for element in elements:
            label: dict[str, Any] = element.get("text") or {}
            labels.append(str(label.get("text") or ""))
    return _build(headline=headline, facts=facts, footer=footer, button_labels=tuple(labels))


def read_teams_card(card: object) -> CapturedCard:
    """Read a card off the Adaptive Card `build_adaptive_card` produced.

    The headline is the first TextBlock, each fact a subtle one after it, the
    buttons one ActionSet and the footer the one small TextBlock.
    """
    if not isinstance(card, dict):
        raise ValueError(f"expected an Adaptive Card, got {type(card).__name__}")
    body: list[dict[str, Any]] = [b for b in card.get("body") or [] if isinstance(b, dict)]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    texts = [b for b in body if b.get("type") == "TextBlock"]
    if not texts:
        raise ValueError("a posted card always renders its headline")
    small = [str(b.get("text")) for b in texts[1:] if b.get("size") == "Small"]
    facts = tuple(str(b.get("text")) for b in texts[1:] if b.get("size") != "Small")
    labels: list[str] = []
    for block in body:
        if block.get("type") == "ActionSet":
            actions: list[dict[str, Any]] = block.get("actions") or []
            labels.extend(str(action.get("title") or "") for action in actions)
    return _build(
        headline=str(texts[0].get("text")),
        facts=facts,
        footer=small[0] if small else None,
        button_labels=tuple(labels),
    )
