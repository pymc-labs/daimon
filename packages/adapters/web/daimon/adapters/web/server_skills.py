"""Skills a connection node's server serves: the agent editor's menu, mirrors and their sync.

Each call reaches the server as a person, with the token the node exchanges for them; the
sync asks as the person who added the skill, so the server's own visibility rules decide.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final

import httpx
import structlog
from daimon.adapters.web.agent_access import Editor, Viewer, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.caller import chat_standings, has_left
from daimon.adapters.web.nodes.data_server import DataServerNode
from daimon.adapters.web.nodes.exchange import (
    NodeAccessError,
    forget_node_token,
    node_http,
    node_token,
)
from daimon.adapters.web.nodes.kinds import build_nodes
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentEditDeps, AgentEditError
from daimon.core.authz import Subject
from daimon.core.channel_backend import load_backend_agent, make_backend_service
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.mcp_rpc import McpStatusError
from daimon.core.mcp_skills.install import ServerSkill, fetch_server_skill
from daimon.core.mcp_skills.manifest import SkillEntry, SkillRejected, parse_entry
from daimon.core.mcp_skills.protocol import Endpoint, fetch_skill_listing
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.server_skills.anthropic import ManagedAgentsTarget
from daimon.core.server_skills.consent import ConsentKeeper, Renewer
from daimon.core.server_skills.follows import (
    SkillFollow,
    agent_follows,
    apply_held_update,
    set_agent_follow,
)
from daimon.core.server_skills.mirror import add_server_skill
from daimon.core.server_skills.sync import (
    CheckResult,
    SyncDeps,
    UpstreamGone,
    UpstreamUnreachable,
    approve_update,
    check_mirror,
    follow_now,
)
from daimon.core.skill_library import MirrorDoc, mirror_doc
from daimon.core.skill_library_files import (
    SkillRef,
    custom_skill_names,
    fork_library_skill,
    skill_lock,
)
from daimon.core.skills.ingest import SkillIngestError
from daimon.core.stores import server_skills as store
from daimon.core.stores.domain import BackendAgent, ServerSkillRow, WebPersonRow
from daimon.core.stores.web_people import get_person
from daimon.core.turn.admission import capture_agent_source
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "active_adder",
    "server_skills_node",
    "server_skills_router",
    "sync_server_skills_forever",
]

log = structlog.get_logger(__name__)

_GONE = frozenset({403, 404})
CHECK_INTERVAL_S: Final = 900
"""Seconds between checks of each added skill for a new version."""

type _Call[T] = Callable[[httpx.AsyncClient, Endpoint], Awaitable[T]]


def server_skills_node(runtime: WebRuntime) -> DataServerNode | None:
    """The node whose server's skills are offered; None without a data server node."""
    if runtime.anthropic is None:
        return None
    return next((n for n in build_nodes(runtime.settings) if isinstance(n, DataServerNode)), None)


async def active_adder(
    sessionmaker: async_sessionmaker[AsyncSession], mirror: ServerSkillRow
) -> WebPersonRow | None:
    """Who added the mirror, while they could still sign in: not suspended, not left."""
    if mirror.added_by_person_id is None:
        return None
    async with sessionmaker() as db:
        person = await get_person(
            db, tenant_id=mirror.tenant_id, person_id=mirror.added_by_person_id
        )
        if person is None or person.status == "suspended":
            return None
        if has_left(await chat_standings(db, [person]), person):
            return None
    return person


