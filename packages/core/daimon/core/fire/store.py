"""Where a fire records what happened: the one storage seam the fire core sees."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from daimon.core.config import Settings
from daimon.core.routine_sandbox import RoutineSandbox
from daimon.core.turn.termination import TerminationReason
from sqlalchemy.ext.asyncio import AsyncSession

#: How a fire ended. `unreadable` (a feature read failed) gives the slot back.
FireStatus = Literal["ok", "failed", "skipped", "unreadable"]
#: What a refusal is fenced on: the whole claim, the destination with the private
#: revision, or the private revision alone.
Fence = Literal["claim", "destination", "private"]


@dataclass(frozen=True)
class FireResult:
    """A fire's result: its reply, the files and hold it is delivered with, and how."""

    tail: str
    payload: str | None = None
    hold: dict[str, Any] | None = None
    delivery: Literal["pending", "skipped"] | None = None
    note: str | None = None
    producing_agent_id: str | None = None


class FireGuardStore(Protocol):
    """What the fire guard (`fire`) records through, fenced on what fired."""

    def execution(self, lifetime_s: float) -> AbstractAsyncContextManager[None]:
        """The execution grant the turn runs under."""
        ...

    async def fail(
        self, session: AsyncSession, *, error: str, reason: TerminationReason | None
    ) -> bool:
        """Record a failed or skipped fire on `session`; returns whether its channel is told."""
        ...

    def tell_owner(self, reason: str | None, *, channel_told: bool) -> None:
        """Tell the owner of a failed fire, once per failing streak."""
        ...


class FireStore(FireGuardStore, Protocol):
    """Every write a fire's turn makes (`fire_turn`), each fenced on what fired."""

    async def refuse(
        self,
        session: AsyncSession,
        reason: str,
        *,
        error: str | None = None,
        fault: bool = False,
        tell: bool = True,
        fence: Fence = "claim",
    ) -> None:
        """Record a fire refused before it ran, as an issue; `tell` False tells nobody."""
        ...

    async def pause_for_lapsed_role(self, session: AsyncSession, *, error: str) -> bool:
        """Pause for an owner who lost a role the agent needs; whether this call paused it."""
        ...

    async def lapsed_role_card(self) -> tuple[str, str]:
        """The card DMed to the owner after a lapsed-role pause."""
        ...

    async def heal_agent_id(self, agent_id: str) -> bool:
        """Record the agent id the fire resolved; False when an edit raced the fire."""
        ...

    async def sandbox(self, settings: Settings, *, platform: str) -> RoutineSandbox | None:
        """The built-in sandbox the fire runs with, if an admin opted it in."""
        ...

    async def finish(self, result: FireResult) -> None:
        """Record the result and queue its delivery, on a fresh session."""
        ...
