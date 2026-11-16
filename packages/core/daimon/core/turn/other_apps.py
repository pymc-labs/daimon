"""Whether the running turn keeps other chat apps' names (the `other_app_names` feature).

The turn reads the feature once and marks its observation. The card reducers and the
outbound wrapper ask here, so a status line and the answer follow one decision. Without a
running turn (a hand-built render) the names are scrubbed, as they always were.
"""

from __future__ import annotations

from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, Sessions, place_feature
from daimon.core.turn.admission import Admission
from daimon.core.turn.outcomes import current_outcome


def mark_turn(admission: Admission, platform: str) -> bool:
    """Whether this turn keeps other apps' names, and the turn's observation told, so the
    card and status lines follow the same decision as the answer."""
    keep = admission.shown("other_app_names", platform)
    if (observation := current_outcome.get()) is not None:
        observation.other_app_names = keep
    return keep


def scrub_platform(platform: str | None) -> str | None:
    """`platform` for `scrub_other_platforms`, or None (text kept as written) on a turn that
    keeps the names."""
    observation = current_outcome.get()
    return None if observation is not None and observation.other_app_names else platform


async def place_scrub_platform(
    sessions: Sessions, settings: Settings | None, place: FeaturePlace
) -> str | None:
    """`place.platform` for `scrub_other_platforms`, or None when `other_app_names` is on at
    `place`. For a post or tool call outside a turn; a failed read keeps the scrub."""
    kept = (await place_feature(sessions, settings, place, "other_app_names")).on
    return None if kept else place.platform
