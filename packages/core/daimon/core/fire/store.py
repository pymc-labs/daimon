"""Where a fire records what happened: the one storage seam the fire core sees."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol

from daimon.core.turn.termination import TerminationReason
from sqlalchemy.ext.asyncio import AsyncSession

#: How a fire ended. `unreadable` (a feature read failed) gives the slot back.
FireStatus = Literal["ok", "failed", "skipped", "unreadable"]


class FireStore(Protocol):
    """The writes the fire guard makes, each fenced on what fired as the store knows it."""

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
