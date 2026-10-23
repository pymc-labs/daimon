"""The owner is real on every surface: creators recorded, owner facts by who acts."""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

import httpx
import pytest
from daimon.core.agent_fork import _record_copy_owner  # pyright: ignore[reportPrivateUsage]
from daimon.core.agent_lifecycle import create_blank_agent
from daimon.core.agent_ownership import agent_owner
from daimon.core.agent_reach import load_target_facts
from daimon.core.errors import AgentNameCollision
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.native_admins import AdminCaller
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    json_body,
    list_response,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


@pytest.fixture
async def tenant_people(db_preflight: DatabasePreflight, db_committing_engine: AsyncEngine):
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        owner = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, tenant=tenant, account=owner, platform="slack", external_id="U-OWNER"
        )
        other = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, tenant=tenant, account=other, platform="slack", external_id="U-OTHER"
        )
        await agent_owners.set_owner(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_owned"),
            owner_account_id=owner.id,
            source="admin_assigned",
        )
    return sessions, tenant, owner, other


async def _facts(sessions, tenant, *, user: str | None, account: uuid.UUID | None, managed=False):
    async with sessions() as session:
        return await load_target_facts(
            session,
            "agent_spec_edit",
            tenant_id=tenant.id,
            platform="slack",
            agent_names=("helper",),
            ma_agent_id="ag_owned",
            default=DeploymentDefault(),
            caller=AdminCaller(platform_user_id=user),
            is_daimon_managed=managed,
            caller_account_id=account,
        )


async def test_owner_facts_follow_the_person_who_acts(tenant_people) -> None:
    sessions, tenant, owner, other = tenant_people
    assert (await _facts(sessions, tenant, user="U-OWNER", account=None)).owned_by_caller
    assert not (await _facts(sessions, tenant, user="U-OTHER", account=None)).owned_by_caller
    # A submit path carrying the requester's account counts the clicker, not the account.
    assert not (await _facts(sessions, tenant, user="U-OTHER", account=owner.id)).owned_by_caller
    assert (await _facts(sessions, tenant, user=None, account=owner.id)).owned_by_caller
    assert not (await _facts(sessions, tenant, user="U-NOBODY", account=None)).owned_by_caller
    # A defaults-managed agent is never the owner's to edit.
    managed = await _facts(sessions, tenant, user="U-OWNER", account=None, managed=True)
    assert not managed.owned_by_caller
    del other


def _ma(tenant_id: uuid.UUID) -> tuple[Any, list[dict[str, Any]], list[str]]:
    stored: list[dict[str, Any]] = []
    posts: list[str] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        made = ma_agent(id=f"ag_{len(stored)}", name=body["name"], tenant_id=tenant_id)
        payload = {**made.model_dump(mode="json"), "metadata": body["metadata"]}
        stored.append(payload)
        posts.append(req.url.path)
        return httpx.Response(200, json=payload)

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        posts.append(req.url.path)
        return httpx.Response(500)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response(list(stored)))
    router.add("POST", r"/v1/agents", on_create)
    router.add("POST", r"/v1/agents/([^/]+)", on_update)
    return build_fake_anthropic(router.dispatch), stored, posts


async def test_the_person_who_presses_new_agent_owns_it(tenant_people) -> None:
    sessions, tenant, owner, other = tenant_people
    client, _stored, posts = _ma(tenant.id)

    async def create(creator: uuid.UUID) -> Any:
        return await create_blank_agent(
            client,
            tenant_id=tenant.id,
            name="mine",
            system=None,
            model="claude-opus-4-5",
            account_id=uuid.uuid4(),
            public_url=None,
            sessionmaker=sessions,
            creator_account_id=creator,
        )

    created = await create(owner.id)
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=created.anthropic_id)
    assert await agent_owner(sessions, tenant_id=tenant.id, agent_id=agent_id) == owner.id
    with pytest.raises(AgentNameCollision):
        await create(other.id)
    assert posts == ["/v1/agents"], "the second creator neither created nor rewrote it"
    assert await agent_owner(sessions, tenant_id=tenant.id, agent_id=agent_id) == owner.id


async def test_two_panel_creators_of_one_name_get_one_agent(
    tenant_people, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.defaults import reconcile_agents

    sessions, tenant, owner, other = tenant_people
    client, _stored, posts = _ma(tenant.id)
    real_create = reconcile_agents.create_agent

    async def slow_create(*args: Any, **kwargs: Any) -> Any:
        # A slow provider call between "no such name" and the create.
        await asyncio.sleep(0.2)
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(reconcile_agents, "create_agent", slow_create)
    results = await asyncio.gather(
        *(
            create_blank_agent(
                client,
                tenant_id=tenant.id,
                name="race",
                system=None,
                model="claude-opus-4-5",
                account_id=uuid.uuid4(),
                public_url=None,
                sessionmaker=sessions,
                creator_account_id=person,
            )
            for person in (owner.id, other.id)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, AgentNameCollision) for r in results) == 1
    assert posts == ["/v1/agents"]


async def test_a_copy_is_owned_by_its_maker_and_a_channel_copy_keeps_the_owner(
    tenant_people,
) -> None:
    sessions, tenant, owner, other = tenant_people

    async def owner_of(ma_id: str) -> uuid.UUID | None:
        return await agent_owner(
            sessions,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_id),
        )

    await _record_copy_owner(
        sessions, tenant_id=tenant.id, source_id="ag_owned", copy_id="ag_fork", owner=other.id
    )
    assert await owner_of("ag_fork") == other.id
    await _record_copy_owner(
        sessions, tenant_id=tenant.id, source_id="ag_owned", copy_id="ag_chan", owner="source"
    )
    assert await owner_of("ag_chan") == owner.id
    await _record_copy_owner(
        sessions, tenant_id=tenant.id, source_id="ag_unowned", copy_id="ag_c2", owner="source"
    )
    assert await owner_of("ag_c2") is None
