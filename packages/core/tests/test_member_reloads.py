"""Changes noted while a member list reload runs, and laid over its read."""

from __future__ import annotations

import uuid

from daimon.core.member_reloads import ReloadChanges, overlay

_TENANT = uuid.UUID(int=1)


def test_changes_are_kept_only_while_a_reload_runs() -> None:
    changes = ReloadChanges()
    changes.note(_TENANT, "before", None)
    changes.start(_TENANT)
    changes.note(_TENANT, "u1", None)
    changes.note(_TENANT, "u2", True)
    changes.note(_TENANT, "u2", False)

    assert changes.finish(_TENANT) == {"u1": None, "u2": False}, "latest per user, none before"
    changes.note(_TENANT, "after", None)
    assert changes.seen(_TENANT) == {}, "nothing is kept once no reload runs"


def test_two_overlapping_reloads_both_see_the_changes() -> None:
    changes = ReloadChanges()
    changes.start(_TENANT)
    changes.start(_TENANT)
    changes.note(_TENANT, "u1", None)

    assert changes.finish(_TENANT) == {"u1": None}
    changes.note(_TENANT, "u2", True)
    assert changes.finish(_TENANT) == {"u1": None, "u2": True}, "the second still tracked"


def test_overlay_drops_leavers_and_sets_flags() -> None:
    merged = overlay({"a": True, "b": False}, {"a": False, "b": None, "c": True})
    assert merged == {"a": False, "c": True}, "events win over the older read"
