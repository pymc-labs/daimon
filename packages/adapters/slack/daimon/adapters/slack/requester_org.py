"""Which organisation a Slack requester belongs to, from `users.info` and the event.

`users.info` is the authority: only a verified member of the workspace that
hosts the app is `HOST`. The event's own team fields (`user_team` and friends)
are optional and Slack does not promise them on every event, so they can only
ever add `OTHER`: when one names another team, the requester is from another
organisation even if `users.info` disagrees. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from daimon.adapters.slack.admin import SlackRequester
from daimon.adapters.slack.gating import ambiguous_event_team, foreign_event_team
from daimon.core.turn_hold import RequesterOrg

__all__ = ["RequesterStanding", "decide_requester_org"]


@dataclass(frozen=True)
class RequesterStanding:
    """What the platform says about the requester's organisation, and the admin signal it allows."""

    org: RequesterOrg
    #: The requester's home workspace; set for `OTHER`, which admission requires.
    home_id: str | None
    #: True only for a verified host member carrying an admin signal. An external
    #: member is never an admin, whatever their own workspace says.
    is_admin: bool

    @property
    def foreign(self) -> bool:
        """Whether the requester is, or may be, from another organisation."""
        return self.org is not RequesterOrg.HOST


def decide_requester_org(
    requester: SlackRequester, *, event: Mapping[str, Any] | None = None
) -> RequesterStanding:
    """The requester's organisation: `HOST` only on a positive `users.info` verdict.

    An event team field naming another (non-empty) team makes the requester `OTHER`;
    an empty hint is ignored and an unreadable one makes them `UNKNOWN`. Without one,
    a requester `users.info` places in another workspace is `OTHER`, and one it cannot
    place anywhere is `UNKNOWN` (another organisation for this turn only).
    """
    event_team = foreign_event_team(event, team_id=requester.host_team_id) if event else None
    if event_team is not None:
        return RequesterStanding(RequesterOrg.OTHER, event_team, is_admin=False)
    if event and ambiguous_event_team(event):
        # A team hint we cannot read proves nothing either way: no persistent writes.
        return RequesterStanding(RequesterOrg.UNKNOWN, None, is_admin=False)
    if requester.is_host_member:
        return RequesterStanding(RequesterOrg.HOST, None, is_admin=requester.is_admin)
    home = requester.team_id
    if home is not None and home != requester.host_team_id:
        return RequesterStanding(RequesterOrg.OTHER, home, is_admin=False)
    return RequesterStanding(RequesterOrg.UNKNOWN, None, is_admin=False)
