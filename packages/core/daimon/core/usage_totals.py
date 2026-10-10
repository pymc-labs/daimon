"""Alternate-provider totals retain revisioned observations beside legacy stages."""

from __future__ import annotations

from dataclasses import dataclass

from daimon.core.turn.state import UsageTotals
from daimon.core.usage_aggregation import (
    disjoint_observations,
    replace_observation,
    reported_total,
)
from mux.contracts.usage import UsageObservation


@dataclass(frozen=True, slots=True)
class ProviderUsageTotals(UsageTotals):
    """Legacy stage fields are known lower bounds; observations retain unknowns.

    This subtype is used only for explicit alternate providers, leaving the
    default Anthropic dataclass and its serialized four fields unchanged.
    Price observations, never these compatibility rendering counters.
    """

    observations: tuple[UsageObservation, ...] = ()

    @property
    def selected(self) -> tuple[UsageObservation, ...]:
        return disjoint_observations(self.observations)

    @property
    def reported_input_tokens(self) -> int | None:
        return reported_total(self.selected, "input_tokens")

    @property
    def reported_output_tokens(self) -> int | None:
        return reported_total(self.selected, "output_tokens")

    def add_observation(self, usage: UsageObservation) -> ProviderUsageTotals:
        values = replace_observation(self.observations, usage)
        selected = disjoint_observations(values)
        for value in selected:
            if (
                value.input_tokens is not None
                and value.input_cached_tokens is not None
                and value.input_cached_tokens + (value.input_cache_write_tokens or 0)
                > value.input_tokens
            ):
                raise ValueError("cached input exceeds inclusive input tokens")
        return ProviderUsageTotals(
            input_tokens=sum(
                (
                    value.input_tokens
                    - value.input_cached_tokens
                    - (value.input_cache_write_tokens or 0)
                )
                if value.input_tokens is not None and value.input_cached_tokens is not None
                else 0
                for value in selected
            ),
            output_tokens=sum(value.output_tokens or 0 for value in selected),
            cache_read_input_tokens=sum(value.input_cached_tokens or 0 for value in selected),
            cache_creation_input_tokens=sum(
                value.input_cache_write_tokens or 0 for value in selected
            ),
            observations=values,
        )
