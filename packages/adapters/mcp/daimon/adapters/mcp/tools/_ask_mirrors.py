"""Hand a finished `ask` to the tenant's mirrors (`daimon.core.mirrors`)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.mirror_delivery import record_conversation_event
from daimon.core.mirror_events import ActorRef, ConversationEventV1, EventSurface, SourceRef
from daimon.core.stores import mirrors as mirrors_store
from daimon.core.stores.platform_names import get_user_names
from daimon.core.turn.outcomes import current_outcome

log = structlog.get_logger(__name__)

#: The answer is already the caller's; recording it for mirrors may delay
#: the reply this long at most.
_RECORD_TIMEOUT_S = 5.0


async def mirror_ask(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    handle: str,
    question: str,
    answer: str,
    surface: EventSurface,
    read_seal: Callable[[], Awaitable[frozenset[str]]],
    now: datetime,
) -> tuple[str, ...]:
    """Queue the turn for every mirror that covers it; the names of those
    mirrors, for the asker. Never raises: a mirror never fails an answered turn."""
    if not runtime.settings.mirrors.enabled or auth.agent_id is None:
        return ()
    observation = current_outcome.get()
    try:
        async with asyncio.timeout(_RECORD_TIMEOUT_S):
            async with runtime.session_factory() as session:
                active = await mirrors_store.list_for_tenant(
                    session, auth.tenant_id, active_only=True
                )
            if not any(mirror.source_agent_id == auth.agent_id for mirror in active):
                return ()
            name = None
            if auth.platform is not None and auth.platform_user_id is not None:
                async with runtime.session_factory() as session:
                    names = await get_user_names(
                        session,
                        tenant_id=auth.tenant_id,
                        platform=auth.platform,
                        user_ids=[auth.platform_user_id],
                    )
                known = names.get(auth.platform_user_id)
                name = known.label if known is not None else None
            event = ConversationEventV1(
                id=observation.id if observation is not None else uuid.uuid4(),
                surface=surface,
                occurred_at=now,
                tenant_id=auth.tenant_id,
                agent_id=auth.agent_id,
                source=SourceRef(
                    channel_id=token_channel_id(auth),
                    seal_ids=tuple(sorted(await read_seal())),
                    session_key=handle,
                ),
                actor=ActorRef(
                    account_id=auth.account_id,
                    platform_user_id=auth.platform_user_id,
                    display_name=name,
                    is_external=auth.is_external,
                ),
                question=question,
                answer=answer,
            )
            mirrors = await record_conversation_event(runtime.session_factory, event, now=now)
    except Exception as err:
        # Named boundary: the caller already has the answer; losing its
        # mirror copy is logged, never raised.
        log.warning("mirror.event_dropped", error_type=type(err).__name__)
        return ()
    return tuple(mirror.name for mirror in mirrors)
