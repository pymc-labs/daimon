"""Source capture for surfaces that name an agent but no place.

A provider switch records what the move drops by reading the agent's own source. A
panel, the web editor or a settings tool answers nowhere in particular, so it reads
the agent as its requester: the one rule here, shared by every such surface.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from daimon.core.authz import Subject
from daimon.core.channel_backend import AgentBackendRefused, BackendAgent, CaptureSource

if TYPE_CHECKING:
    from daimon.core.turn.deps import TurnDeps


def for_agent(
    deps: TurnDeps | None,
    *,
    account_id: uuid.UUID,
    subject: Subject,
    external_user_id: str | None = None,
) -> CaptureSource:
    """Capture an agent's own source for `account_id`; refused without turn machinery.

    `deps` None (an app that runs no turns) cannot read a source, so a switch there is
    refused as unavailable instead of being stored without its consent.
    """

    async def capture(source: BackendAgent) -> BackendAgent:
        if deps is None:
            raise AgentBackendRefused("source_unavailable")
        from daimon.core.turn.admission import capture_agent_source

        return await capture_agent_source(
            deps, source, account_id=account_id, subject=subject, external_user_id=external_user_id
        )

    return capture
