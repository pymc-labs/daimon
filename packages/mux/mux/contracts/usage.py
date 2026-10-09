"""Usage observations: what a provider says a session consumed.

Null is not zero: a token count the provider did not report is `None`.
Observations are revisioned; a new revision of the same observation is a
correction, applied as a signed delta, never a new charge. Totals from
overlapping grains (a turn total and its model requests) are never summed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue

from mux.contracts._base import Contract
from mux.contracts.ids import ModelRef, ResourceRef


class UsageObservation(Contract):
    """One measurement.

    `input_tokens` is inclusive of the cached and cache-write counts, and
    `output_reasoning_tokens` is a subset of `output_tokens`. `native_meter`
    keeps the provider's own usage record untouched, so a conversion can be
    audited against it.
    """

    id: str
    revision: str
    session: ResourceRef
    turn_id: str | None = None
    thread_id: str | None = None
    model: ModelRef | None = None
    grain: Literal["model_request", "turn", "session"]
    basis: Literal["increment", "cumulative"]
    input_tokens: int | None = Field(default=None, ge=0)
    input_cached_tokens: int | None = Field(default=None, ge=0)
    input_cache_write_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    output_reasoning_tokens: int | None = Field(default=None, ge=0)
    native_meter: Mapping[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    completeness: Literal["partial", "measured", "unknown"]
    observed_at: datetime
    covers: tuple[str, ...] = ()
