"""The model each provider runs for small side calls (titles, participation verdicts).

Anthropic stays on the model these calls have always used. A side call never runs
unmetered: an entry with no price today is logged at import and skipped at call time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import structlog
from daimon.core.pricing import side_call_rates
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import Provider
from mux.profiles import INLINE_REUSE, PERSISTENT_WORKSPACE

log = structlog.get_logger()

AUXILIARY_MODELS: Final[Mapping[Provider, str]] = {
    DEFAULT_BACKEND: "claude-haiku-5-5",
    PERSISTENT_WORKSPACE.provider: "gpt-6-luna",
    INLINE_REUSE.provider: "gemini-3.5-flash-lite",
}


def side_call_priced(provider: Provider) -> bool:
    """True when the provider's side-call model has a price today."""
    try:
        side_call_rates(AUXILIARY_MODELS[provider])
    except KeyError:
        return False
    return True


for _provider, _model in AUXILIARY_MODELS.items():
    if not side_call_priced(_provider):
        log.warning("auxiliary_model.unpriced", provider=_provider, model=_model)
