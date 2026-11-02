"""`/api/routines/*`: a person's routines, and an admin's view of every one.

Every check lives in `daimon.core.routine_service`, the same one the chat tool and the
chat panels use; this module describes who is asking and shapes the answers. Members
manage their own routines, admins all of them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.access import is_admin
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.directory import chattable
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import credit_views, routine_service
from daimon.core.authz import Action, Place, Surface, authorize
from daimon.core.cron_words import schedule_words
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.platform_names import UNNAMED_CHANNEL
from daimon.core.routine_service import (
    Happened,
    InvalidSchedule,
    NewRoutine,
    Placement,
    RoutineCaller,
    RoutineChange,
    RoutineEnv,
    RoutineNotFound,
    RoutineRefused,
    UnknownAgent,
)
from daimon.core.stores import platform_names as names_store
from daimon.core.stores import usage_events
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import CatchUpPolicy, RoutineDestinationKind, RoutineRow
from daimon.core.stores.tenants import get_tenant
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

Viewer = Annotated[WebCaller, Depends(requires("routines.view"))]
Manager = Annotated[WebCaller, Depends(requires("routines.manage"))]

GONE = "This routine isn't here."
BAD_SCHEDULE = "That schedule or time zone isn't valid."
PICK_AGENT = "Pick an agent from the list."
PICK_CHANNEL = "Pick a channel from the list."
UNAVAILABLE = "Routines are not available right now."

_ACTIONS: dict[Happened, str] = {
    "created": "Created routine",
    "changed": "Changed routine",
    "paused": "Paused routine",
    "resumed": "Resumed routine",
    "deleted": "Deleted routine",
}


class Choice(BaseModel):
    id: str
    name: str


class Destination(BaseModel):
    id: str
    name: str


class Routine(BaseModel):
    id: str
    agent: Choice
    cron: str
    timezone: str
    schedule: str
    message: str
    enabled: bool
    catch_up: CatchUpPolicy
    destination: Destination | None
    next_run: str | None
    last_run: str | None
    failed: bool
    last_output: str | None
    mine: bool
    owner: str | None


class Choices(BaseModel):
    agents: list[Choice]
    destinations: list[Destination]
    can_create: bool
    platform: str


class NewBody(BaseModel):
    agent_id: str
    cron: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=20_000)
    catch_up: CatchUpPolicy = "skip"
    enabled: bool = True
    destination: str | None = None


class EditBody(BaseModel):
    agent_id: str | None = None
    cron: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    message: str | None = Field(default=None, min_length=1, max_length=20_000)
    catch_up: CatchUpPolicy | None = None
    destination: str | None = None
    clear_destination: bool = False


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _agent_name(agent: BetaManagedAgentsAgent) -> str:
    return str(agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name)


def _refusal(error: RoutineRefused) -> HTTPException:
    """The service's refusal in the status the app reads."""
    if isinstance(error, RoutineNotFound):
        return HTTPException(status_code=404, detail=GONE)
    if isinstance(error, InvalidSchedule):
        return HTTPException(status_code=422, detail=BAD_SCHEDULE)
    if isinstance(error, UnknownAgent):
        return HTTPException(status_code=422, detail=PICK_AGENT)
    return HTTPException(status_code=422, detail=str(error))


