"""The credential presses of an eval item's script, through the same single-use consume and
write the chat adapters run; nothing is posted, and the follow-up is the runner's."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.core.agent_pins import FormPinRefused
from daimon.core.credential_submit import apply_env_submit, prepare_env_submit
from daimon.core.stores import credential_requests
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _never() -> bool:
    return False


@dataclass(frozen=True)
class CredentialPresses:
    """`Presses` over the credential requests of the eval tenant; a card's id is the token."""

    sessionmaker: async_sessionmaker[AsyncSession]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def submit_credential(self, token: str, value: str) -> bool:
        """Save `value` as the request's key; whether it was saved."""
        async with self.sessionmaker() as session:
            row = await credential_requests.peek_credential_request(session, token=token)
        if row is None:
            return False
        plan = await prepare_env_submit(self.sessionmaker, row=row, replacement_refused=_never)
        try:
            result = await apply_env_submit(
                self.sessionmaker, row=row, agent=None, platform=None, value=value, plan=plan,
                now=self.clock(),
            )  # fmt: skip
        except FormPinRefused:
            return False
        return result.consumed is not None and result.state == "applied"

    async def skip_credential(self, token: str) -> bool:
        """Spend the request unanswered, as Skip does; whether it was still open."""
        async with self.sessionmaker.begin() as session:
            row = await credential_requests.consume_credential_request(
                session, token=token, now=self.clock()
            )
            if row is not None:
                await credential_requests.set_credential_request_outcome(
                    session, token=token, outcome="declined"
                )
        return row is not None
