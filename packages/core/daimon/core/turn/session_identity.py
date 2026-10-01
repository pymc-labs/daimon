"""Check responder compatibility before using an existing session's credentials."""

from __future__ import annotations

from dataclasses import dataclass

from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.thread_sessions import update_agent_identity
from daimon.core.turn.errors import SessionAgentMismatch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["SessionIdentity", "check_session_agent"]


@dataclass(frozen=True)
class SessionIdentity:
    """Outcome of the responder check, plus the session read if one happened.

    `observed` is the `sessions.retrieve` response from the legacy-identity
    branch, handed back so a caller that also needs the session (to backfill
    its configuration snapshot) does not pay a second read. It is None whenever
    no read was needed or the session was gone — never a claim about the
    session's contents.
    """

    session_exists: bool
    observed: BetaManagedAgentsSession | None


async def check_session_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    mapping: ThreadSessionRow,
    responder_ma_agent_id: str,
) -> SessionIdentity:
    """Report `session_exists=False` only for a missing legacy session, left to
    ordinary recovery."""
    source_agent_id = mapping.ma_agent_id
    observed: BetaManagedAgentsSession | None = None
    if source_agent_id is None:
        try:
            observed = await anthropic.beta.sessions.retrieve(mapping.ma_session_id)
        except APIStatusError as error:
            if error.status_code == 404:
                # There is no workspace to inspect. Let the existing event-send
                # recovery path handle the missing session and its user notice.
                return SessionIdentity(session_exists=False, observed=None)
            raise
        source_agent_id = observed.agent.id
        async with sessionmaker() as session, session.begin():
            await update_agent_identity(session, id=mapping.id, ma_agent_id=source_agent_id)
    if source_agent_id != responder_ma_agent_id:
        raise SessionAgentMismatch(
            mapping_id=mapping.id,
            session_id=mapping.ma_session_id,
            source_agent_id=source_agent_id,
            destination_agent_id=responder_ma_agent_id,
        )
    return SessionIdentity(session_exists=True, observed=observed)