def routines_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/routines")

    def anthropic() -> AsyncAnthropic:
        client = runtime.anthropic or (runtime.turns.deps.anthropic if runtime.turns else None)
        if client is None:
            raise HTTPException(status_code=503, detail=UNAVAILABLE)
        return client

    async def usable_agents(db: AsyncSession, caller: WebCaller) -> list[BetaManagedAgentsAgent]:
        """The agents this person may chat with, which a member may also schedule."""
        tenant_id = caller.person.tenant_id
        listed = await runtime.agents.agents(anthropic(), tenant_id)
        principal = await caller.principal(db)
        found = await chattable(
            db, listed, person=caller.person, role=caller.role, principal=principal
        )
        return sorted(found, key=lambda a: _agent_name(a).lower())

    async def reachable_channels(
        db: AsyncSession, caller: WebCaller, platform: str
    ) -> dict[str, str]:
        """The channels a routine of this person may deliver to, by id.

        Every channel the workspace knows that the rules let people write in; for a
        member, only those they have had replies in. This process cannot ask the chat
        platform what the person may see, so their own use stands in for it.
        """
        tenant_id = caller.person.tenant_id
        known = {
            row.channel_id: row.name
            for row in await names_store.list_channel_names(
                db, tenant_id=tenant_id, platform=platform
            )
        }
        principal = await caller.principal(db)
        policy = await load_access_policy(db, tenant_id=tenant_id)
        if not is_admin(caller.role):
            used = await usage_events.channels_used_by_user(
                db, tenant_id=tenant_id, platform_user_id=principal.external_id
            )
            known = {cid: name for cid, name in known.items() if cid in used}
        return {
            cid: name
            for cid, name in sorted(known.items(), key=lambda item: item[1].lower())
            if authorize(
                policy,
                subject=principal.subject,
                action=Action.POST,
                surface=Surface.ROUTINE,
                place=Place(channel_id=cid, category_unresolved=True),
            )
        }

    async def service(
        caller: WebCaller,
    ) -> tuple[RoutineEnv, RoutineCaller, str]:
        """The service's view of this request, with the workspace's chat platform."""
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            tenant = await get_tenant(db, tenant_id)
            principal = await caller.principal(db)
            agents = await usable_agents(db, caller)
        if tenant is None:
            raise HTTPException(status_code=404, detail=GONE)
        platform = tenant.platform

        async def resolve(kind: RoutineDestinationKind, destination_id: str) -> Placement:
            async with runtime.sessionmaker() as db:
                channels = await reachable_channels(db, caller, platform)
            if kind != "channel" or destination_id not in channels:
                raise RoutineRefused(PICK_CHANNEL)
            return Placement(channel_id=destination_id, category_unresolved=True)

        async def record(db: AsyncSession, happened: Happened, row: RoutineRow) -> None:
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=caller.person.account_id,
                action=_ACTIONS[happened],
                target="Routine",
                detail=row.agent_name,
                at=runtime.now(),
            )

        env = RoutineEnv(
            sessionmaker=runtime.sessionmaker,
            anthropic=anthropic(),
            resolve_destination=resolve,
            load_view=routine_service.isolation_view_loader(
                runtime.sessionmaker, anthropic(), tenant_id
            ),
            on_change=record,
            now=runtime.now,
        )
        acting = RoutineCaller(
            tenant_id=tenant_id,
            subject=principal.subject,
            platform=platform,
            is_admin=is_admin(caller.role),
            own_agent_ids=frozenset(
                derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=a.id) for a in agents
            ),
        )
        return env, acting, principal.platform

    def require_linked(chat_platform: str, linked_platform: str) -> None:
        """A routine runs as its owner's chat account, so only a linked one can own it."""
        if linked_platform != chat_platform:
            raise HTTPException(
                status_code=409,
                detail=f"Link your {chat_platform.title()} account to schedule routines.",
            )

    async def agent_named(db: AsyncSession, caller: WebCaller, agent_id: str) -> str:
        agent = next((a for a in await usable_agents(db, caller) if a.id == agent_id), None)
        if agent is None:
            raise HTTPException(status_code=422, detail=PICK_AGENT)
        return _agent_name(agent)

    async def shown(
        caller: WebCaller, rows: list[RoutineRow], acting: RoutineCaller
    ) -> list[Routine]:
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            tenant = await get_tenant(db, tenant_id)
            platform = tenant.platform if tenant is not None else ""
            channels = await names_store.get_channel_names(
                db,
                tenant_id=tenant_id,
                platform=platform,
                channel_ids=[
                    c
                    for r in rows
                    if (
                        c := routine_service.destination_channel_of(
                            platform, r.destination_kind, r.destination_id
                        )
                    )
                    is not None
                ],
            )
            owners = (
                await credit_views.label_people(
                    db,
                    tenant_id=tenant_id,
                    user_ids=[
                        r.created_by_user_id
                        for r in rows
                        if r.created_by_user_id is not None
                        and r.created_by_user_id != acting.user_id
                    ],
                )
                if acting.is_admin
                else {}
            )
            agents = {a.id: _agent_name(a) for a in await usable_agents(db, caller)}
        out: list[Routine] = []
        for row in rows:
            channel = routine_service.destination_channel_of(
                platform, row.destination_kind, row.destination_id
            )
            destination = None
            if row.destination_kind is not None and row.destination_id is not None:
                name = (
                    channels.get(channel or "", UNNAMED_CHANNEL)
                    if row.destination_kind == "channel"
                    else "Thread"
                )
                destination = Destination(id=row.destination_id, name=name)
            mine = row.created_by_user_id is not None and row.created_by_user_id == acting.user_id
            label = owners.get(row.created_by_user_id or "")
            out.append(
                Routine(
                    id=str(row.id),
                    agent=Choice(id=row.agent_id, name=agents.get(row.agent_id, row.agent_name)),
                    cron=row.cron_expr,
                    timezone=row.timezone,
                    schedule=schedule_words(row.cron_expr, row.timezone),
                    message=row.trigger_message,
                    enabled=row.enabled,
                    catch_up=row.catch_up_policy,
                    destination=destination,
                    next_run=_iso(row.next_fire_at),
                    last_run=_iso(row.last_fired_at),
                    failed=row.last_error is not None,
                    last_output=row.last_error or row.last_result_tail,
                    mine=mine,
                    owner=None if mine else (label.name if label else None),
                )
            )
        return out

    async def one(caller: WebCaller, routine_id: uuid.UUID) -> Routine:
        env, acting, _ = await service(caller)
        try:
            row = await routine_service.get_routine(env, acting, routine_id)
        except RoutineRefused as refused:
            raise _refusal(refused) from refused
        return (await shown(caller, [row], acting))[0]

    @router.get("")
    async def listing(caller: Viewer) -> list[Routine]:  # pyright: ignore[reportUnusedFunction]
        env, acting, _ = await service(caller)
        try:
            rows = await routine_service.list_routines(env, acting)
        except RoutineRefused as refused:
            raise _refusal(refused) from refused
        rows.sort(key=lambda r: (not r.enabled, r.next_fire_at is None, r.next_fire_at, r.id))
        return await shown(caller, rows, acting)

    @router.get("/choices")
    async def choices(caller: Viewer) -> Choices:  # pyright: ignore[reportUnusedFunction]
        _, acting, linked = await service(caller)
        async with runtime.sessionmaker() as db:
            agents = await usable_agents(db, caller)
            channels = await reachable_channels(db, caller, acting.platform)
        return Choices(
            agents=[Choice(id=a.id, name=_agent_name(a)) for a in agents],
            destinations=[Destination(id=i, name=n) for i, n in channels.items()],
            can_create=linked == acting.platform,
            platform=acting.platform,
        )

    @router.post("")
    async def create(body: NewBody, caller: Manager) -> Routine:  # pyright: ignore[reportUnusedFunction]
        env, acting, linked = await service(caller)
        require_linked(acting.platform, linked)
        async with runtime.sessionmaker() as db:
            name = await agent_named(db, caller, body.agent_id)
        try:
            row = await routine_service.create_routine(
                env,
                acting,
                NewRoutine(
                    agent_name=name,
                    cron_expr=body.cron,
                    timezone=body.timezone,
                    trigger_message=body.message,
                    enabled=body.enabled,
                    catch_up_policy=body.catch_up,
                    destination_kind="channel" if body.destination else None,
                    destination_id=body.destination,
                ),
            )
        except RoutineRefused as refused:
            raise _refusal(refused) from refused
        return (await shown(caller, [row], acting))[0]

    @router.patch("/{routine_id}")
    async def edit(routine_id: uuid.UUID, body: EditBody, caller: Manager) -> Routine:  # pyright: ignore[reportUnusedFunction]
        env, acting, _ = await service(caller)
        name = None
        if body.agent_id is not None:
            async with runtime.sessionmaker() as db:
                name = await agent_named(db, caller, body.agent_id)
        try:
            row = await routine_service.update_routine(
                env,
                acting,
                routine_id,
                RoutineChange(
                    agent_name=name,
                    cron_expr=body.cron,
                    timezone=body.timezone,
                    trigger_message=body.message,
                    catch_up_policy=body.catch_up,
                    destination_kind="channel" if body.destination else None,
                    destination_id=body.destination,
                    clear_destination=body.clear_destination,
                ),
            )
        except RoutineRefused as refused:
            raise _refusal(refused) from refused
        return (await shown(caller, [row], acting))[0]

    async def toggled(routine_id: uuid.UUID, caller: WebCaller, *, enabled: bool) -> Routine:
        env, acting, _ = await service(caller)
        try:
            row = await routine_service.set_enabled(env, acting, routine_id, enabled=enabled)
        except RoutineRefused as refused:
            raise _refusal(refused) from refused
        return (await shown(caller, [row], acting))[0]

    @router.post("/{routine_id}/pause")
    async def pause(routine_id: uuid.UUID, caller: Manager) -> Routine:  # pyright: ignore[reportUnusedFunction]
        return await toggled(routine_id, caller, enabled=False)

    @router.post("/{routine_id}/resume")
    async def resume(routine_id: uuid.UUID, caller: Manager) -> Routine:  # pyright: ignore[reportUnusedFunction]
        return await toggled(routine_id, caller, enabled=True)

    @router.delete("/{routine_id}", status_code=204)
    async def remove(routine_id: uuid.UUID, caller: Manager) -> None:  # pyright: ignore[reportUnusedFunction]
        env, acting, _ = await service(caller)
        try:
            await routine_service.delete_routine(env, acting, routine_id)
        except RoutineRefused as refused:
            raise _refusal(refused) from refused

    @router.get("/{routine_id}")
    async def detail(routine_id: uuid.UUID, caller: Viewer) -> Routine:  # pyright: ignore[reportUnusedFunction]
        return await one(caller, routine_id)

    return router
