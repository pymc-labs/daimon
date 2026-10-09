"""`create_agent` makes a new agent its channel admins' only when one made it from their channel."""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    _create_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores.agent_creation_channels import get_creation_channel
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOM = "111111111111111111"
THREAD = "555555555555555555"
USER = "444444444444444444"
BUILTIN = "agent_builtin"


async def test_create_agent_records_the_channel_a_channel_admin_made_it_for(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await set_channel_admins(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=ROOM,
            role_ids=[],
            user_ids=[USER],
            actor_account_id=None,
        )
        now = dt.datetime.now(dt.UTC)
        origins = {
            thread: str(
                (
                    await create_origin(
                        session,
                        tenant_id=tenant.id,
                        account_id=account.id,
                        platform="discord",
                        parent_channel_id=ROOM,
                        thread_id=thread,
                        responder_ma_agent_id=BUILTIN,
                        responder_name="daimon",
                        configuration_target_ma_agent_id=None,
                        configuration_target_name=None,
                        role=Role.USER,
                        expires_at=now + dt.timedelta(minutes=10),
                        now=now,
                        is_setup=True,
                    )
                ).id
            )
            for thread in (THREAD, f"dm:{uuid.uuid4()}")
        }
    ids = (f"agent_{n}" for n in itertools.count())
    created: list[str] = []

    def create(_req: httpx.Request, _m: object) -> httpx.Response:
        created.append(next(ids))
        return httpx.Response(200, json=ma_agent(id=created[-1]).model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    router.add("POST", r"/v1/agents", create)
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=ma_agent(id=created[-1]).model_dump(mode="json")),
    )
    runtime = McpRuntime(
        session_factory=committing_sessionmaker,
        client=build_fake_anthropic(router.dispatch),
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )

    def auth(*, user: str = USER, admin: bool = False) -> AuthIdentity:
        return AuthIdentity(
            account_id=account.id,
            tenant_id=tenant.id,
            role=Role.ADMIN if admin else Role.USER,
            platform="discord",
            platform_user_id=user,
            is_admin=admin,
            chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=BUILTIN),
        )

    async def made_for(caller: AuthIdentity, origin: str | None) -> str | None:
        spec = AgentSpec(name=f"bot-{len(created)}", model="claude-opus-4-5")
        await _create_agent_impl(runtime, caller, spec, origin)
        async with committing_sessionmaker() as session:
            return await get_creation_channel(
                session, tenant_id=tenant.id, ma_agent_id=created[-1], platform="discord"
            )

    assert await made_for(auth(), origins[THREAD]) == ROOM, (
        "a channel admin's agent from their channel's setup thread is that channel's"
    )
    assert await made_for(auth(), None) is None, "with no verified origin it is nobody's"
    dm = next(origin for thread, origin in origins.items() if thread != THREAD)
    assert await made_for(auth(), dm) is None, "a private conversation is in no channel"
    assert await made_for(auth(user="999"), origins[THREAD]) is None, (
        "a member's agent is not the channel's"
    )
    assert await made_for(auth(admin=True), origins[THREAD]) is None, (
        "a server admin's agent reaches a channel by being made its default"
    )
    granted = dataclasses.replace(auth(), administered_channel_ids=frozenset({ROOM}))
    before = len(created)
    with pytest.raises(ToolError, match="needs this turn's origin_context_id"):
        await made_for(granted, None)
    assert len(created) == before, "a channel admin's chat create without its origin makes nothing"
    assert await made_for(granted, origins[THREAD]) == ROOM, "with it, the agent is theirs"
