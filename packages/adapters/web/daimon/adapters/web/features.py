"""`/api/features`: every feature at a workspace, agent, channel or thread, over the core service.

One reader and one writer for every key the registry holds, so a new key needs no route.
`GET /api/features/schema` describes each key for the controls the page draws; `GET` reads
a place; `PUT` changes one tier of it with the value the caller last read. Who may change a
tier is the core's rule; this only answers `admin_for` from the caller's role,
capabilities and, for a server-admin-only feature, the account's own role. It also
builds the agent tier of `tool.*` from the agent's own tool config.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.agent_access import actor_of as agent_actor_of
from daimon.adapters.web.agent_access import change_deps, edit_refusal
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import NOT_ALLOWED, WebCaller, current_caller
from daimon.adapters.web.channels import channel_refusals, one_row
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.admin_actions import DelegatedAction, admin_for_action
from daimon.core.agent_editing import (
    AgentEditError,
    builtin_reader,
    builtin_writer,
    load_tenant_agent,
)
from daimon.core.agent_ownership import owns_agent
from daimon.core.channel_changes import Expected
from daimon.core.channel_directory import WEB_ROW_ID
from daimon.core.features.registry import DEFAULT, Tier
from daimon.core.features.service import (
    FeatureActor,
    change_feature,
    read_features,
    thread_open_to_anyone,
)
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.surface import feature_schema, view_json
from daimon.core.stores.domain import CHAT_PLATFORMS
from daimon.core.stores.platform_names import get_channel_names
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import thread_in_channel
from daimon.core.web_roles_rules import server_admin
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

Caller = Annotated[WebCaller, Depends(current_caller)]
_NEEDED: dict[Tier, str] = {
    "workspace": "workspace.manage",
    "agent": "agents.edit",
    "channel": "channels.manage",
    "thread": "channels.manage",
}
_NO_WEB = "Web chat has no such setting."


class FeatureChangeBody(BaseModel):
    key: str
    tier: Tier
    value: str
    locked: bool = False
    expected: str | None = None
    """The tier's own value as last read (`tiers[].text`); None skips the check."""


class FeaturesDoc(BaseModel):
    platform: str
    features: list[dict[str, Any]]


