"""The audience rule for a bot post, pure: no I/O, no feature or store reads.

Kept apart from `daimon.core.audience_gate` so `authz` can use it without a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from daimon.core.turn_hold import TurnHold

if TYPE_CHECKING:
    from daimon.core.channel_audience import ChannelAudience

AUDIENCE_CONTAINED: Final = "audience_contained"
AudienceRefusal = Literal["audience_contained"]


def audience_refusal(
    fact: ChannelAudience, hold: TurnHold | None, channel_id: str | None, parent_id: str | None
) -> AudienceRefusal | None:
    """`audience_contained` when the destination is contained and the execution is not held to it.

    A held answer in its own room, and in the threads under it, passes. `fact.contained`
    already counts an unknown room as contained.
    """
    if not fact.contained:
        return None
    if hold is not None and hold.contains(channel_id, parent_id):
        return None
    return AUDIENCE_CONTAINED


__all__ = ["AUDIENCE_CONTAINED", "AudienceRefusal", "audience_refusal"]
