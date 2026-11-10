"""The model each provider runs for small side calls (titles, participation verdicts).

Anthropic stays on the model these calls have always used. An unpriced entry is
refused at import: a side call must never run unmetered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from daimon.core.pricing import MODEL_PRICING
from mux.contracts.ids import Provider

AUXILIARY_MODELS: Final[Mapping[Provider, str]] = {
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-5-mini",
    "gemini": "gemini-2.5-flash",
}

for _model in AUXILIARY_MODELS.values():
    if _model not in MODEL_PRICING:
        raise RuntimeError(f"auxiliary model {_model!r} has no entry in MODEL_PRICING")