def features_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/features")
    settings = runtime.settings

    async def read_place(
        caller: WebCaller,
        tenant: str | None,
        agent: str | None,
        channel: str | None,
        thread: str | None,
    ) -> FeaturePlace:
        tenant_id = caller.person.tenant_id
        if tenant is not None and tenant != str(tenant_id):
            raise HTTPException(404, "No such workspace.")
        if thread and not channel:
            raise HTTPException(422, "A thread belongs to a channel.")
        if channel == WEB_ROW_ID:
            raise HTTPException(422, _NO_WEB)
        if channel:
            platform = (await one_row(runtime, caller, channel)).platform
        else:
            async with runtime.sessionmaker() as db:
                found = await get_tenant(db, tenant_id)
            if found is None:
                raise HTTPException(404, "No such workspace.")
            platform = found.platform
        return FeaturePlace.of(tenant_id, platform, channel, thread, agent_id=agent)

    def readable(caller: WebCaller, place: FeaturePlace) -> None:
        needed = "agents.view" if place.channel_id is None and place.agent_id else "channels.view"
        if needed not in caller.capabilities:
            raise HTTPException(403, f"You can't see this. {NOT_ALLOWED}")

    async def names(caller: WebCaller, place: FeaturePlace) -> str:
        """What an audit row calls the place."""
        if place.channel_id is not None:
            async with runtime.sessionmaker() as db:
                found = await get_channel_names(
                    db,
                    tenant_id=place.tenant_id,
                    platform=place.platform,
                    channel_ids=(place.channel_id,),
                )
            name = found.get(place.channel_id)
            return f"#{name}" if name else place.channel_id
        if place.agent_id is not None:
            try:
                agent = await load_tenant_agent(
                    change_deps(runtime).edit,
                    actor=agent_actor_of(caller),
                    ma_agent_id=place.agent_id,
                )
            except AgentEditError:
                return place.agent_id
            return agent.name
        return "Workspace"

    async def builtins(caller: WebCaller, place: FeaturePlace) -> dict[str, bool | None] | None:
        if place.agent_id is None:
            return None
        try:
            return await builtin_reader(
                change_deps(runtime).edit,
                actor=agent_actor_of(caller),
                ma_agent_id=place.agent_id,
            )()
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.get("/schema")
    async def schema(caller: Caller) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        return feature_schema()

    @router.get("")
    async def read(  # pyright: ignore[reportUnusedFunction]
        caller: Caller,
        tenant: str | None = None,
        agent: str | None = None,
        channel: str | None = None,
        thread: str | None = None,
    ) -> FeaturesDoc:
        place = await read_place(caller, tenant, agent, channel, thread)
        readable(caller, place)
        with channel_refusals():
            views = await read_features(
                runtime.sessionmaker,
                settings,
                place,
                agent_builtins=await builtins(caller, place),
            )
        return FeaturesDoc(
            platform=place.platform, features=[view_json(v, place.platform) for v in views]
        )

    @router.put("")
    async def change(  # pyright: ignore[reportUnusedFunction]
        caller: Caller,
        body: FeatureChangeBody,
        tenant: str | None = None,
        agent: str | None = None,
        channel: str | None = None,
        thread: str | None = None,
    ) -> dict[str, Any]:
        place = await read_place(caller, tenant, agent, channel, thread)
        readable(caller, place)
        needed = _NEEDED.get(body.tier)
        if body.tier == "thread":
            # Who may change a thread is the core's rule (`thread_open_to_anyone`): anyone who can
            # read this channel where it holds, else a channel admin. The thread must be one this
            # workspace has a session in, run for this channel.
            if not thread_open_to_anyone(body.key, locked=body.locked) and (
                needed not in caller.capabilities
            ):
                raise HTTPException(403, f"You can't change this. {NOT_ALLOWED}")
            thread_key, channel_id = place.thread_key, place.channel_id
            async with runtime.sessionmaker() as db:
                if (
                    thread_key is None
                    or channel_id is None
                    or not await thread_in_channel(
                        db,
                        tenant_id=place.tenant_id,
                        platform=place.platform,
                        channel_id=channel_id,
                        thread_key=thread_key,
                    )
                ):
                    raise HTTPException(404, "No such thread in this channel.")
        account = caller.person.account_id

        async def admin_for(action: DelegatedAction | None) -> bool:
            """A workspace owner or admin may do a delegated action; with no action only a
            server admin (a platform admin or the chat workspace's own admin) may."""
            workspace_admin = needed in caller.capabilities and is_admin(caller.role)
            if not workspace_admin:
                return False
            async with runtime.sessionmaker() as db:
                platform_admin = await server_admin(db, caller.person)
            return admin_for_action(platform_admin=platform_admin, daimon_admin=True, action=action)

        async def owns(agent_id: str) -> bool:
            async with runtime.sessionmaker() as db:
                return await owns_agent(
                    db, tenant_id=place.tenant_id, ma_agent_id=agent_id, account_id=account
                )

        async def native_user() -> str | None:
            if place.platform not in CHAT_PLATFORMS:
                return None
            async with runtime.sessionmaker() as db:
                principal = await caller.principal(db)
            return principal.external_id if principal.platform == place.platform else None

        async def audited(db: AsyncSession) -> None:
            await audit(
                db,
                tenant_id=place.tenant_id,
                actor_account_id=account,
                action="Changed agent"
                if place.channel_id is None and place.agent_id
                else "Changed channel",
                target=await names(caller, place),
                detail=body.key,
                at=datetime.now(UTC),
            )

        read_tools: Callable[[], Awaitable[Mapping[str, bool | None]]] | None = None
        write_tool: Callable[[str, bool | None], Awaitable[bool | None]] | None = None
        if place.agent_id is not None and body.key.startswith("tool.") and body.tier == "agent":
            edit, agent_actor = change_deps(runtime).edit, agent_actor_of(caller)
            inner = builtin_writer(edit, actor=agent_actor, ma_agent_id=place.agent_id)
            read_tools = builtin_reader(edit, actor=agent_actor, ma_agent_id=place.agent_id)

            async def audited_write(tool: str, enabled: bool | None) -> bool | None:
                try:
                    was = await inner(tool, enabled)
                except AgentEditError as exc:
                    raise edit_refusal(exc) from exc
                async with runtime.sessionmaker.begin() as db:
                    await audited(db)
                return was

            write_tool = audited_write

        actor = FeatureActor(
            place.tenant_id,
            account,
            "web",
            await native_user(),
            admin_for,
            on_write=audited,
            owns_agent=owns,
            write_builtin=write_tool,
            read_builtins=read_tools,
        )
        with channel_refusals():
            result = await change_feature(
                runtime.sessionmaker,
                settings,
                actor,
                key=body.key,
                tier=body.tier,
                value=body.value or DEFAULT,
                locked=body.locked,
                place=place,
                expected=Expected(body.expected) if body.expected is not None else None,
            )
        return view_json(result.view, place.platform)

    return router
