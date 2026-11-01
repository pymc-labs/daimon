"""An agent as the web app shows it, and what each viewer may read of it.

`build_agent_view` and `redact_for` are pure; `load_agent_view` and
`load_agent_summaries` read Managed Agents and the database and call them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, get_args, get_type_hints

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_anthropic_skill_params import (
    BetaManagedAgentsAnthropicSkillParams,
)
from cryptography.fernet import MultiFernet
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_editing import (
    BUILTIN_NAMES,
    builtin_enabled,
    ma_tool_to_param,
)
from daimon.core.agent_editing import system_agent_rejection as _system_agent_rejection
from daimon.core.agent_guidance import without_credential_guidance
from daimon.core.agent_mcp_credentials import resolve_agent_mcp_credentials
from daimon.core.agent_pins import agent_pin_names
from daimon.core.agent_reach import build_agent_reach
from daimon.core.credit_views import billed
from daimon.core.defaults.mcp_merge import DAIMON_MCP_SERVER_NAME
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_SETUP_HIDDEN,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.defaults.skills import resolve_custom_skill_titles
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import internal_token_label, mint_agent_mcp_token, token_jti
from daimon.core.mcp_personal_servers import hidden_mcp_server_names
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.mcp_tool_listing import ListedTool, McpToolLister, McpToolListingError
from daimon.core.model_presets import model_choice_id
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.permissions import WEB_PLACE_ID, channel_rule
from daimon.core.platform_names import UNNAMED_CHANNEL, ChannelLookup, lookup_channel_names
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow
from daimon.core.stores import (
    agent_backends,
    agent_owners,
    agent_tool_rules,
    identity,
    platform_names,
    turn_outcomes,
    usage_events,
)
from daimon.core.stores import agent_mcp_credentials as credentials_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.agent_avatars import AvatarLink, get_agent_avatars, normalize_agent_name
from daimon.core.stores.agent_tool_rules import AgentToolRule
from daimon.core.stores.agent_tool_rules import Rule as RuleChoice
from daimon.core.stores.direct_messages import DmOrigin, list_dm_origins
from daimon.core.stores.domain import AgentBackendRevision
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.tenants import get_tenant
from daimon.core.tool_safety import ToolAnnotations, ToolSafetyPolicy, classify_tool
from daimon.core.turn.mcp_context import ContextServer, context_servers
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.errors import ScopeViolation
from mux.contracts.ids import Provider
from mux.profiles import backend_selection
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

#: The spend an agent's card and the agent list show: this long up to now.
SPEND_WINDOW = timedelta(days=30)

Backend = Provider
Effort = Literal["low", "medium", "high", "xhigh", "max"]
PlaceKind = Literal["channel", "dm", "web"]

_DEFAULT_EFFORT: Effort = "high"
_EFFORTS: dict[str, Effort] = {e: e for e in ("low", "medium", "high", "xhigh", "max")}
RunsIn = Literal["discord", "slack", "teams", "dm", "web"]
_RUNS_IN: tuple[RunsIn, ...] = ("discord", "slack", "teams", "dm", "web")


def _omit_none(value: object) -> bool:
    return value is None


class _Doc(BaseModel):
    model_config = ConfigDict(frozen=True)


class ModelChoice(_Doc):
    id: str
    backend: Backend
    speed: Literal["standard", "fast"]
    effort: Effort


class BuiltinTool(_Doc):
    name: str
    enabled: bool


class McpTool(_Doc):
    name: str
    description: str
    effect: Literal["read", "write"]
    rule: RuleChoice


class McpServer(_Doc):
    key: str
    name: str
    url: str
    auth: Literal["oauth", "token", "none"]
    credential: Literal["connected", "missing", "expired"] | None
    tools: list[McpTool] = Field(default_factory=list[McpTool])
    # False when the server's own list could not be read for this viewer; None when not asked.
    listed: bool | None = Field(default=None, exclude_if=_omit_none)


def _skill_types() -> frozenset[str]:
    """The skill types the SDK accepts: derived from it, so the provider's names stay there."""
    (anthropic_type,) = get_args(get_type_hints(BetaManagedAgentsAnthropicSkillParams)["type"])
    return frozenset({"custom", anthropic_type})


