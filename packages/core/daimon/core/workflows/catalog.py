"""What the editor offers: agents, tools, people and channels of the workspace.

The web route builds one `Options` per request (it reaches the agent platform). Every
validation reads the caller's agents as a `taint.Catalog` through `tool_catalog`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.defaults.ma_index import list_agents_by_tenant, list_skills_strict
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, strip_tenant_prefix
from daimon.core.stores import web_people
from daimon.core.stores import workflow_reads as reads
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.workflows.lanes import agent_of
from daimon.core.workflows.spec import AgentNode, ForEachNode, Spec, slug
from daimon.core.workflows.taint import ANY_TOOL, Catalog, SkillPin, catalog_of
from daimon.core.workflows.views import Person
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: The agents a caller may build with; every validation reads them through `tool_catalog`.
AgentLister = Callable[[], Awaitable[Sequence[BetaManagedAgentsAgent]]]
#: The workspace library's skills by name, at their latest versions.
SkillLister = Callable[[], Awaitable[Mapping[str, SkillPin]]]


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
    """The `server/tool` ids of the agent's enabled MCP tools. A toolset on by default
    enables tools it does not list, as run time reads it (`tool_client._enabled`), and
    Managed Agents lists only the configured ones, so it adds `server/*`."""
    ids: list[str] = []
    for t in agent.tools:
        if t.type != "mcp_toolset":
            continue
        ids += [f"{t.mcp_server_name}/{c.name}" for c in t.configs if c.enabled is not False]
        if t.default_config.enabled is not False:
            ids.append(f"{t.mcp_server_name}/{ANY_TOOL}")
    return ids


async def tool_catalog(
    policy: ToolSafetyPolicy, agents: AgentLister | None, skills: SkillLister | None = None
) -> Catalog:
    """The catalog every validation reads; no lister means no agents or skills (fails closed)."""
    listed: Sequence[BetaManagedAgentsAgent] = await agents() if agents is not None else ()
    pins: Mapping[str, SkillPin] = await skills() if skills is not None else {}
    held = {agent_key(a): frozenset(s.skill_id for s in a.skills) for a in listed}
    return catalog_of(policy, {agent_key(a): agent_tool_ids(a) for a in listed}, pins, held)


def tenant_skills(client: AsyncAnthropic | None, tenant_id: uuid.UUID) -> SkillLister | None:
    """The workspace's library skills (not an agent's own uploads), newest of a name."""
    if client is None:
        return None

    async def listed() -> Mapping[str, SkillPin]:
        newest: dict[str, tuple[str, SkillPin]] = {}
        for row in await list_skills_strict(client):
            title = (row.display_title or "") if row.source == "custom" else ""
            name = strip_tenant_prefix(tenant_id=tenant_id, display_title=title)
            if not name or "/" in name or not row.latest_version:
                continue
            if name not in newest or newest[name][0] < row.created_at:
                newest[name] = (row.created_at, SkillPin(id=row.id, version=row.latest_version))
        return {name: pin for name, (_, pin) in newest.items()}

    return listed


def tenant_agents(
    client: AsyncAnthropic | None,
    tenant_id: uuid.UUID,
    records: async_sessionmaker[AsyncSession] | None = None,
) -> AgentLister | None:
    """The workspace's agents, for a path with no narrower list (chat, MCP, CLI); `records`
    adds the Daimon-owned ones."""
    if client is None:
        return None

    async def listed() -> Sequence[BetaManagedAgentsAgent]:
        return await list_agents_by_tenant(client, tenant_id=tenant_id, records=records)

    return listed


def spec_skills(spec: Spec, catalog: Catalog) -> dict[str, dict[str, str]]:
    """The library skills `spec`'s steps load, pinned as a published version keeps them."""
    nodes = [n.do if isinstance(n, ForEachNode) else n for n in spec.nodes.values()]
    names = {s for n in nodes if isinstance(n, AgentNode) for s in n.skills or []}
    pins = catalog.skill_pins
    return {name: pins[name].model_dump() for name in sorted(names) if name in pins}


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
