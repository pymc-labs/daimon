"""The emoji that name the model and effort a turn runs on."""

from __future__ import annotations

from typing import Final

_MODELS: Final[tuple[tuple[str, str], ...]] = (
    ("haiku", "🍃"),
    ("sonnet", "🪶"),
    ("opus", "🎼"),
    ("fable", "🦊"),
)
_EFFORTS: Final[dict[str, str]] = {
    "low": "🌑",
    "medium": "🌘",
    "high": "🌓",
    "xhigh": "🌖",
    "max": "🌕",
}


def model_marks(model_id: str | None, effort: str | None) -> tuple[str, ...]:
    """The model's emoji, then the effort's; one that is unknown or not set is left out."""
    model = next((mark for word, mark in _MODELS if word in (model_id or "").lower()), None)
    level = _EFFORTS.get((effort or "").lower())
    return tuple(mark for mark in (model, level) if mark is not None)
