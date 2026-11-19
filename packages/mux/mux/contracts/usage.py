"""Usage observations: what a provider says a session consumed.

Null is not zero: a token count the provider did not report is `None`.
Observations are revisioned; a new revision of the same observation is a
correction, applied as a signed delta, never a new charge. Totals from
overlapping grains (a turn total and its model requests) are never summed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue

from mux.contracts._base import Contract, FrozenMap
from mux.contracts.ids import ModelRef, ResourceRef


class UsageObservation(Contract):
    """One measurement.

    `input_tokens` is inclusive of the cached and cache-write counts, and
    `output_reasoning_tokens` is a subset of `output_tokens`. `revision`
    orders corrections to the same `id`: a higher number supersedes a lower
    one, and an observation never overwrites a higher revision already
    applied. `native_revision` keeps the provider's own version string, if
    any, for audit only. `native_meter`
    keeps the provider's own usage record untouched, so a conversion can be
    audited against it. `final` is the provider saying the measured work has
    ended: no later revision will add usage. A running snapshot is never final.
    `web_searches` counts the billable web searches the work ran, cumulative
    like the tokens. `occurred_at` is when the provider says the work last
    ran, if it says; `observed_at` is when the driver read it.
    """

    id: str
    revision: int = Field(ge=1)
    native_revision: str | None = None
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
    native_meter: FrozenMap[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    completeness: Literal["partial", "measured", "unknown"]
    observed_at: datetime
    occurred_at: datetime | None = Field(default=None, exclude_if=lambda value: value is None)
    covers: tuple[str, ...] = ()
    final: bool = Field(default=False, exclude_if=lambda value: not value)
    web_searches: int = Field(default=0, ge=0, exclude_if=lambda value: not value)


__all__ = [
    "UsageObservation",
]
