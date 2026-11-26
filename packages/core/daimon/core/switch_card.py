"""What a model switch turns off, said once as information after it applies.

A switch applies at once on every surface, like a pick between Claude models. Anything the
new provider can't carry is off there and comes back on a switch back (`switch_lines`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final

from daimon.core.confirmation import loss_word
from daimon.core.turn.notices import provider_name

if TYPE_CHECKING:
    from daimon.core.stores.domain import CompatibilityPreview

_WORDS_LIMIT: Final = 500


def loss_words(drops: Sequence[str], names: Mapping[str, str] | None = None) -> str:
    """'saved keys, memory and web search': saved keys first, as the costliest."""
    ordered = sorted(drops, key=lambda item: (item != "credentials", item))
    words = [loss_word(d, names) for d in ordered]
    joined = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return joined if len(joined) <= _WORDS_LIMIT else joined[:_WORDS_LIMIT] + "…"


def switch_lines(
    preview: CompatibilityPreview, *, escape: Callable[[str], str] = str
) -> tuple[str, ...]:
    """What is off on the new provider, then any shared-thread limit; empty when nothing is."""
    lines: list[str] = []
    if preview.declared_drops:
        words = escape(loss_words(preview.declared_drops, preview.skill_names))
        name = provider_name(preview.target_backend)
        lines.append(f"Off on {name}: {words}. They come back when you switch back.")
    lines.extend(escape(limit) for limit in preview.shared_thread_limits)
    return tuple(lines)