class SkillDoc(_Doc):
    type: str
    skill_id: str
    title: str
    description: str = ""
    version: str

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in _skill_types():
            raise ValueError(f"unknown skill type {value!r}")
        return value


class PlaceDoc(_Doc):
    id: str
    kind: PlaceKind
    platform: str
    name: str
    default: bool | None = Field(default=None, exclude_if=_omit_none)
    confidential: bool | None = Field(default=None, exclude_if=_omit_none)


class AgentConfigDoc(_Doc):
    """`AgentConfig` in the web contract: what a draft edits and a version stores."""

    name: str
    purpose: str
    avatar: str
    model: ModelChoice
    system: str | None
    skills: list[SkillDoc]
    tools: list[BuiltinTool]
    mcp_servers: list[McpServer]
    places: list[PlaceDoc] | None


class PersonRef(_Doc):
    id: str
    name: str


class AgentView(AgentConfigDoc):
    id: str
    version: int
    owner: PersonRef
    co_owners: list[PersonRef]
    updated_at: datetime
    updated_by: PersonRef
    last_used_at: datetime | None
    spend_usd: float
    can_edit: bool
    can_view_details: bool


class AgentSummary(_Doc):
    id: str
    name: str
    purpose: str
    avatar: str
    version: int
    owner: PersonRef
    co_owners: list[PersonRef]
    updated_at: datetime
    updated_by: PersonRef
    last_used_at: datetime | None
    spend_usd: float
    can_edit: bool
    draft_changes: int
    model_label: str
    backend: Backend
    runs_in: list[RunsIn]


@dataclass(frozen=True)
class AgentViewer:
    """Who is looking: their account, whether they are an admin, and whether they edit."""

    account_id: uuid.UUID
    is_admin: bool
    may_edit: bool


@dataclass(frozen=True)
class AgentFacts:
    """Everything one agent's view is built from, already read."""

    agent: BetaManagedAgentsAgent
    skill_titles: Mapping[str, str] = field(default_factory=dict[str, str])
    backend: AgentBackendRevision | None = None
    tool_rules: Sequence[AgentToolRule] = ()
    credential_urls: frozenset[str] = frozenset()
    places: Sequence[PlaceDoc] = ()
    owner_account_id: uuid.UUID | None = None
    # The account the agent's metadata says made it, when it resolves to a person. Names
    # the owner where none is recorded; it grants no access.
    creator_account_id: uuid.UUID | None = None
    co_owner_account_ids: Sequence[uuid.UUID] = ()
    people: Mapping[uuid.UUID, PersonRef] = field(default_factory=dict[uuid.UUID, PersonRef])
    updated_by_account_id: uuid.UUID | None = None
    last_used_at: datetime | None = None
    spend_usd: float = 0.0
    avatar_url: str = ""
    # What the skills' metadata says, by title; a skill not listed has no description.
    skill_descriptions: Mapping[str, str] = field(default_factory=dict[str, str])
    # Each server's tools as the server lists them, by server name; absent where
    # no listing was read, and the recorded rules stand in.
    mcp_tools: Mapping[str, Sequence[ListedTool]] = field(
        default_factory=dict[str, Sequence[ListedTool]]
    )
    # Whether servers were asked: then one missing from `mcp_tools` could not be listed.
    tools_read: bool = False
    # Servers people sign in to one by one, by name: whether this viewer has.
    sign_ins: Mapping[str, bool] = field(default_factory=dict[str, bool])


_NOBODY = PersonRef(id="", name="Daimon")
_CHAT_PLATFORMS = ("discord", "slack", "teams")


def _person(facts: AgentFacts, account_id: uuid.UUID | None) -> PersonRef:
    if account_id is None:
        return _NOBODY
    return facts.people.get(account_id, PersonRef(id=str(account_id), name="A member"))


