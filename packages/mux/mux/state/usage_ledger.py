"""Usage revisions and the accounting outbox.

The ledger keeps, per (binding, observation), the latest revision applied
and the token counts accounted so far. A higher revision produces one outbox
row of signed deltas against them; an equal or lower revision is stale and
produces nothing. Null is not zero: a count the provider did not report
leaves the accounted value alone, and its delta is `None`.
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from mux.contracts._base import Contract, FrozenMap
from mux.contracts.usage import UsageObservation
from mux.errors import MuxError

TOKEN_FIELDS: Final = (
    "input_tokens",
    "input_cached_tokens",
    "input_cache_write_tokens",
    "output_tokens",
    "output_reasoning_tokens",
)


class UsageRevisionConflict(MuxError):
    """The same observation revision arrived again with different counts."""

    def __init__(self, observation_id: str, revision: int) -> None:
        super().__init__(f"revision {revision} of observation {observation_id} changed")
        self.observation_id = observation_id
        self.revision = revision


class AppliedUsage(Contract):
    """What has been accounted for one observation, as of `revision`, and
    the observation that revision came from."""

    binding_id: str
    observation_id: str
    revision: int = Field(ge=1)
    tokens: FrozenMap[str, int | None]
    observation: UsageObservation


class OutboxRow(Contract):
    """One revision's adjustment, for the host to apply exactly once.

    Unique on (binding_id, observation_id, revision), which also fixes
    `prior_applied_revision`: the revision the deltas are measured against,
    the one the ledger held before this row was written, whether or not
    that one's row has been applied by the host yet (None for the first).
    Rows of one observation sum to its latest counts in any apply order.
    """

    binding_id: str
    observation_id: str
    revision: int = Field(ge=1)
    prior_applied_revision: int | None = None
    deltas: FrozenMap[str, int | None]
    observation: UsageObservation
    applied: bool = False

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.binding_id, self.observation_id, self.revision)

    @property
    def is_noop(self) -> bool:
        return not any(self.deltas.values())


def apply_observation(
    prior: AppliedUsage | None, binding_id: str, observation: UsageObservation
) -> tuple[AppliedUsage, OutboxRow] | None:
    """The new accounted state and its outbox row, or None if stale.

    A replay of the current revision is ignored when its counts match and
    raises `UsageRevisionConflict` when they do not: a correction must come
    as a higher revision.
    """
    if prior is not None and observation.revision == prior.revision:
        if any(getattr(observation, f) != getattr(prior.observation, f) for f in TOKEN_FIELDS):
            raise UsageRevisionConflict(observation.id, observation.revision)
        return None
    if prior is not None and observation.revision < prior.revision:
        return None
    tokens: dict[str, int | None] = {}
    deltas: dict[str, int | None] = {}
    for field in TOKEN_FIELDS:
        before = prior.tokens.get(field) if prior else None
        reported: int | None = getattr(observation, field)
        if reported is None:
            tokens[field] = before
            deltas[field] = None
        else:
            tokens[field] = reported
            deltas[field] = reported - (before or 0)
    applied = AppliedUsage(
        binding_id=binding_id,
        observation_id=observation.id,
        revision=observation.revision,
        tokens=tokens,
        observation=observation,
    )
    row = OutboxRow(
        binding_id=binding_id,
        observation_id=observation.id,
        revision=observation.revision,
        prior_applied_revision=prior.revision if prior else None,
        deltas=deltas,
        observation=observation,
    )
    return applied, row
