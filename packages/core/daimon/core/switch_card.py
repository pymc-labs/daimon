"""When a model switch asks first, and the one short card it shows when it does.

One rule for every surface (`needs_switch_card`): a switch that loses nothing applies at
once, like a pick between Claude models; one that loses something asks once and names it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final

from daimon.core.confirmation import loss_word

if TYPE_CHECKING:
    from daimon.core.stores.domain import CompatibilityPreview

_WORDS_LIMIT: Final = 500
#: The card's two buttons, worded the same on every surface.
SWITCH_LABEL: Final = "Switch"
CANCEL_LABEL: Final = "Cancel"


def needs_switch_card(preview: CompatibilityPreview | None) -> bool:
    """Whether a switch asks first: something stops working, or a shared-thread limit applies."""
    return preview is not None and bool(preview.declared_drops or preview.shared_thread_limits)


def switch_title(agent_name: str | None, label: str) -> str:
    """'Switch Ada to GPT-5?'; a pick for one conversation names it instead."""
    return f"Switch {agent_name or 'this conversation'} to {label}?"


def loss_words(drops: Sequence[str], names: Mapping[str, str] | None = None) -> str:
    """'saved keys, memory and web search': saved keys first, as the costliest."""
    ordered = sorted(drops, key=lambda item: (item != "credentials", item))
    words = [loss_word(d, names) for d in ordered]
    joined = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return joined if len(joined) <= _WORDS_LIMIT else joined[:_WORDS_LIMIT] + "…"


def switch_card_lines(
    preview: CompatibilityPreview, *, escape: Callable[[str], str] = str
) -> tuple[str, ...]:
    """The card's facts under its title: what stops working, then any shared-thread limit."""
    lines = (
        [f"Stops working: {escape(loss_words(preview.declared_drops, preview.skill_names))}"]
        if preview.declared_drops
        else []
    )
    lines.extend(escape(limit) for limit in preview.shared_thread_limits)
    return tuple(lines)
