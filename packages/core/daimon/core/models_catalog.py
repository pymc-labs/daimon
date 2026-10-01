"""The model catalog surfaced to users picking an agent's model.

Single source of truth for the choice list: `constants.ALLOWED_MODEL_IDS`
gives the set and order, `constants.MODEL_DISPLAY_NAMES` gives the label. A
caller (a Discord select menu, a Slack modal) asks for the catalog with the
agent's current default and gets back exactly one entry flagged as default.
"""

from __future__ import annotations

from daimon.core.constants import ALLOWED_MODEL_IDS, MODEL_DISPLAY_NAMES
from pydantic import BaseModel, ConfigDict

_DEFAULT_DESCRIPTION = "the default"


class ModelChoice(BaseModel):
    """One selectable model, as presented to a user."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    description: str | None
    is_default: bool


def list_model_choices(*, default: str) -> tuple[ModelChoice, ...]:
    """Return every selectable model, ordered as `ALLOWED_MODEL_IDS`.

    Exactly the entry matching `default` gets `is_default=True` and the
    `"the default"` description; every other entry has `description=None`.
    Raises `ValueError` if `default` is not an allowed model id.
    """
    if default not in ALLOWED_MODEL_IDS:
        raise ValueError(f"default model {default!r} is not one of {ALLOWED_MODEL_IDS}")
    return tuple(
        ModelChoice(
            id=model_id,
            label=MODEL_DISPLAY_NAMES.get(model_id, model_id),
            description=_DEFAULT_DESCRIPTION if model_id == default else None,
            is_default=model_id == default,
        )
        for model_id in ALLOWED_MODEL_IDS
    )
