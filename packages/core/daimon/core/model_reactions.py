"""The emoji that name the model and effort a turn runs on."""

from __future__ import annotations

from typing import Final

_MODELS: Final[tuple[tuple[str, str], ...]] = (
    ("haiku", "🍃"),
    ("sonnet", "🪶"),
    ("opus", "🎼"),
    ("fable", "🦊"),
)
# A waxing moon: the fuller it is, the harder the model thinks.
_EFFORTS: Final[dict[str, str]] = {
    "low": "🌑",
    "medium": "🌒",
    "high": "🌓",
    "xhigh": "🌔",
    "max": "🌕",
}
MODEL_MARKS: Final[frozenset[str]] = frozenset([*(mark for _, mark in _MODELS), *_EFFORTS.values()])
"""Every emoji `model_marks` can return."""


MODEL_MARKS: Final[frozenset[str]] = frozenset(
    {mark for _, mark in _MODELS} | set(_EFFORTS.values())
)
"""Every model and effort emoji, so a rearrangement can tell them from other reactions."""


def model_marks(model_id: str | None, effort: str | None) -> tuple[str, ...]:
    """The model's emoji, then the effort's; one that is unknown or not set is left out."""
    model = next((mark for word, mark in _MODELS if word in (model_id or "").lower()), None)
    level = _EFFORTS.get((effort or "").lower())
    return tuple(mark for mark in (model, level) if mark is not None)


_EFFORT_MARKS: Final[frozenset[str]] = frozenset(_EFFORTS.values())


def swapped_marks(
    shown: tuple[str, ...], model_id: str | None, effort: str | None
) -> tuple[str, ...]:
    """`shown` with the emoji of a newly picked model or effort; one not picked keeps its own."""
    model = model_marks(model_id, None) or tuple(m for m in shown if m not in _EFFORT_MARKS)
    level = model_marks(None, effort) or tuple(m for m in shown if m in _EFFORT_MARKS)
    return (*model, *level)
