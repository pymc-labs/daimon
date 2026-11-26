"""Who drives an agent-chat session, as the identity relay may name them.

An agent-chat turn has no place, so the relay cannot match it to a live turn as it
does a chat message. It names the person only from what the sending path recorded
here: every path that sends or resumes a turn on an agent-chat session calls
`record_driver` first, and the relay reads `driver_signed_in` on each call.

Only a person signed in to the MCP hub with their platform login counts. The holder
of an agent key, a coding key or a CLI-minted key is not established as one person,
so a turn such a key drives names nobody, even on a session a person started. A
send to a session that is still running another turn names nobody either: the relay
cannot tell which of the two turns is calling. That is decided from Daimon's own count
of the sends (`earlier_turn_open`), not only from the provider's status, which can
still read idle just after a send was accepted. Each send counts itself here before it
is made, and its turn records that number once the send is accepted; a person is named
only when the latest send was recorded and its turn was seen to end. Both are written
only under the session's mutation fence, which every send takes.

A send that fails with an error that may hide its acceptance (a 5xx or a timeout) is
never recorded, so the next send names nobody and logs
`agent_chat.requester_withheld`; the send after that is named again. A session started
before sends were counted has no count, so its first send names nobody the same way.

The sign-in is checked when the send is made, not on each relay call: a hub login
revoked mid-turn still names the person until the turn ends, within the turn ceiling.

The person named is the credential's own account's platform user, read live by the
MCP verifier; nothing from the request or the tool arguments is used.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_CHAT_SESSION,
)
from daimon.core.stores import session_settle_state
from daimon.core.stores.session_settle_state import SessionProvenance
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: The session status in which no other turn can be calling the relay.
IDLE_STATUS = "idle"


@dataclass(frozen=True)
class EarlierTurn:
    """What the sending path read before its send: whether an earlier turn may still be
    running (`open`), and the count of sends begun it read (`sends`), which the write
    checks is unchanged."""

    open: bool
    sends: int | None = None


#: For a path that reads nothing: any earlier turn may still be running.
UNREAD = EarlierTurn(open=True)


def counts_as_signed_in(
    *, signed_in_person: bool, session_status: str | None, earlier_turn_open: bool
) -> bool:
    """Whether a send names its sender: a signed-in person, on a new session (no
    status) or an idle one whose latest recorded send was seen to end."""
    return signed_in_person and session_status in (None, IDLE_STATUS) and not earlier_turn_open


def session_provenance(metadata: Mapping[str, str] | None) -> SessionProvenance:
    """Where an agent-chat session came from, read from the metadata Daimon stamped on it
    at creation (`sessions.create_session`), which a caller cannot set: the grant of its
    Daimon credential, the channel its tools were chosen for and its creator's account."""
    stamp = metadata or {}
    account = stamp.get(MA_METADATA_KEY_ACCOUNT)
    try:
        account_id = None if account is None else uuid.UUID(account)
    except ValueError:
        account_id = None
    return SessionProvenance(
        grant=stamp.get(MA_METADATA_KEY_CHAT_SESSION) or None,
        channel_id=stamp.get(MA_METADATA_KEY_CHANNEL) or None,
        account_id=account_id,
    )


async def record_driver(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    managed_session_id: str,
    signed_in_person: bool,
    session_status: str | None,
    earlier: EarlierTurn,
    provenance: SessionProvenance,
    resuming: bool = False,
    new_session: bool = False,
) -> int | None:
    """Record who drives the turn about to be sent or resumed on `managed_session_id`.

    Written under the session fence before the send, and never swallowed: a send whose
    driver is not recorded could run under the previous driver's name. `earlier` is
    what the path read: whether an earlier send's turn has not been seen to end, and
    the count of sends it read. A send returns its number, which `record_turn` writes
    once the send is accepted. The write names the person only if nothing changed since
    the read, in the same statement: on a session the caller just created, only if it
    creates the row; on another, only if the count of sends is still the one read.
    A resume carries on a turn someone else may have started, so it can only take a
    name away: a signed-in person's resume keeps the record, anyone else's clears it.
    Each write also copies `provenance` (`session_provenance`), whether or not a relay
    server opted in, so the MCP server can find the session by its credential's grant.
    """
    if resuming and signed_in_person:
        return None
    async with sessionmaker.begin() as db:
        return await session_settle_state.record_agent_chat_driver(
            db,
            managed_session_id=managed_session_id,
            signed_in=not resuming
            and counts_as_signed_in(
                signed_in_person=signed_in_person,
                session_status=session_status,
                earlier_turn_open=earlier.open,
            ),
            send=not resuming,
            provenance=provenance,
            new_session=new_session,
            sends_read=earlier.sends,
        )


async def driver_signed_in(
    sessionmaker: async_sessionmaker[AsyncSession], *, managed_session_id: str
) -> bool:
    """Whether the latest recorded driver of the session is a signed-in person."""
    async with sessionmaker() as db:
        return await session_settle_state.is_agent_chat_signed_in(
            db, managed_session_id=managed_session_id
        )
