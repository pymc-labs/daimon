"""What the editor offers: agents, tools, people and channels of the workspace.

The web route builds one `Options` per request (it reaches the agent platform); the
validator reads it as a `taint.Catalog`.
"""

from __future__ import annotations

import re
import uuid

from daimon.core.stores import web_people
from daimon.core.stores import workflow_reads as reads
from daimon.core.workflows.taint import Catalog, ToolFacts
from daimon.core.workflows.views import Person
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession


class AgentOption(BaseModel):
    key: str
    id: str
    name: str
    tools: int


class ToolOption(BaseModel):
    id: str
    label: str
    writes: bool


class PersonOption(Person):
    platforms: list[str]


class ChannelOption(BaseModel):
    id: str
    name: str
    platform: str
    shared: bool


class Options(BaseModel):
    agents: list[AgentOption]
    tools: list[ToolOption]
    people: list[PersonOption]
    channels: list[ChannelOption]
    #: Which tools each agent key may call, for the validator.
    agent_tools: dict[str, list[str]] = {}

    def for_validation(self) -> Catalog:
        return Catalog(
            agents={k: frozenset(v) for k, v in self.agent_tools.items()},
            tools={t.id: ToolFacts(writes=t.writes) for t in self.tools},
        )


def slug(name: str) -> str:
    """An agent's key in a spec: its name in lowercase words joined by dashes."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


async def people(session: AsyncSession, tenant_id: uuid.UUID) -> list[PersonOption]:
    """Everyone in the workspace, with the chat platforms they can be reached on."""
    reachable = await reads.platforms_by_account(session, tenant_id=tenant_id)
    rows = await web_people.list_people(session, tenant_id=tenant_id)
    return [
        PersonOption(
            id=str(r.account_id), name=r.display_name, platforms=reachable.get(r.account_id, [])
        )
        for r in rows
    ]