@dataclass(frozen=True)
class NodeUpstream:
    """The node's server, asked as the person who added a mirror."""

    runtime: WebRuntime
    node: DataServerNode

    @asynccontextmanager
    async def _endpoint(
        self, mirror: ServerSkillRow
    ) -> AsyncIterator[tuple[httpx.AsyncClient, Endpoint]]:
        person = await active_adder(self.runtime.sessionmaker, mirror)
        if person is None:
            raise UpstreamGone("the person who added it is gone")
        async with node_http(self.runtime, self.node.server) as http:
            try:
                token = await _token(self.runtime, self.node, person, http)
            except NodeAccessError as err:
                if err.problem in ("unreachable", "unconfigured"):
                    raise UpstreamUnreachable(err.problem) from err
                raise UpstreamGone(err.problem) from err
            try:
                yield http, Endpoint(url=self.node.server.url, bearer=token)
            except McpStatusError as err:
                if err.status_code == 401:
                    await forget_node_token(self.runtime, person, self.node.server)
                if err.status_code in _GONE:
                    raise UpstreamGone(f"status {err.status_code}") from err
                raise UpstreamUnreachable(f"status {err.status_code}") from err
            except (httpx.HTTPError, ValueError) as err:
                raise UpstreamUnreachable(type(err).__name__) from err

    async def entry(self, mirror: ServerSkillRow) -> SkillEntry | None:
        async with self._endpoint(mirror) as (http, endpoint):
            found = await fetch_skill_listing(
                http,
                endpoint,
                keep=lambda raw: isinstance(raw, dict) and raw.get("uri") == mirror.uri,
            )
        if not found:
            return None
        try:
            return parse_entry(found[0])
        except SkillRejected as err:
            raise UpstreamUnreachable(f"the server's entry failed a check: {err}") from err

    async def fetch(self, mirror: ServerSkillRow) -> ServerSkill:
        async with self._endpoint(mirror) as (http, endpoint):
            try:
                return await fetch_server_skill(
                    http, endpoint, server=self.node.label, skill=mirror.uri
                )
            except SkillIngestError as err:
                raise UpstreamUnreachable(str(err)) from err


async def _token(
    runtime: WebRuntime, node: DataServerNode, person: WebPersonRow, http: httpx.AsyncClient
) -> str:
    return await node_token(
        runtime, person, http, kind=node.kind, server=node.server, client=node.exchange_client()
    )


def _lock(deps: AgentEditDeps, mirror: ServerSkillRow) -> AbstractAsyncContextManager[None]:
    return skill_lock(deps, tenant_id=mirror.tenant_id, skill_id=mirror.skill_id)


def consent_keeper(runtime: WebRuntime, edit: AgentEditDeps) -> ConsentKeeper:
    """Renewals vouched for by each mirror's adder; consented agents are held without one."""
    turns = runtime.turns

    async def renewer(mirror: ServerSkillRow) -> Renewer | None:
        # Without turns there is no source capture, so nothing can be renewed.
        person = None if turns is None else await active_adder(runtime.sessionmaker, mirror)
        if turns is None or person is None:
            return None
        deps = turns.deps
        account = person.account_id
        # Never an admin: the sync reads each source with the adder's plainest rights.
        subject = Subject(platform_user_id=str(account))

        async def capture(source: BackendAgent) -> BackendAgent:
            return await capture_agent_source(deps, source, account_id=account, subject=subject)

        service = make_backend_service(
            runtime.sessionmaker, runtime.settings.turn, capture_source=capture
        )
        return Renewer(backends=service, account_id=account)

    async def load(mirror: ServerSkillRow, agent_id: str) -> BackendAgent:
        scope = resource_scope(tenant_id=str(mirror.tenant_id))
        actual = await retrieve_agent(edit.client, agent_id, scope=scope)
        name = (actual.metadata or {}).get(MA_METADATA_KEY_NAME, "")
        return await load_backend_agent(
            edit.client,
            runtime.sessionmaker,
            tenant_id=mirror.tenant_id,
            agent_name=name,
            actual_agent=actual,
        )

    return ConsentKeeper(sessionmaker=runtime.sessionmaker, renewer=renewer, load=load)


