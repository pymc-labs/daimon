"""Parse and match an explicit agent name without changing ordinary messages."""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Sequence

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.scope import ResolvedConfig
from daimon.core.stores.thread_agent_bindings import create_named_binding_if_absent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def normalized_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def name_after_mention(text: str, mention: str | None = None) -> str | None:
    """Read the first `name:` token after the bot mention (or stripped Teams text)."""
    if mention is not None:
        match = re.search(re.escape(mention), text)
        if match is None:
            return None
        text = text[match.end() :]
    match = re.match(r"\s*([^\s:]+):(?=\s|$)", text)
    return match.group(1) if match else None


def matching_agent(
    agents: Sequence[BetaManagedAgentsAgent], name: str
) -> BetaManagedAgentsAgent | None:
    matches = [agent for agent in agents if normalized_name(agent.name) == normalized_name(name)]
    # A collision after Unicode normalization is ambiguous, so do not guess.
    return matches[0] if len(matches) == 1 else None


async def bind_named_thread(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    config: ResolvedConfig,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    responder_ma_agent_id: str,
    responder_name: str,
    creator_account_id: uuid.UUID,
) -> bool:
    """Keep the first selected responder for later turns through the handoff binding path."""
    if config.agent_name_tier not in ("named", "authored") or config.thread_binding_id is not None:
        return False
    async with sessionmaker.begin() as session:
        return await create_named_binding_if_absent(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            responder_ma_agent_id=responder_ma_agent_id,
            responder_name=responder_name,
            creator_account_id=creator_account_id,
        )
