"""What the editor offers: agents, tools, people and channels of the workspace.

The web route builds one `Options` per request (it reaches the agent platform). Every
validation reads the caller's agents as a `taint.Catalog` through `tool_catalog`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.stores import web_people
from daimon.core.stores import workflow_reads as reads
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.workflows.lanes import agent_of
from daimon.core.workflows.spec import AgentNode, ForEachNode, Spec, slug
from daimon.core.workflows.taint import Catalog, catalog_of
from daimon.core.workflows.views import Person
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

#: The agents a caller may build with; every validation reads them through `tool_catalog`.
AgentLister = Callable[[], Awaitable[Sequence[BetaManagedAgentsAgent]]]


def tool_label(tool_id: str) -> str:
    """A `server/tool` id in words, as the editor names it: `Send message`."""
    return tool_id.rsplit("/", 1)[-1].replace("_", " ").capitalize()


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


def agent_key(agent: BetaManagedAgentsAgent) -> str:
    """The key a spec names the agent by."""
    return slug(agent_name(agent))


def agent_name(agent: BetaManagedAgentsAgent) -> str:
    return str(agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name)


def agent_tool_ids(agent: BetaManagedAgentsAgent) -> list[str]:
    """The `server/tool` ids of the agent's enabled MCP tools."""
    return [
        f"{t.mcp_server_name}/{c.name}"
        for t in agent.tools
        if t.type == "mcp_toolset"
        for c in t.configs
        if c.enabled is not False
    ]


async def tool_catalog(policy: ToolSafetyPolicy, agents: AgentLister | None) -> Catalog:
    """The catalog every validation reads; no lister means no agents (fails closed)."""
    listed: Sequence[BetaManagedAgentsAgent] = await agents() if agents is not None else ()
    return catalog_of(policy, {agent_key(a): agent_tool_ids(a) for a in listed})


def tenant_agents(client: AsyncAnthropic | None, tenant_id: uuid.UUID) -> AgentLister | None:
    """The workspace's agents, for a path with no narrower list (chat, MCP, CLI)."""
    if client is None:
        return None

    async def listed() -> Sequence[BetaManagedAgentsAgent]:
        return await list_agents_by_tenant(client, tenant_id=tenant_id)

    return listed


def spec_agent_tools(spec: Spec, catalog: Catalog) -> dict[str, list[str]]:
    """The tools of the agents `spec` uses, as a published version keeps them for its runs."""
    nodes = [n.do if isinstance(n, ForEachNode) else n for n in spec.nodes.values()]
    keys = {agent_of(spec, n) for n in nodes if isinstance(n, AgentNode)}
    return {k: sorted(catalog.agents[k]) for k in keys if k is not None and k in catalog.agents}


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
