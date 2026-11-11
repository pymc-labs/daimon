"""The model each provider runs for small side calls (titles, participation verdicts).

Anthropic stays on the model these calls have always used. An unpriced entry is
refused at import: a side call must never run unmetered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from daimon.core.pricing import side_call_rates
from mux.contracts.ids import Provider

AUXILIARY_MODELS: Final[Mapping[Provider, str]] = {
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-6-luna",
    "gemini": "gemini-3.5-flash-lite",
}

for _model in AUXILIARY_MODELS.values():
    try:
        side_call_rates(_model)
    except KeyError:
        raise RuntimeError(f"auxiliary model {_model!r} has no price") from None
