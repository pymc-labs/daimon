"""Human principal provenance, independent of the tool confirmation policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daimon.core.turn.admission import AdmissionGrant


HUMAN_MESSAGE_ORIGINS = frozenset({"chat", "handoff", "task_handoff"})


def human_invocation(
    origin: str, attended: bool | None, parent_grant: AdmissionGrant | None = None
) -> bool:
    """Only a person's message/click or their frozen delegation carries a principal.

    Workers disable confirmation cards independently of provenance. Every wake,
    headless send and unknown future origin lacks a human principal, even when
    it retains a recorded owner whose current membership can be looked up.
    """
    if origin == "worker":
        return parent_grant is not None and parent_grant.human_invocation
    return origin in HUMAN_MESSAGE_ORIGINS and attended is not False
