"""The data server node's skills, asked for as a chat account: listing, fetching and sync.

Each call reaches the server with the token exchanged for whoever asks; the sync asks as
the account that added the skill, so the server's own visibility rules decide.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import httpx
from daimon.core.agent_editing import AgentActor, AgentEditDeps
from daimon.core.authz import Subject
from daimon.core.channel_backend import load_backend_agent, make_backend_service
from daimon.core.config import Settings, SignedIdentityServer, TurnSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.mcp_rpc import McpStatusError
from daimon.core.mcp_skills.install import ServerSkill, fetch_server_skill
from daimon.core.mcp_skills.manifest import SkillEntry, SkillRejected, parse_entry
from daimon.core.mcp_skills.protocol import Endpoint, fetch_skill_listing
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.node_exchange import (
    NodeAccessError,
    NodeAsker,
    NodeExchange,
    asker_of_account,
    exchange_client,
    forget_node_token,
    node_http,
    node_token,
)
from daimon.core.server_skills.anthropic import ManagedAgentsTarget
from daimon.core.server_skills.consent import ConsentKeeper, Renewer
from daimon.core.server_skills.sync import SyncDeps, UpstreamGone, UpstreamUnreachable
from daimon.core.skill_library_files import custom_skill_names, skill_lock
from daimon.core.skills.ingest import SkillIngestError
from daimon.core.stores import server_skills as store
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.domain import BackendAgent, ServerSkillRow
from daimon.core.stores.web_people import get_person_for_account
from daimon.core.token_exchange import ExchangeClient
from pydantic import BaseModel, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.turn.deps import TurnDeps

__all__ = [
    "NodeUpstream",
    "ServerSkillOption",
    "SkillsNode",
    "active_adder",
    "ask_node",
    "consent_keeper",
    "list_server_skills",
    "skill_options",
    "skill_sync_deps",
    "skills_node_of",
]

_GONE = frozenset({403, 404})

type NodeCall[T] = Callable[[httpx.AsyncClient, Endpoint], Awaitable[T]]


@dataclass(frozen=True)
class SkillsNode:
    """The data server node whose server's skills the library mirrors."""

    label: str
    server: SignedIdentityServer
    client: ExchangeClient
    kind: str = "data_server"


def skills_node_of(settings: Settings) -> SkillsNode | None:
    """The configured data server node; None when it is missing or incomplete."""
    config = None if settings.web is None else settings.web.nodes.data_server
    if (
        config is None
        or config.identity_server is None
        or config.client_id is None
        or config.client_secret is None
        or settings.mcp_identity.signing_key is None
    ):
        return None
    server = next(
        (s for s in settings.mcp_identity.servers if s.name == config.identity_server), None
    )
    if server is None:
        return None
    client = exchange_client(
        server,
        client_id=config.client_id,
        client_secret=config.client_secret.get_secret_value(),
        scope=config.scope,
    )
    return SkillsNode(label=config.label, server=server, client=client)


async def ask_node[T](
    exchange: NodeExchange,
    shared: httpx.AsyncClient | None,
    node: SkillsNode,
    asker: NodeAsker,
    call: NodeCall[T],
) -> T:
    """`call` against the node's server as `asker`; a token it stopped taking is dropped.

    Raises `NodeAccessError` without a token, else whatever `call` raises.
    """
    async with node_http(shared, node.server) as http:
        token = await node_token(
            exchange, asker, http, kind=node.kind, server=node.server, client=node.client
        )
        try:
            return await call(http, Endpoint(url=node.server.url, bearer=token))
        except McpStatusError as err:
            if err.status_code == 401:
                await forget_node_token(exchange.sessionmaker, asker.person_id, node.server)
            raise


async def active_adder(
    sessionmaker: async_sessionmaker[AsyncSession], mirror: ServerSkillRow
) -> NodeAsker | None:
    """Who added the mirror, while they could still use it: not suspended, not left."""
    account_id = mirror.added_by_account_id
    if account_id is None:
        return None
    async with sessionmaker() as db:
        asker = await asker_of_account(db, tenant_id=mirror.tenant_id, account_id=account_id)
        if asker is None:
            return None
        person = await get_person_for_account(db, tenant_id=mirror.tenant_id, account_id=account_id)
        if person is not None and person.status == "suspended":
            return None
        standings = await members_store.list_chat_standings(
            db, people={(mirror.tenant_id, account_id)}
        )
    standing = standings.get((mirror.tenant_id, account_id))
    return None if standing is not None and not standing.admitted else asker


@dataclass(frozen=True)
class NodeUpstream:
    """The node's server, asked as the account that added a mirror."""

    exchange: NodeExchange
    node: SkillsNode
    shared: httpx.AsyncClient | None = None

    async def _asked[T](self, mirror: ServerSkillRow, call: NodeCall[T]) -> T:
        asker = await active_adder(self.exchange.sessionmaker, mirror)
        if asker is None:
            raise UpstreamGone("the person who added it is gone")
        try:
            return await ask_node(self.exchange, self.shared, self.node, asker, call)
        except (UpstreamGone, UpstreamUnreachable):
            raise
        except NodeAccessError as err:
            if err.problem in ("unreachable", "unconfigured"):
                raise UpstreamUnreachable(err.problem) from err
            raise UpstreamGone(err.problem) from err
        except McpStatusError as err:
            if err.status_code in _GONE:
                raise UpstreamGone(f"status {err.status_code}") from err
            raise UpstreamUnreachable(f"status {err.status_code}") from err
        except (httpx.HTTPError, ValueError) as err:
            raise UpstreamUnreachable(type(err).__name__) from err

    async def entry(self, mirror: ServerSkillRow) -> SkillEntry | None:
        async def listing(http: httpx.AsyncClient, endpoint: Endpoint) -> Sequence[JsonValue]:
            return await fetch_skill_listing(
                http,
                endpoint,
                keep=lambda raw: isinstance(raw, dict) and raw.get("uri") == mirror.uri,
            )

        found = await self._asked(mirror, listing)
        if not found:
            return None
        try:
            return parse_entry(found[0])
        except SkillRejected as err:
            raise UpstreamUnreachable(f"the server's entry failed a check: {err}") from err

    async def fetch(self, mirror: ServerSkillRow) -> ServerSkill:
        async def fetch(http: httpx.AsyncClient, endpoint: Endpoint) -> ServerSkill:
            try:
                return await fetch_server_skill(
                    http, endpoint, server=self.node.label, skill=mirror.uri
                )
            except SkillIngestError as err:
                raise UpstreamUnreachable(str(err)) from err

        return await self._asked(mirror, fetch)