def _model(agent: BetaManagedAgentsAgent, backend: AgentBackendRevision | None) -> ModelChoice:
    if backend is not None and backend.backend is not None and backend.backend != DEFAULT_BACKEND:
        return ModelChoice(
            # No model stored means the provider's default one.
            id=backend.model or backend_selection(backend.backend).model or "",
            backend=backend.backend,
            speed="standard",
            effort=_DEFAULT_EFFORT,
        )
    choice = model_choice_id(agent.model)
    effort = (agent.model.model_extra or {}).get("effort")
    return ModelChoice(
        id=agent.model.id,
        backend=DEFAULT_BACKEND,
        speed="fast" if choice.endswith("-fast") or agent.model.speed == "fast" else "standard",
        effort=_EFFORTS.get(effort, _DEFAULT_EFFORT)
        if isinstance(effort, str)
        else _DEFAULT_EFFORT,
    )


def _tool_rule(rules: Mapping[str, RuleChoice], tool: str) -> RuleChoice:
    return rules.get(tool, "allow")


def _servers(facts: AgentFacts) -> list[McpServer]:
    policy = ToolSafetyPolicy()
    servers: list[McpServer] = []
    for server in facts.agent.mcp_servers:
        rules: dict[str, RuleChoice] = {
            rule.tool_key.partition("/")[2]: rule.rule
            for rule in facts.tool_rules
            if rule.channel_id is None
            and rule.rule is not None
            and rule.tool_key.partition("/")[0] == server.name
        }
        listed = facts.mcp_tools.get(server.name)
        # The server's own list is the truth; without one, the rules recorded for it.
        tools = (
            [
                McpTool(
                    name=tool.name,
                    description=tool.description,
                    effect=classify_tool(
                        policy,
                        server_name=server.name,
                        tool_name=tool.name,
                        annotations=ToolAnnotations(
                            read_only_hint=tool.read_only_hint,
                            destructive_hint=tool.destructive_hint,
                        ),
                    ),
                    rule=_tool_rule(rules, tool.name),
                )
                for tool in listed
            ]
            if listed is not None
            else [
                McpTool(
                    name=name,
                    description="",
                    effect=classify_tool(policy, server_name=server.name, tool_name=name),
                    rule=rule,
                )
                for name, rule in rules.items()
            ]
        )
        connected = server.url in facts.credential_urls
        signed_in = facts.sign_ins.get(server.name)
        servers.append(
            McpServer(
                key=server.name,
                name=server.name,
                url=server.url,
                auth="token" if connected else "none",
                credential="connected"
                if connected or signed_in
                else "missing"
                if signed_in is False
                else None,
                tools=tools,
                listed=listed is not None if facts.tools_read else None,
            )
        )
    return servers


def config_of(facts: AgentFacts) -> AgentConfigDoc:
    """The agent's whole config, unredacted. Pure."""
    agent = facts.agent
    tools = [ma_tool_to_param(tool) for tool in agent.tools]
    return AgentConfigDoc(
        name=agent.name,
        purpose=agent.description or "",
        avatar=facts.avatar_url,
        model=_model(agent, facts.backend),
        # Daimon adds its credential guidance on every write; people edit their own text.
        system=without_credential_guidance(agent.system or ""),
        skills=[
            SkillDoc(
                type=skill.type,
                skill_id=skill.skill_id,
                title=facts.skill_titles.get(skill.skill_id, skill.skill_id),
                description=facts.skill_descriptions.get(
                    facts.skill_titles.get(skill.skill_id, skill.skill_id), ""
                ),
                version=skill.version,
            )
            for skill in agent.skills
        ],
        tools=[
            BuiltinTool(name=name, enabled=bool(builtin_enabled(tools, name)))
            for name in BUILTIN_NAMES
        ],
        mcp_servers=_servers(facts),
        places=list(facts.places),
    )


def sees_details(facts: AgentFacts, viewer: AgentViewer) -> bool:
    """Prompt, credentials and reach detail: admins, and owners and co-owners
    unless the agent is a member's copy whose inherited setup stays hidden."""
    if viewer.is_admin:
        return True
    owns = (
        viewer.account_id == facts.owner_account_id
        or viewer.account_id in facts.co_owner_account_ids
    )
    return owns and facts.agent.metadata.get(MA_METADATA_KEY_SETUP_HIDDEN) != "true"


