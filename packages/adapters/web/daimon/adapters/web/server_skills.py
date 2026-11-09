"""Skills a connection node's server serves: the agent editor's menu, mirrors and their sync.

Each call reaches the server as a person, with the token the node exchanges for them; the
sync asks as the account that added the skill, so the server's own visibility rules decide.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

import httpx
import structlog
from daimon.adapters.web.agent_access import Editor, Viewer, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.nodes.data_server import DataServerNode
from daimon.adapters.web.nodes.exchange import (
    NodeAccessError,
    node_exchange_of,
    require_served,
    serves_tenant,
)
from daimon.adapters.web.nodes.kinds import build_nodes
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentEditError
from daimon.core.mcp_rpc import McpStatusError
from daimon.core.mcp_skills.install import ServerSkill, fetch_server_skill
from daimon.core.mcp_skills.protocol import Endpoint, fetch_serves_skills
from daimon.core.node_exchange import NodeAsker, asker_of_person
from daimon.core.server_skills.follows import (
    SkillFollow,
    agent_follows,
    apply_held_update,
    set_agent_follow,
)
from daimon.core.server_skills.mirror import add_server_skill
from daimon.core.server_skills.node import (
    NodeCall,
    ServerSkillOption,
    ServesCache,
    SkillsNode,
    ask_node,
    list_server_skills,
    skill_sync_deps,
)
from daimon.core.server_skills.sync import (
    CheckResult,
    SyncDeps,
    approve_update,
    check_mirror,
    follow_now,
)
from daimon.core.skill_library import MirrorDoc, mirror_doc
from daimon.core.skill_library_files import SkillRef, fork_library_skill
from daimon.core.skills.ingest import SkillIngestError
from daimon.core.stores import server_skills as store
from daimon.core.stores.domain import ServerSkillRow, WebPersonRow
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

__all__ = [
    "server_skills_node",
    "server_skills_router",
    "sync_deps",
    "sync_server_skills_forever",
]

log = structlog.get_logger(__name__)

CHECK_INTERVAL_S: Final = 900
"""Seconds between checks of each added skill for a new version."""


def server_skills_node(runtime: WebRuntime) -> DataServerNode | None:
    """The node whose server's skills are offered; None without a data server node."""
    if runtime.anthropic is None:
        return None
    return next((n for n in build_nodes(runtime.settings) if isinstance(n, DataServerNode)), None)


def sync_deps(runtime: WebRuntime, node: SkillsNode) -> SyncDeps:
    turns = runtime.turns
    return skill_sync_deps(
        node=node,
        exchange=node_exchange_of(runtime),
        shared=runtime.http_client,
        edit=change_deps(runtime).edit,
        turn_settings=runtime.settings.turn,
        turn_deps=lambda: None if turns is None else turns.deps,
        now=runtime.now,
    )


async def sync_server_skills(runtime: WebRuntime, node: DataServerNode) -> None:
    """Check every mirror once; one that fails is retried on the next pass."""
    deps = sync_deps(runtime, node.skills_node())
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


def server_skills_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")
    served = ServesCache()

    async def node_or_404(caller: WebCaller) -> DataServerNode:
        """The node, or 404 as when none is configured: also for a workspace it doesn't serve."""
        node = server_skills_node(runtime)
        if node is None:
            raise HTTPException(status_code=404, detail="Not found.")
        await require_served(runtime, node.server, caller.person.tenant_id)
        return node

    async def _answered[T](
        node: DataServerNode, person: WebPersonRow, run: Callable[[NodeAsker], Awaitable[T]]
    ) -> T:
        if runtime.identity_key is None:
            raise node.access_refusal(NodeAccessError("unconfigured"))
        async with runtime.sessionmaker() as db:
            asker = await asker_of_person(db, person)
        try:
            return await run(asker)
        except NodeAccessError as err:
            raise node.access_refusal(err) from err
        except (McpStatusError, httpx.HTTPError, ValueError) as err:
            log.warning("server_skill.listing_failed", err_type=type(err).__name__)
            raise HTTPException(
                status_code=502, detail=f"{node.label} didn't answer. Try again soon."
            ) from err

    async def _ask[T](node: DataServerNode, person: WebPersonRow, call: NodeCall[T]) -> T:
        exchange, skills = node_exchange_of(runtime), node.skills_node()
        return await _answered(
            node,
            person,
            lambda asker: ask_node(exchange, runtime.http_client, skills, asker, call),
        )

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

    async def _serves(node: DataServerNode, person: WebPersonRow) -> bool:
        """Whether the node's server offers skills; remembered, so editor loads don't ask."""
        return await served.serves(
            node.server.name, lambda: _ask(node, person, fetch_serves_skills)
        )

    @router.get("/server-skills")
    async def menu(caller: Editor) -> ServerSkillMenu:  # pyright: ignore[reportUnusedFunction]
        node = await node_or_404(caller)
        if not await _serves(node, caller.person):
            raise HTTPException(status_code=404, detail="Not found.")

        exchange, skills = node_exchange_of(runtime), node.skills_node()
        edit, actor = change_deps(runtime).edit, actor_of(caller)
        options = await _answered(
            node,
            caller.person,
            lambda asker: list_server_skills(
                exchange, runtime.http_client, skills, asker, edit=edit, actor=actor
            ),
        )
        return ServerSkillMenu(label=node.label, skills=options)

    @router.post("/server-skills", status_code=201)
    async def add(caller: Editor, body: AddServerSkillBody) -> SkillRef:  # pyright: ignore[reportUnusedFunction]
        node = await node_or_404(caller)

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
                server=node.server.name,
                fetched=fetched,
                now=runtime.now(),
                audited=recorded(runtime, caller, "Added server skill"),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/skills/{skill_id}/check")
    async def check(caller: Editor, skill_id: str) -> MirrorCheck:  # pyright: ignore[reportUnusedFunction]
        node = await node_or_404(caller)
        try:
            require_admin(actor_of(caller), "The skill library")
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        mirror = await _mirror(caller.person.tenant_id, skill_id)
        return await _checked(
            mirror, await check_mirror(sync_deps(runtime, node.skills_node()), mirror.id)
        )

    @router.post("/skills/{skill_id}/approve")
    async def approve(caller: Editor, skill_id: str, body: ApproveBody) -> MirrorCheck:  # pyright: ignore[reportUnusedFunction]
        node = await node_or_404(caller)
        try:
            require_admin(actor_of(caller), "The skill library")
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        mirror = await _mirror(caller.person.tenant_id, skill_id)
        if mirror.pending_digest is None:
            raise HTTPException(status_code=409, detail="No update is waiting.")
        result = await approve_update(
            sync_deps(runtime, node.skills_node()), mirror.id, digest=body.digest
        )
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
        # Without the node (or its server serving this workspace) there is nothing to fetch.
        if (
            body.follow
            and node is not None
            and await serves_tenant(runtime.sessionmaker, node.server, caller.person.tenant_id)
        ):
            await follow_now(
                sync_deps(runtime, node.skills_node()),
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