def consent_keeper(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    edit: AgentEditDeps,
    turn_settings: TurnSettings,
    turn_deps: Callable[[], TurnDeps | None],
) -> ConsentKeeper:
    """Renewals vouched for by each mirror's adder; consented agents are held without one."""

    async def renewer(mirror: ServerSkillRow) -> Renewer | None:
        adder = await active_adder(sessionmaker, mirror)
        # Without turns there is no source capture, so nothing can be renewed.
        deps = None if adder is None else turn_deps()
        if adder is None or deps is None:
            return None
        account = adder.account_id
        # Never an admin: the sync reads each source with the adder's plainest rights.
        subject = Subject(platform_user_id=str(account))

        async def capture(source: BackendAgent) -> BackendAgent:
            from daimon.core.turn.admission import capture_agent_source

            return await capture_agent_source(deps, source, account_id=account, subject=subject)

        service = make_backend_service(sessionmaker, turn_settings, capture_source=capture)
        return Renewer(backends=service, account_id=account)

    async def load(mirror: ServerSkillRow, agent_id: str) -> BackendAgent:
        scope = resource_scope(tenant_id=str(mirror.tenant_id))
        actual = await retrieve_agent(edit.client, agent_id, scope=scope)
        name = (actual.metadata or {}).get(MA_METADATA_KEY_NAME, "")
        return await load_backend_agent(
            edit.client,
            sessionmaker,
            tenant_id=mirror.tenant_id,
            agent_name=name,
            actual_agent=actual,
        )

    return ConsentKeeper(sessionmaker=sessionmaker, renewer=renewer, load=load)


def skill_sync_deps(
    *,
    node: SkillsNode,
    exchange: NodeExchange,
    shared: httpx.AsyncClient | None,
    edit: AgentEditDeps,
    turn_settings: TurnSettings,
    turn_deps: Callable[[], TurnDeps | None],
    now: Callable[[], datetime],
) -> SyncDeps:
    """The sync's dependencies: the node's server upstream, Managed Agents downstream."""
    upstream = NodeUpstream(exchange, node, shared)
    keeper = consent_keeper(
        sessionmaker=exchange.sessionmaker,
        edit=edit,
        turn_settings=turn_settings,
        turn_deps=turn_deps,
    )

    def lock(mirror: ServerSkillRow) -> AbstractAsyncContextManager[None]:
        return skill_lock(edit, tenant_id=mirror.tenant_id, skill_id=mirror.skill_id)

    return SyncDeps(
        server=node.server.name,
        sessionmaker=exchange.sessionmaker,
        upstream=lambda _mirror: upstream,
        targets=(ManagedAgentsTarget(edit.client, consents=keeper),),
        lock=lock,
        now=now,
    )


class ServerSkillOption(BaseModel):
    uri: str
    name: str
    description: str
    skill_id: str | None
    """The library skill mirroring it, once added."""
    taken: bool
    """Another library skill holds its name, so it can't be added."""


def skill_options(
    raw: Sequence[JsonValue], mirrored: dict[str, str], names: frozenset[str]
) -> list[ServerSkillOption]:
    """The server's listing as options, by name; entries that fail a check are left out."""
    out: list[ServerSkillOption] = []
    for item in raw:
        try:
            entry = parse_entry(item)
        except SkillRejected:
            continue
        if entry is None:
            continue
        description = entry.frontmatter.get("description")
        out.append(
            ServerSkillOption(
                uri=entry.uri,
                name=entry.name,
                description=description if isinstance(description, str) else "",
                skill_id=mirrored.get(entry.uri),
                taken=entry.uri not in mirrored and entry.name in names,
            )
        )
    return sorted(out, key=lambda o: o.name)


async def list_server_skills(
    exchange: NodeExchange,
    shared: httpx.AsyncClient | None,
    node: SkillsNode,
    asker: NodeAsker,
    *,
    edit: AgentEditDeps,
    actor: AgentActor,
) -> list[ServerSkillOption]:
    """What the server offers `asker`, marked by what the tenant's library holds.

    Raises `NodeAccessError`, or the server's own failure, like `ask_node`.
    """

    async def listing(http: httpx.AsyncClient, endpoint: Endpoint) -> Sequence[JsonValue]:
        return await fetch_skill_listing(http, endpoint, keep=lambda _raw: True)

    raw = await ask_node(exchange, shared, node, asker, listing)
    async with exchange.sessionmaker() as session:
        mirrors = await store.list_mirrors(session, tenant_id=actor.tenant_id)
    mirrored = {m.uri: m.skill_id for m in mirrors if m.server == node.server.name}
    names = await custom_skill_names(edit, actor=actor)
    return skill_options(raw, mirrored, names)