def build_agent_view(facts: AgentFacts, viewer: AgentViewer) -> AgentView:
    """The agent as ``viewer`` may see it. Pure."""
    config = config_of(facts)
    details = sees_details(facts, viewer)
    # A seeded agent is changed by copying it, never in place; its prompt stays withheld.
    seeded = _system_agent_rejection(facts.agent) is not None
    if seeded:
        config = config.model_copy(update={"system": None})
    shown_owner = facts.owner_account_id or facts.creator_account_id
    owner = _person(facts, shown_owner)
    view = AgentView(
        **config.model_dump(),
        id=facts.agent.id,
        version=facts.agent.version,
        owner=owner,
        co_owners=[_person(facts, account) for account in facts.co_owner_account_ids],
        updated_at=facts.agent.updated_at,
        updated_by=_person(facts, facts.updated_by_account_id or shown_owner),
        last_used_at=facts.last_used_at,
        spend_usd=facts.spend_usd,
        can_edit=details and viewer.may_edit and not seeded,
        can_view_details=details,
    )
    return redact_for(view, details=details, is_admin=viewer.is_admin)


def redact_config[Config: AgentConfigDoc](
    config: Config, *, details: bool, is_admin: bool
) -> Config:
    """Strip what a viewer may not read. Pure.

    Without details: no prompt, no places, and no MCP servers but Daimon's own,
    whose presence is a capability (as `get_agent` shows a member), without URL,
    credential state or tool rules.
    With details but no admin: confidential places stay hidden.
    """
    if not details:
        return config.model_copy(
            update={
                "system": None,
                "places": None,
                "mcp_servers": [
                    server.model_copy(
                        update={"url": "", "credential": None, "tools": [], "listed": False}
                    )
                    for server in config.mcp_servers
                    if server.key == DAIMON_MCP_SERVER_NAME
                ],
            }
        )
    if is_admin or config.places is None:
        return config
    return config.model_copy(
        update={"places": [place for place in config.places if not place.confidential]}
    )


def redact_for(view: AgentView, *, details: bool, is_admin: bool) -> AgentView:
    return redact_config(view, details=details, is_admin=is_admin)


def summary_of(view: AgentView, *, draft_changes: int, runs_in: Sequence[str]) -> AgentSummary:
    """The roster row for a view. Pure."""
    return AgentSummary(
        id=view.id,
        name=view.name,
        purpose=view.purpose,
        avatar=view.avatar,
        version=view.version,
        owner=view.owner,
        co_owners=view.co_owners,
        updated_at=view.updated_at,
        updated_by=view.updated_by,
        last_used_at=view.last_used_at,
        spend_usd=view.spend_usd,
        can_edit=view.can_edit,
        draft_changes=draft_changes,
        model_label=view.model.id,
        backend=view.model.backend,
        runs_in=[kind for kind in _RUNS_IN if kind in runs_in],
    )


def _runs_in(policy: TenantAccessPolicy, names: tuple[str, ...]) -> frozenset[str] | None:
    """Where the agent's rules let it run: every rule must allow a place; None is anywhere."""
    allowed: frozenset[str] | None = None
    for name in names:
        rule = policy.agent_rules.get(name)
        if rule is not None and rule.runs_in is not None:
            here = frozenset(rule.runs_in)
            allowed = here if allowed is None else allowed & here
    return allowed


def places_of(
    *,
    names: tuple[str, ...],
    ma_agent_id: str,
    policy: TenantAccessPolicy,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    dm_origins: Sequence[DmOrigin],
    platform: str,
    channel_names: Mapping[str, str],
) -> list[PlaceDoc]:
    """Where the agent answers: the channels it is the default of, the ones its rule lets it
    run in, and the web chat unless its rule keeps it off. Pure.

    A channel kept to its own agents is confidential. A workspace-wide default and
    direct messages are not places a rule names, so they are not listed.
    """
    reach = build_agent_reach(
        names,
        tenant=tenant,
        channels=channels,
        default=default,
        ma_agent_id=ma_agent_id,
        dm_origins=dm_origins,
    )
    defaults = {place.channel_id for place in reach.places if place.channel_id is not None}
    allowed = _runs_in(policy, names)
    ids = sorted((defaults | (allowed or frozenset())) - {WEB_PLACE_ID})
    places = [
        PlaceDoc(
            id=channel_id,
            kind="channel",
            platform=platform,
            name=channel_names.get(channel_id, UNNAMED_CHANNEL),
            default=True if channel_id in defaults else None,
            confidential=True if channel_rule(policy, channel_id).readers == "own" else None,
        )
        for channel_id in ids
    ]
    if allowed is None or WEB_PLACE_ID in allowed:
        places.append(PlaceDoc(id=WEB_PLACE_ID, kind="web", platform="web", name="Web chat"))
    return places