def sync_deps(runtime: WebRuntime, node: DataServerNode) -> SyncDeps:
    edit = change_deps(runtime).edit
    upstream = NodeUpstream(runtime, node)
    return SyncDeps(
        server=node.server.name,
        sessionmaker=runtime.sessionmaker,
        upstream=lambda _mirror: upstream,
        targets=(ManagedAgentsTarget(edit.client, consents=consent_keeper(runtime, edit)),),
        lock=lambda mirror: _lock(edit, mirror),
        now=runtime.now,
    )


async def sync_server_skills(runtime: WebRuntime, node: DataServerNode) -> None:
    """Check every mirror once; one that fails is retried on the next pass."""
    deps = sync_deps(runtime, node)
    async with runtime.sessionmaker() as session:
        mirrors = await store.list_mirrors(session)
    for mirror in mirrors:
        try:
            await check_mirror(deps, mirror.id)
        # Boundary: one mirror's surprise must not stop the others or the loop.
        except Exception:
            log.warning("server_skill.check_failed", mirror_id=str(mirror.id), exc_info=True)


async def sync_server_skills_forever(runtime: WebRuntime, node: DataServerNode) -> None:
    """Check every mirror now and then every `CHECK_INTERVAL_S`, until cancelled."""
    while True:
        try:
            await sync_server_skills(runtime, node)
        except SQLAlchemyError:
            log.warning("server_skill.sync_failed", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_S)


class ServerSkillOption(BaseModel):
    uri: str
    name: str
    description: str
    skill_id: str | None
    """The library skill mirroring it, once added."""
    taken: bool
    """Another library skill holds its name, so it can't be added."""


class ServerSkillMenu(BaseModel):
    label: str
    skills: list[ServerSkillOption]


class AddServerSkillBody(BaseModel):
    uri: str = Field(min_length=1, max_length=2048)


class FollowBody(BaseModel):
    skill_id: str = Field(min_length=1, max_length=200)
    follow: bool


class ApproveBody(BaseModel):
    digest: str = Field(min_length=1, max_length=128)
    """The waiting update's `pending_digest`, as the person reviewed it."""


class ForkBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class MirrorCheck(BaseModel):
    result: CheckResult
    server: MirrorDoc | None


def _options(
    raw: Sequence[JsonValue], mirrored: dict[str, str], names: frozenset[str]
) -> list[ServerSkillOption]:
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


