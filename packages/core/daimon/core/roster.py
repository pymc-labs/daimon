"""Who the workspace's agents are, and which one answers where the caller is.

The setup panels on both platforms open on the same question — "who answers
here, and what else is there?" — so the answer is assembled once, here, and
the adapters only render it. Ordering is part of the answer: the agent that
answers where the caller is standing comes first, because it is the one the
caller is asking about.

`order_roster` and `paginate` are pure; `load_roster` is the shell that pays
for one MA listing and one config resolution and folds them together.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    account_id_from_metadata,
)
from daimon.core.scope import ConfigTier, DeploymentDefault, ScopeContext
from daimon.core.stores import scoped_config_read
from daimon.core.stores.domain import Platform
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class RosterAgent(BaseModel):
    """One agent as the setup panel shows it.

    `answering_tier` is the tier that put this agent in front of the caller —
    thread binding, channel, workspace, or deployment — and None for every
    agent that does not answer where the caller is. The tier is kept rather
    than flattened to a boolean because "answers here" and "answers here
    because the workspace says so" lead to different next steps.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    ma_agent_id: str
    model_id: str
    is_built_in: bool
    created_by_account_id: uuid.UUID | None = None
    answering_tier: ConfigTier | None = None


class Roster(BaseModel):
    """The tenant's agents, answering-here first, with that one called out."""

    model_config = ConfigDict(frozen=True)

    rows: tuple[RosterAgent, ...] = ()
    answering: RosterAgent | None = None


class Page[T](BaseModel):
    """One window onto a longer list, with everything a pager needs to render.

    `page` is zero-based and always within range: `paginate` clamps rather
    than raising, so a stale page number from a panel that has since lost rows
    shows the last page instead of an error.
    """

    model_config = ConfigDict(frozen=True)

    items: tuple[T, ...]
    page: int
    page_count: int
    total: int
    has_previous: bool
    has_next: bool


def paginate[T](items: Sequence[T], *, page: int, page_size: int) -> Page[T]:
    """Return the `page`-th window of `items`, clamping `page` into range.

    An empty list is one empty page (`page=0`, `page_count=1`), so a caller
    never has to special-case "no pages at all".
    """
    if page_size < 1:
        raise ValueError("page_size must be at least 1")
    total = len(items)
    page_count = max(1, -(-total // page_size))
    current = min(max(page, 0), page_count - 1)
    start = current * page_size
    return Page[T](
        items=tuple(items[start : start + page_size]),
        page=current,
        page_count=page_count,
        total=total,
        has_previous=current > 0,
        has_next=current < page_count - 1,
    )


def order_roster(
    entries: Sequence[RosterAgent], *, answering_here: str | None
) -> tuple[RosterAgent, ...]:
    """Answering-here first, then case-insensitive name, then MA id.

    The MA id is the last key so the order is total: two agents can share a
    casefolded name across a rename race, and a panel that reorders itself
    between renders is worse than one that picks arbitrarily but stably.
    """
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if answering_here is not None and entry.name == answering_here else 1,
                entry.name.casefold(),
                entry.ma_agent_id,
            ),
        )
    )


async def load_roster(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: Platform,
    channel_id: str | None,
    thread_id: str | None,
    default: DeploymentDefault,
) -> Roster:
    """Build the tenant's roster, marking whichever agent answers for the caller.

    One MA listing plus, when the caller's channel is known, one config
    resolution. `platform` and `thread_id` are passed through so a setup
    thread reports its own responder rather than the parent channel's.
    """
    agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    answering_name: str | None = None
    answering_tier: ConfigTier | None = None
    if channel_id is not None:
        config = await scoped_config_read.resolve(
            session,
            context=ScopeContext(
                tenant_id=tenant_id,
                channel_id=channel_id,
                platform=platform,
                thread_id=thread_id,
            ),
            default=default,
        )
        answering_name = config.agent_name
        answering_tier = config.agent_name_tier
    entries = [
        RosterAgent(
            name=agent.name,
            ma_agent_id=agent.id,
            model_id=agent.model.id,
            is_built_in=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
            created_by_account_id=account_id_from_metadata(
                agent.metadata.get(MA_METADATA_KEY_ACCOUNT)
            ),
            answering_tier=answering_tier if agent.name == answering_name else None,
        )
        for agent in agents
    ]
    rows = order_roster(entries, answering_here=answering_name)
    return Roster(
        rows=rows,
        answering=next((row for row in rows if row.answering_tier is not None), None),
    )