def runs_in_of(places: Sequence[PlaceDoc]) -> list[str]:
    """The platforms the places span, for the roster. Pure."""
    return list(
        dict.fromkeys(place.platform if place.kind == "channel" else place.kind for place in places)
    )


# ---------- Shell ----------


@dataclass(frozen=True)
class OwnMcpServer:
    """The deployment's own MCP server: its URL, and how to mint a viewer a token for it."""

    url: str
    secret: bytes
    now: Callable[[], datetime]


@dataclass(frozen=True)
class AgentViewDeps:
    client: AsyncAnthropic
    session_factory: async_sessionmaker[AsyncSession]
    # The workspace's markup: spend is shown as billed, like the credit pages.
    markup: Decimal
    # Where agent faces are served (`/avatars/...`); None leaves avatars empty.
    avatar_base_url: str | None = None
    deployment_default: DeploymentDefault = DeploymentDefault()
    # SKILL.md descriptions of the skills the deployment ships, by name.
    skill_descriptions: Mapping[str, str] = field(default_factory=dict[str, str])
    # Where a server's tool list comes from; without both, the recorded rules stand in.
    mcp_tools: McpToolLister | None = None
    fernet: MultiFernet | None = None
    # The deployment's own MCP server, listed with a short token minted for the viewer.
    own_server: OwnMcpServer | None = None
    # Names a channel the store has not seen, where the deployment can ask its platform.
    channel_lookup: ChannelLookup | None = None


def is_tenant_agent(agent: BetaManagedAgentsAgent, tenant_id: uuid.UUID) -> bool:
    return agent.archived_at is None and agent.metadata.get(MA_METADATA_KEY_TENANT) == str(
        tenant_id
    )


async def _people(
    session: AsyncSession, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, PersonRef]:
    """The people behind `account_ids`, keyed by account: the web person, else the name
    their chat platform last gave. An account nothing names is left out."""
    people: dict[uuid.UUID, PersonRef] = {}
    for person in await people_store.list_people(session, tenant_id=tenant_id):
        if person.account_id in account_ids:
            people[person.account_id] = PersonRef(id=str(person.id), name=person.display_name)
    for platform in _CHAT_PLATFORMS:
        rest = {account for account in account_ids if account not in people}
        if not rest:
            break
        external = await identity.platform_external_ids(
            session, tenant_id=tenant_id, platform=platform, account_ids=rest
        )
        names = await platform_names.get_user_names(
            session, tenant_id=tenant_id, platform=platform, user_ids=set(external.values())
        )
        for account, user_id in external.items():
            known = names.get(user_id)
            if known is not None and known.label:
                people[account] = PersonRef(id=str(account), name=known.label)
    return people


def _creator_of(agent: BetaManagedAgentsAgent) -> uuid.UUID | None:
    """The account the agent's metadata names as its maker, if it parses."""
    try:
        return uuid.UUID(agent.metadata.get(MA_METADATA_KEY_ACCOUNT, ""))
    except ValueError:
        return None


def served_avatar_url(base_url: str | None, avatar: AvatarLink | None) -> str:
    """Where an agent's stored face is served; empty when it has none or nothing serves it."""
    if avatar is None or not base_url:
        return ""
    return f"{base_url.rstrip('/')}/avatars/{avatar.token}/{avatar.sha256[:12]}.png"


def _avatar_name(agent: BetaManagedAgentsAgent) -> str:
    return agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name


async def load_avatar_urls(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agents: Sequence[BetaManagedAgentsAgent],
    base_url: str | None,
) -> dict[str, str]:
    """Each agent's stored face, by agent id; an agent with none is left out."""
    if not base_url or not agents:
        return {}
    found = await get_agent_avatars(
        session, tenant_id=tenant_id, agent_names=[_avatar_name(a) for a in agents]
    )
    return {
        agent.id: served_avatar_url(base_url, link)
        for agent in agents
        if (link := found.get(normalize_agent_name(_avatar_name(agent)))) is not None
    }


