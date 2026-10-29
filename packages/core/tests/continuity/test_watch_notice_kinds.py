"""Each claimed notice kind is worded once, so a receipt's kind names the text that was sent."""

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.core.continuity.watch_imports import UNMAPPED_NOTICE
from daimon.core.continuity.watch_notices import notice_text
from daimon.core.stores.domain import WatchRow
from daimon.core.stores.watches import notice_kind

CASES: list[tuple[str, dict[str, object], str, str]] = [
    ("expired", {"reason": "source_unmapped"}, "expired", " expired."),
    ("paused", {"reason": "source_unmapped"}, "source_unmapped", UNMAPPED_NOTICE),
    ("paused", {"reason": "Watch paused: source moved"}, "paused", ": Watch paused: source moved"),
    ("paused", {"reason": "tool_unlisted"}, "tool_unlisted", "can no longer check that tool"),
    ("paused", {"reason": "source_changed"}, "source_changed", "changed on its server"),
    ("paused", {"reason": "access_revoked"}, "access_revoked", "owner access was revoked"),
    ("paused", {"reason": "failed"}, "failed", "could not check its source for a day"),
    (
        "active",
        {"notice_state": "daily_cap", "daily_cap_kind": "owner"},
        "owner_daily_cap",
        "your other watches reached your daily limit",
    ),
    ("active", {"notice_state": "daily_cap"}, "daily_cap", "reached its daily limit"),
    ("paused", {"reason": "cap_reached"}, "cap_reached", "reached its firing limit: fired 3"),
    ("active", {"notice_state": "cap_reached"}, "cap_reached", "reached its firing limit"),
]


@pytest.mark.parametrize("status,state,kind,wording", CASES)
def test_notice_kind_picks_the_wording_sent(
    status: str, state: dict[str, object], kind: str, wording: str
) -> None:
    watch_id = uuid.uuid4()
    watch = cast(
        WatchRow,
        SimpleNamespace(
            id=watch_id, instructions="Report  changes", source_state=state, fire_count=3
        ),
    )
    assert notice_kind(status, state) == kind
    text = notice_text(watch, kind)
    assert wording in text
    # Every owner notice names its watch, so a resumed send matches only its own DM.
    assert text.startswith(f"Watch {watch_id} (Report changes)")


def test_every_kind_has_its_own_wording() -> None:
    watch = cast(
        WatchRow,
        SimpleNamespace(id=uuid.uuid4(), instructions="x", source_state={}, fire_count=0),
    )
    kinds = {kind for _, _, kind, _ in CASES}
    assert len({notice_text(watch, kind) for kind in kinds}) == len(kinds)