def server_skills_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    def node_or_404() -> DataServerNode:
        node = server_skills_node(runtime)
        if node is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return node

    async def _ask[T](node: DataServerNode, person: WebPersonRow, call: _Call[T]) -> T:
        async with node_http(runtime, node.server) as http:
            try:
                token = await _token(runtime, node, person, http)
            except NodeAccessError as err:
                raise node.access_refusal(err) from err
            try:
                return await call(http, Endpoint(url=node.server.url, bearer=token))
            except (McpStatusError, httpx.HTTPError, ValueError) as err:
                if isinstance(err, McpStatusError) and err.status_code == 401:
                    await forget_node_token(runtime, person, node.server)
                log.warning("server_skill.listing_failed", err_type=type(err).__name__)
                raise HTTPException(
                    status_code=502, detail=f"{node.label} didn't answer. Try again soon."
                ) from err

    async def _mirror(caller_tenant: uuid.UUID, skill_id: str) -> ServerSkillRow:
        async with runtime.sessionmaker() as session:
            mirror = await store.mirror_of_skill(
                session, tenant_id=caller_tenant, skill_id=skill_id
            )
        if mirror is None:
            raise HTTPException(status_code=404, detail="No such server skill.")
        return mirror

    async def _checked(mirror: ServerSkillRow, result: CheckResult) -> MirrorCheck:
        async with runtime.sessionmaker() as session:
            fresh = await store.get_mirror(session, mirror.id)
        return MirrorCheck(result=result, server=mirror_doc(fresh))

    @router.get("/server-skills")
    async def menu(caller: Editor) -> ServerSkillMenu:  # pyright: ignore[reportUnusedFunction]
        node = node_or_404()

        async def listing(http: httpx.AsyncClient, endpoint: Endpoint) -> Sequence[JsonValue]:
            return await fetch_skill_listing(http, endpoint, keep=lambda _raw: True)

        raw = await _ask(node, caller.person, listing)
        async with runtime.sessionmaker() as session:
            mirrors = await store.list_mirrors(session, tenant_id=caller.person.tenant_id)
        mirrored = {m.uri: m.skill_id for m in mirrors if m.server == node.server.name}
        names = await custom_skill_names(change_deps(runtime).edit, actor=actor_of(caller))
        return ServerSkillMenu(label=node.label, skills=_options(raw, mirrored, names))

    @router.post("/server-skills", status_code=201)
    async def add(caller: Editor, body: AddServerSkillBody) -> SkillRef:  # pyright: ignore[reportUnusedFunction]
        node = node_or_404()

        async def fetch(http: httpx.AsyncClient, endpoint: Endpoint) -> ServerSkill:
            try:
                return await fetch_server_skill(http, endpoint, server=node.label, skill=body.uri)
            except SkillIngestError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        fetched = await _ask(node, caller.person, fetch)
        try:
            return await add_server_skill(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                person_id=caller.person.id,
                server=node.server.name,
                fetched=fetched,
                now=runtime.now(),
                audited=recorded(runtime, caller, "Added server skill"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/skills/{skill_id}/check")
    async def check(caller: Editor, skill_id: str) -> MirrorCheck:  # pyright: ignore[reportUnusedFunction]
        node = node_or_404()
        try:
            require_admin(actor_of(caller), "The skill library")
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        mirror = await _mirror(caller.person.tenant_id, skill_id)
        return await _checked(mirror, await check_mirror(sync_deps(runtime, node), mirror.id))

    @router.post("/skills/{skill_id}/approve")
    async def approve(caller: Editor, skill_id: str, body: ApproveBody) -> MirrorCheck:  # pyright: ignore[reportUnusedFunction]
        node = node_or_404()
        try:
            require_admin(actor_of(caller), "The skill library")
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        mirror = await _mirror(caller.person.tenant_id, skill_id)
        if mirror.pending_digest is None:
            raise HTTPException(status_code=409, detail="No update is waiting.")
        result = await approve_update(sync_deps(runtime, node), mirror.id, digest=body.digest)
        return await _checked(mirror, result)

    @router.post("/skills/{skill_id}/fork", status_code=201)
    async def fork(caller: Editor, skill_id: str, body: ForkBody) -> SkillRef:  # pyright: ignore[reportUnusedFunction]
        try:
            return await fork_library_skill(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                skill_id=skill_id,
                name=body.name,
                audited=recorded(runtime, caller, "Forked skill"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.get("/{agent_id}/skill-follows")
    async def follows(caller: Viewer, agent_id: str) -> dict[str, SkillFollow]:  # pyright: ignore[reportUnusedFunction]
        try:
            return await agent_follows(
                change_deps(runtime).edit, actor=actor_of(caller), agent_id=agent_id
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.put("/{agent_id}/skill-follows", status_code=204)
    async def put_follow(caller: Editor, agent_id: str, body: FollowBody) -> None:  # pyright: ignore[reportUnusedFunction]
        try:
            await set_agent_follow(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                agent_id=agent_id,
                skill_id=body.skill_id,
                follow=body.follow,
                now=runtime.now(),
                audited=recorded(runtime, caller, "Changed skill updates"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        node = server_skills_node(runtime)
        if body.follow and node is not None:
            await follow_now(
                sync_deps(runtime, node),
                tenant_id=actor_of(caller).tenant_id,
                skill_id=body.skill_id,
                agent_id=agent_id,
            )

    @router.post("/{agent_id}/skill-follows/{skill_id}/apply", status_code=204)
    async def apply_update(caller: Editor, agent_id: str, skill_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        try:
            await apply_held_update(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                agent_id=agent_id,
                skill_id=skill_id,
                audited=recorded(runtime, caller, "Applied skill update"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    return router