@dataclass(frozen=True)
class _TenantPlaces:
    """What every agent's places are built from, read once per call."""

    platform: str
    policy: TenantAccessPolicy
    tenant: TenantConfigRow | None
    channels: Sequence[ChannelConfigRow]
    dm_origins: Sequence[DmOrigin]
    channel_names: Mapping[str, str]
    external_id: str
    unnamed: frozenset[str]
    """Channels the store has no name for, left to the platform lookup after the session closes."""


async def _tenant_places(session: AsyncSession, tenant_id: uuid.UUID) -> _TenantPlaces:
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        raise StoreError(f"no tenant {tenant_id}")
    policy = await load_access_policy(session, tenant_id=tenant_id)
    tenant_row, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    ruled = {c for rule in policy.agent_rules.values() for c in rule.runs_in or ()}
    wanted = {row.channel_id for row in channels} | ruled
    names = await platform_names.get_channel_names(
        session, tenant_id=tenant_id, platform=tenant.platform, channel_ids=wanted
    )
    return _TenantPlaces(
        tenant.platform,
        policy,
        tenant_row,
        channels,
        await list_dm_origins(session, tenant_id=tenant_id),
        names,
        tenant.external_id,
        frozenset(wanted - names.keys()),
    )


async def _named_places(
    places: _TenantPlaces, deps: AgentViewDeps, tenant_id: uuid.UUID
) -> _TenantPlaces:
    """``places`` with the names the platform gave for channels the store had none for.

    Runs with no database session held: the lookup is a network call.
    """
    if deps.channel_lookup is None or places.platform != "discord" or not places.unnamed:
        return places
    found = await lookup_channel_names(
        deps.session_factory,
        tenant_id=tenant_id,
        platform=places.platform,
        external_id=places.external_id,
        channel_ids=places.unnamed,
        lookup=deps.channel_lookup,
    )
    return replace(places, channel_names={**places.channel_names, **found.labels()})


async def _tools_of(
    lister: McpToolLister,
    server: ContextServer,
    *,
    agent_id: str,
    cache_as: tuple[str, str] | None = None,
) -> tuple[ListedTool, ...] | None:
    try:
        return await lister.list_tools(server, cache_as=cache_as)
    except McpToolListingError as err:
        # An unreachable server must not break the agent's page: it shows as not listed.
        cause = err.__cause__
        log.warning(
            "agent_view.tools_unlisted",
            agent_id=agent_id,
            server=server.name,
            error=type(cause).__name__ if cause is not None else type(err).__name__,
        )
        return None


async def _own_server_tools(
    deps: AgentViewDeps,
    own: OwnMcpServer,
    lister: McpToolLister,
    *,
    name: str,
    tenant_id: uuid.UUID,
    agent: BetaManagedAgentsAgent,
    account_id: uuid.UUID,
) -> tuple[ListedTool, ...] | None:
    """The deployment's own server's tools as ``account_id`` sees them on ``agent``."""
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)
    key = (canonical_mcp_url(own.url), f"viewer:{account_id}:{agent_uuid}")
    known = lister.cache.recall(key)
    if known is not None:
        return known
    if lister.failed_recently(key):
        return None  # no token is minted to ask again at once
    jti = uuid.uuid4()
    async with deps.session_factory.begin() as db:
        token = await mint_agent_mcp_token(
            db,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            label=internal_token_label(jti),
            secret=own.secret,
            now=own.now(),
            ttl_days=900 / 86400,  # pyright: ignore[reportArgumentType]
            chat_execution=True,
            surface="provider_turn",
            jti=jti,
        )
    try:
        return await _tools_of(
            lister, ContextServer(name, own.url, token), agent_id=agent.id, cache_as=key
        )
    finally:
        async with deps.session_factory.begin() as db:
            await revoke_mcp_token(db, jti=token_jti(token), now=own.now())


