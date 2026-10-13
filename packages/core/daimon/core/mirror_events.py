"""The conversation event a mirror copies: one finished question and answer.

Built at the end of an agent-chat `ask` and stored only in the outbox row of
each mirror it matched (`mirror_deliveries.payload`), never on its own, so a
tenant without mirrors stores no conversation content. `schema_version` lets a
later shape be added beside this one; readers keep accepting version 1. Pure.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

#: Where the turn ran. Separate from `authz.Surface`, which names where a
#: result lands.
EventSurface = Literal["agent_chat", "hub"]


class SourceRef(BaseModel):
    """Where the conversation lives: the channel an agent key was minted in
    (None for a person's own unbound session), the ids that sealed it (its
    stamp and its channel's seal now), and the
    managed-agent session the answer came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel_id: str | None = None
    seal_ids: tuple[str, ...] = ()
    session_key: str


class ActorRef(BaseModel):
    """Who asked. The platform id is kept for the record, never posted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: uuid.UUID | None = None
    platform_user_id: str | None = None
    display_name: str | None = None
    is_external: bool = False


class ConversationEventV1(BaseModel):
    """One finished turn. `id` is the turn's outcome id, so a repeated emit
    for the same turn stores one delivery per mirror."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    id: uuid.UUID
    kind: Literal["turn.completed"] = "turn.completed"
    surface: EventSurface
    occurred_at: datetime
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    source: SourceRef
    actor: ActorRef
    question: str
    answer: str

    @property
    def person_scoped(self) -> bool:
        """Whether only the asker can read the session: no channel holds it."""
        return self.source.channel_id is None


__all__ = ["ActorRef", "ConversationEventV1", "EventSurface", "SourceRef"]
