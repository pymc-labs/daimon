"""The per-turn reach budget: how many threads, places and people a turn reaches.

Pure rules; the MCP server keeps the per-turn tally and asks for approval.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReachVerdict = Literal["ok", "needs_approval"]

# An outside client's budget renews each window, so it limits bursts, not a lifetime.
CLIENT_WINDOW = timedelta(hours=2)
# People one turn tags without approval; the Slack automatic reply is held to the same count.
DEFAULT_MAX_PEOPLE = 5
CHEAPER_SHAPE = "Offer one thread with a checklist instead, each owner tagged once."
DECLINED = f"Not posted: the person did not approve. {CHEAPER_SHAPE}"
NO_CARD = (
    "Not posted: this reaches more threads, places or people than Daimon posts without "
    f"approval, and approval cannot be asked here. {CHEAPER_SHAPE}"
)
NO_PLAN = "Not posted: add plan with the totals for the whole request (threads, places, people)."
REACH_OFF = "Not posted: the team keeps Daimon to the conversation it is in. Tell the requester."
TOO_LONG = "Lead with the ask and a three-line summary; attach or link the rest."
SHORT_CAPTION = "Say who it is for, the one takeaway, and how long it runs."


class ReachPlan(BaseModel):
    """Totals for a whole request: new threads, outside places, people tagged."""

    model_config = ConfigDict(frozen=True)

    threads: int = Field(default=0, ge=0, le=1000)
    places: int = Field(default=0, ge=0, le=1000)
    people: int = Field(default=0, ge=0, le=10_000)

    @classmethod
    def from_args(cls, args: Mapping[str, object]) -> ReachPlan:
        """The plan in a tool call's raw arguments, validated once for the card and the store.

        A missing or malformed count raises, so a card never shows less than is approved.
        """
        return cls.model_validate({key: args.get(key) for key in ("threads", "places", "people")})

    def covers(self, other: ReachPlan) -> bool:
        return (
            other.threads <= self.threads
            and other.places <= self.places
            and other.people <= self.people
        )

    def widest(self, other: ReachPlan) -> ReachPlan:
        return ReachPlan(
            threads=max(self.threads, other.threads),
            places=max(self.places, other.places),
            people=max(self.people, other.people),
        )


class ReachTally(BaseModel):
    """What a turn has reached so far, or what one call would add."""

    model_config = ConfigDict(frozen=True)

    threads: int = 0
    # Untagged new threads in the own channel: capped on their own, not part of ``counts``.
    own_threads: int = 0
    places: frozenset[str] = frozenset()
    people: frozenset[str] = frozenset()

    def plus(self, other: ReachTally) -> ReachTally:
        return ReachTally(
            threads=self.threads + other.threads,
            own_threads=self.own_threads + other.own_threads,
            places=self.places | other.places,
            people=self.people | other.people,
        )

    def counts(self) -> ReachPlan:
        return ReachPlan(threads=self.threads, places=len(self.places), people=len(self.people))


def check_reach(
    *,
    plan: ReachPlan,
    so_far: ReachTally,
    call: ReachTally,
    limits: ReachPlan,
    approved: ReachPlan | None,
) -> ReachVerdict:
    """Decide before anything posts: max(declared plan, actual + this call) against the ceiling.

    A plan smaller than actual just counts as actual, never a refusal of its own;
    past the limits or an approved plan it must ask.
    """
    actual = so_far.plus(call).counts()
    ceiling = approved.widest(limits) if approved is not None else limits
    return "ok" if ceiling.covers(plan.widest(actual)) else "needs_approval"


def needs_approval(plan: ReachPlan, limits: ReachPlan) -> str:
    return (
        f"Not posted: {describe(plan)} is more than Daimon posts without approval "
        f"({describe(limits)}). Call approve_reach with this plan and where it goes, "
        f"then post. Or post less: {CHEAPER_SHAPE}"
    )


def needs_own_approval(total: int, limit: int) -> str:
    return (
        f"Not posted: {total} new threads in this channel is more than Daimon opens "
        f"without approval ({limit}). Call approve_reach with threads={total} and the "
        f"channel, then post. Or post less: {CHEAPER_SHAPE}"
    )


def describe(plan: ReachPlan) -> str:
    return f"{plan.threads} threads, {plan.places} places, {plan.people} people"


def card_title(plan: ReachPlan, where: str) -> str:
    """The one question above the buttons: "Open 3 threads in #x and tag 4 people?" """
    where = where.strip()
    parts: list[str] = []
    if plan.threads:
        parts.append(f"open {_count(plan.threads, 'thread')}" + (f" in {where}" if where else ""))
    elif plan.places or where:
        place = where or _count(plan.places, "place")
        parts.append(f"post in {place}")
    if plan.people:
        parts.append(f"tag {_count(plan.people, 'person', 'people')}")
    phrase = " and ".join(parts) or "post"
    return f"{phrase[0].upper()}{phrase[1:]}?"


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def turn_tally_key(origin_id: object) -> str:
    """The tally of the turn running under ``origin_id``."""
    return f"turn:{origin_id}"


def client_tally_key(client: str, now: datetime) -> str:
    """The tally an outside client spends in the fixed window holding ``now``."""
    bucket = int(now.timestamp() // CLIENT_WINDOW.total_seconds())
    return f"client:{client}:{bucket}"