async def _listed_tools(
    deps: AgentViewDeps,
    *,
    tenant_id: uuid.UUID,
    agent: BetaManagedAgentsAgent,
    account_id: uuid.UUID,
) -> dict[str, tuple[ListedTool, ...]]:
    """Each server's own tool list where one can be read: with the agent's shared
    token, or for the deployment's own server with a token minted for the viewer."""
    lister = deps.mcp_tools
    if lister is None or not agent.mcp_servers:
        return {}
    credentials = (
        await resolve_agent_mcp_credentials(
            sessionmaker=deps.session_factory,
            fernet=deps.fernet,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id),
        )
        if deps.fernet is not None
        else ()
    )
    listed: dict[str, tuple[ListedTool, ...]] = {}
    for server in context_servers([(s.name, s.url) for s in agent.mcp_servers], credentials):
        if (tools := await _tools_of(lister, server, agent_id=agent.id)) is not None:
            listed[server.name] = tools
    own = deps.own_server
    for server in agent.mcp_servers:
        # Only the configured URL gets a minted token, whatever the server is named.
        if (
            own is None
            or server.name in listed
            or canonical_mcp_url(server.url) != canonical_mcp_url(own.url)
        ):
            continue
        tools = await _own_server_tools(
            deps,
            own,
            lister,
            name=server.name,
            tenant_id=tenant_id,
            agent=agent,
            account_id=account_id,
        )
        if tools is not None:
            listed[server.name] = tools
    return listed


async def _sign_ins(
    deps: AgentViewDeps, facts: AgentFacts, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> dict[str, bool]:
    """Of the agent's servers people sign in to one by one, whether ``account_id`` has."""
    urls = {server.name: server.url for server in facts.agent.mcp_servers}
    if not urls:
        return {}
    async with deps.session_factory() as session:
        grants = await flows_store.list_completed_grants(
            session, tenant_id=tenant_id, server_urls=urls.values()
        )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=facts.agent.id)
    hidden = hidden_mcp_server_names(
        grants,
        agent_id=agent_uuid,
        account_id=account_id,
        shared_server_urls=facts.credential_urls,
        server_urls=urls,
    )
    mine = {
        canonical_mcp_url(grant.mcp_server_url)
        for grant in grants
        if grant.agent_id == agent_uuid and grant.account_id == account_id
    }
    return {
        name: name not in hidden
        for name, url in urls.items()
        if name in hidden or canonical_mcp_url(url) in mine
    }


