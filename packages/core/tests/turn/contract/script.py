"""One provider-neutral script of what the model service does during a turn.

Every backend fake in this package reads the same `Script` and plays it on its own
wire: Managed Agents SSE, the OpenAI sessions SSE and REST pages, Gemini
interaction reads, or the self-hosted harness journal. The shared cases in
`test_contract.py` then hold every backend to the same outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

End = Literal["terminal", "eof", "read_error", "protocol_error", "hold"]
"""How one stream connection ends.

`terminal`: the service delivers everything left, including the turn's end.
`eof`: the connection closes cleanly, the turn still running.
`read_error` and `protocol_error`: the connection drops mid-turn
(`httpx.ReadError`, `httpx.RemoteProtocolError`).
`hold`: the connection stays open and says nothing.
"""

Stamps = Literal["exact", "coarse_read", "same_second", "behind", "ahead"]
"""The provider's timestamps, relative to the host clock.

`exact`: every stamp is the host time, with microseconds.
`coarse_read`: the create reply has microseconds, every later read the same
second without them.
`same_second`: every record carries one whole-second stamp.
`behind` and `ahead`: the server stamping reads runs 2 s behind or ahead of
the one that stamped the create.
"""

SKEW = timedelta(seconds=2)


@dataclass(frozen=True)
class Conn:
    """One stream connection: deliver `deliver` new records, then end as `end`."""

    deliver: int = 0
    end: End = "terminal"


@dataclass
class Script:
    pieces: int = 3
    """Answer records the turn produces before it ends."""
    connections: list[Conn] = field(default_factory=lambda: [Conn(end="terminal")])
    """One entry per stream open. Opens past the list deliver the rest and end."""
    stamps: Stamps = "exact"
    status_failures: int = 0
    """Status reads that fail with a transient error before one succeeds."""
    replay_failures: int = 0
    """History reads (the reconnect replay) that fail before one succeeds."""
    stuck: bool = False
    """The service says running forever and never ends the turn."""
    deadline_s: float = 10.0
    """The turn's ceiling. Stuck cases shorten it."""
    after_restart: bool = False
    """The session already holds an earlier, finished turn whose answer was
    `EARLIER`, and the host IO is new (a restarted host has no in-memory cursor)."""

    def answer(self) -> str:
        return "".join(piece(i) for i in range(self.pieces))

    def conn(self, index: int) -> Conn:
        if self.stuck:
            return self.connections[index] if index < len(self.connections) else Conn(end="eof")
        return self.connections[index] if index < len(self.connections) else Conn(end="terminal")


EARLIER = "earlier answer "
"""The text of the turn before a restart; it must never reach this turn's reply."""


def piece(index: int) -> str:
    return f"w{index:02d} "


class Clock:
    """Provider stamps for one script, anchored at the create."""

    def __init__(self, stamps: Stamps) -> None:
        self.stamps = stamps
        self.created = datetime.now(UTC)
        if self.created.microsecond == 0:
            self.created = self.created.replace(microsecond=1)

    def create(self) -> datetime:
        if self.stamps == "same_second":
            return self.created.replace(microsecond=0)
        return self.created

    def read(self) -> datetime:
        if self.stamps in ("coarse_read", "same_second"):
            return self.created.replace(microsecond=0)
        if self.stamps == "behind":
            # Fixed against the create, not the wall clock, so a slow run never
            # catches up: every read is stamped 2 s before the create.
            return self.created - SKEW
        if self.stamps == "ahead":
            return self.created + SKEW
        return datetime.now(UTC)
