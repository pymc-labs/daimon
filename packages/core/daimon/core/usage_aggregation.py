"""Revision-aware neutral measurements; provider meters are never rewritten."""

from __future__ import annotations

from collections.abc import Iterable

from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation
from mux.state.usage_ledger import UsageRevisionConflict


def replace_observation(
    values: tuple[UsageObservation, ...], observation: UsageObservation
) -> tuple[UsageObservation, ...]:
    """Retain the latest snapshot per provider/session/observation identity."""
    for index, prior in enumerate(values):
        if (prior.session, prior.id) != (observation.session, observation.id):
            continue
        if frozenset(prior.covers) != frozenset(observation.covers):
            raise ValueError("usage corrections must retain their coverage set")
        if observation.revision < prior.revision:
            return values
        if observation.revision == prior.revision:
            if observation != prior:
                # Provider refresh timestamps may change on a replay. Counts,
                # attribution and the untouched meter may not change in place.
                fields = {"observed_at", "native_revision"}
                if observation.model_dump(exclude=fields) != prior.model_dump(exclude=fields):
                    raise UsageRevisionConflict(observation.id, observation.revision)
            return values
        if (
            (prior.model is not None and prior.model != observation.model)
            or prior.grain != observation.grain
            or prior.basis != observation.basis
            or prior.turn_id != observation.turn_id
            or prior.thread_id != observation.thread_id
        ):
            raise ValueError("usage corrections must retain their accounting identity")
        return values[:index] + (observation,) + values[index + 1 :]
    return (*values, observation)


def disjoint_observations(values: Iterable[UsageObservation]) -> tuple[UsageObservation, ...]:
    """Coverage is explicit; mixed grains without coverage fail closed."""
    values = tuple(values)
    edges = {(value.session, value.id): value.covers for value in values}
    active: set[tuple[ResourceRef, str]] = set()
    descendants: dict[tuple[ResourceRef, str], set[tuple[ResourceRef, str]]] = {}

    def visit(key: tuple[ResourceRef, str]) -> set[tuple[ResourceRef, str]]:
        if key in active:
            raise ValueError("usage coverage is cyclic")
        if key in descendants:
            return descendants[key]
        active.add(key)
        covered_keys = {key}
        for identity in edges.get(key, ()):
            covered_keys.update(visit((key[0], identity)))
        active.remove(key)
        descendants[key] = covered_keys
        return covered_keys

    for key in edges:
        visit(key)
    covered = {(value.session, identity) for value in values for identity in value.covers}
    selected = tuple(value for value in values if (value.session, value.id) not in covered)
    if values and not selected:
        raise ValueError("usage coverage is cyclic")
    for value in values:
        if value.id in value.covers:
            raise ValueError("usage cannot cover itself")
    grains: dict[object, str] = {}
    selected_coverage: set[tuple[ResourceRef, str]] = set()
    for value in selected:
        coverage = descendants[(value.session, value.id)]
        if selected_coverage.intersection(coverage):
            raise ValueError("overlapping sibling usage coverage")
        selected_coverage.update(coverage)
        if grains.setdefault(value.session, value.grain) != value.grain:
            raise ValueError(
                "overlapping usage grains require explicit coverage or one billing grain"
            )
    return selected


def reported_total(values: Iterable[UsageObservation], field: str) -> int | None:
    """Unknown in any selected snapshot leaves the aggregate unknown."""
    counts = [getattr(value, field) for value in values]
    return None if any(value is None for value in counts) else sum(counts)