async def load_agent_facts(
    deps: AgentViewDeps,
    *,
    tenant_id: uuid.UUID,
    agents: Sequence[BetaManagedAgentsAgent],
    spend_since: datetime | None,
    updated_by: Mapping[str, uuid.UUID] | None = None,
) -> list[AgentFacts]:
    """Read what each agent's view needs: owners, backend, rules, credentials, places, use.

    ``spend_since=None`` skips use and spend, for callers that need only the config.
    """
    titles, _truncated = await resolve_custom_skill_titles(
        deps.client, agents=agents, tenant_id=tenant_id
    )
    facts: list[AgentFacts] = []
    async with deps.session_factory() as session:
        shared = await _tenant_places(session, tenant_id)
    shared = await _named_places(shared, deps, tenant_id)
    async with deps.session_factory() as session:
        # The credit pages' source: billed spend by agent, markup included.
        costs = (
            await usage_events.costs_by_agent_in_tenant_since(
                session, tenant_id=tenant_id, since=spend_since
            )
            if spend_since is not None
            else {}
        )
        # One read per kind of fact for the whole list, not one per agent.
        uuids = {
            agent.id: derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)
            for agent in agents
        }
        owners = await agent_owners.owners_of_many(session, tenant_id, uuids.values())
        co_owners = await agent_owners.co_owners_of_many(session, tenant_id, uuids.values())
        credentials = await credentials_store.list_credentials_many(
            session, tenant_id=tenant_id, agent_ids=uuids.values()
        )
        backends = await agent_backends.current_many(session, tenant_id, uuids.values())
        rules = await agent_tool_rules.list_rules(
            session, tenant_id=tenant_id, agent_ids=[agent.id for agent in agents]
        )
        rules_by_agent: dict[str, list[AgentToolRule]] = {}
        for rule in rules:
            rules_by_agent.setdefault(rule.agent_id, []).append(rule)
        last_used = (
            await turn_outcomes.last_started_at_many(session, tenant_id, list(uuids))
            if spend_since is not None
            else {}
        )
        avatars = await get_agent_avatars(
            session,
            tenant_id=tenant_id,
            agent_names=[_avatar_name(a) for a in agents],
        )
        editors = updated_by or {}
        creators = {agent.id: _creator_of(agent) for agent in agents}
        people = await _people(
            session,
            tenant_id,
            {
                account
                for agent in agents
                for account in (
                    owners.get(uuids[agent.id]),
                    editors.get(agent.id),
                    creators[agent.id],
                    *co_owners.get(uuids[agent.id], ()),
                )
                if account is not None
            },
        )
        for agent in agents:
            agent_uuid = uuids[agent.id]
            owner = owners.get(agent_uuid)
            agent_co_owners = co_owners.get(agent_uuid, [])
            editor = editors.get(agent.id)
            creator = creators[agent.id]
            avatar = avatars.get(normalize_agent_name(_avatar_name(agent)))
            facts.append(
                AgentFacts(
                    agent=agent,
                    skill_titles=titles,
                    skill_descriptions=deps.skill_descriptions,
                    backend=backends.get(agent_uuid),
                    tool_rules=rules_by_agent.get(agent.id, []),
                    credential_urls=frozenset(
                        row.mcp_server_url for row in credentials.get(agent_uuid, ())
                    ),
                    places=places_of(
                        names=tuple(n for n in agent_pin_names(agent.name, agent.metadata) if n),
                        ma_agent_id=agent.id,
                        policy=shared.policy,
                        tenant=shared.tenant,
                        channels=shared.channels,
                        default=deps.deployment_default,
                        dm_origins=shared.dm_origins,
                        platform=shared.platform,
                        channel_names=shared.channel_names,
                    ),
                    owner_account_id=owner,
                    creator_account_id=creator if creator in people else None,
                    co_owner_account_ids=tuple(agent_co_owners),
                    people={
                        account: people[account]
                        for account in (owner, editor, creator, *agent_co_owners)
                        if account is not None and account in people
                    },
                    updated_by_account_id=editor,
                    last_used_at=last_used.get(agent.id),
                    spend_usd=float(billed(costs.get(agent.id), deps.markup)),
                    avatar_url=served_avatar_url(deps.avatar_base_url, avatar),
                )
            )
    return facts


async def fetch_tenant_agent(
    deps: AgentViewDeps, *, tenant_id: uuid.UUID, ma_agent_id: str
) -> BetaManagedAgentsAgent | None:
    """The tenant's live agent ``ma_agent_id``; None when it is missing, archived or foreign."""
    try:
        agent = await retrieve_agent(
            deps.client, ma_agent_id, scope=resource_scope(tenant_id=str(tenant_id))
        )
    except (anthropic.NotFoundError, ScopeViolation):
        return None
    return agent if is_tenant_agent(agent, tenant_id) else None


async def load_agent_view(
    deps: AgentViewDeps,
    *,
    tenant_id: uuid.UUID,
    ma_agent_id: str,
    viewer: AgentViewer,
    spend_since: datetime,
    updated_by: Mapping[str, uuid.UUID] | None = None,
) -> AgentView | None:
    """One agent as ``viewer`` may see it; None when the tenant has no such agent."""
    agent = await fetch_tenant_agent(deps, tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    if agent is None:
        return None
    (facts,) = await load_agent_facts(
        deps,
        tenant_id=tenant_id,
        agents=[agent],
        spend_since=spend_since,
        updated_by=updated_by,
    )
    if sees_details(facts, viewer):
        # Servers are asked for their tools (cached ten minutes) only for who sees them.
        facts = replace(
            facts,
            mcp_tools=await _listed_tools(
                deps, tenant_id=tenant_id, agent=agent, account_id=viewer.account_id
            ),
            tools_read=True,
            sign_ins=await _sign_ins(
                deps, facts, tenant_id=tenant_id, account_id=viewer.account_id
            ),
        )
    return build_agent_view(facts, viewer)
