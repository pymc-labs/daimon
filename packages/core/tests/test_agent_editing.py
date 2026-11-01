"""The shared agent edit service: pure rules, then authorized writes against a fake MA."""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
import pytest
from anthropic.types.beta.agent_create_params import Tool
from daimon.core import agent_editing
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.agent_editing import (
    AgentActor,
    AgentEditDeps,
    AgentEditError,
    AgentPatch,
    PlaceEdit,
)
from daimon.core.authz import Subject, Surface
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores import agent_owners
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.scoped_config_read import get_scope
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, json_body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ALLOW: dict[str, Any] = {"type": "always_allow"}
_TOOLSET: Tool = {
    "type": "agent_toolset_20260401",
    "default_config": {"enabled": True, "permission_policy": _ALLOW},
    "configs": [{"name": "bash", "enabled": False, "permission_policy": _ALLOW}],
}


def test_with_builtin_turns_one_tool_on_and_keeps_the_rest() -> None:
    tools = agent_editing.with_builtin([_TOOLSET], "bash", True)
    assert agent_editing.builtin_enabled(tools, "bash") is True, "bash is turned on"
    assert agent_editing.builtin_enabled(tools, "read") is True, "the default stays on"
    assert agent_editing.builtin_enabled([], "bash") is None, "no toolset says nothing"


def test_forbidden_turned_on_names_only_newly_enabled_denied_builtins() -> None:
    denied = agent_editing.denied_builtins(("builtin/bash", "bash", "builtin/web_fetch"))
    assert denied == frozenset({"bash", "web_fetch"}), "only builtin/<name> forbids a built-in"
    after = agent_editing.with_builtin([_TOOLSET], "bash", True)
    assert agent_editing.forbidden_turned_on(denied, [_TOOLSET], after) == ["bash"], (
        "web_fetch was already on by default, so only bash is newly turned on"
    )


def test_reserved_server_rejection_refuses_repointing_the_daimon_server() -> None:
    url = "https://daimon.example.com/mcp"
    same = [{"name": "daimon-mcp", "type": "url", "url": url + "/"}]
    moved = [{"name": "daimon-mcp", "type": "url", "url": "https://evil.example.com/mcp"}]
    assert agent_editing.reserved_server_rejection(same, url) is None, (  # type: ignore[arg-type]
        "re-sending the canonical entry is allowed"
    )
    assert agent_editing.reserved_server_rejection(moved, url) is not None, (  # type: ignore[arg-type]
        "the reserved name may not point elsewhere"
    )


def test_system_agent_rejection_covers_managed_and_unstamped_agents() -> None:
    tenant_id = uuid.uuid4()
    owned = ma_agent(tenant_id=tenant_id, metadata={"daimon_account": "a"})
    managed = ma_agent(
        tenant_id=tenant_id, metadata={"daimon_account": "a", "daimon_managed": "true"}
    )
    assert agent_editing.system_agent_rejection(owned) is None, "a user agent is editable"
    assert agent_editing.system_agent_rejection(managed) is not None, "a seeded agent is not"
    assert agent_editing.system_agent_rejection(ma_agent(tenant_id=tenant_id)) is not None, (
        "an agent with no account stamp is treated as seeded"
    )


def _router(
    tenant_id: uuid.UUID, updates: list[dict[str, Any]], *, version: int = 3, **meta: str
) -> MARouter:
    agent = ma_agent(
        id="ag_x",
        name="helper",
        tenant_id=tenant_id,
        metadata={"daimon_account": "acct", **meta},
        version=version,
        tools=[_TOOLSET],
    )

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        updates.append(body)
        updated = agent.model_copy(update={"version": agent.version + 1})
        return httpx.Response(200, json=updated.model_dump(mode="json"))

    router = MARouter()
    router.add_agent(agent)
    router.add("POST", r"/v1/agents/ag_x$", on_update)
    router.add(
        "GET",
        r"/v1/agents$",
        lambda _r, _m: httpx.Response(
            200, json={"data": [agent.model_dump(mode="json")], "has_more": False}
        ),
    )
    return router


async def _actor(session: AsyncSession, *, admin: bool = False, owner: bool = False) -> AgentActor:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    if owner:
        await agent_owners.record_creator(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_x"),
            account_id=account.id,
        )
    await session.commit()
    return AgentActor(
        tenant_id=tenant.id,
        account_id=account.id,
        subject=Subject(is_admin=admin, platform_user_id=str(account.id)),
        surface=Surface.WEB,
    )


def _deps(router: MARouter, factory: async_sessionmaker[AsyncSession]) -> AgentEditDeps:
    return AgentEditDeps(
        client=build_fake_anthropic(router.dispatch),
        session_factory=factory,
        public_url=None,
        denied_tools=("builtin/bash",),
    )


async def test_update_agent_by_owner_writes_the_replaced_lists(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, owner=True)
    updates: list[dict[str, Any]] = []
    deps = _deps(_router(actor.tenant_id, updates), db_session_factory)
    patch = AgentPatch(description="Writes reports", skills=[], merge="replace")
    updated = await agent_editing.update_agent(
        deps, actor=actor, ma_agent_id="ag_x", patch=patch, expected_version=3
    )
    assert updated.version == 4, "the service returns the agent MA wrote"
    assert updates[0]["description"] == "Writes reports", "the description is written"
    assert updates[0]["skills"] == [], "replace writes the empty skill list as given"


async def test_update_agent_refuses_a_member_who_does_not_own_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    updates: list[dict[str, Any]] = []
    deps = _deps(_router(actor.tenant_id, updates), db_session_factory)
    with pytest.raises(AgentEditError) as raised:
        await agent_editing.update_agent(
            deps,
            actor=actor,
            ma_agent_id="ag_x",
            patch=AgentPatch(description="x"),
            expected_version=None,
        )
    assert raised.value.kind == "forbidden", "a non-owner member may not edit"
    assert updates == [], "nothing is written"


async def test_update_agent_refuses_a_managed_agent_even_for_an_admin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, admin=True)
    updates: list[dict[str, Any]] = []
    deps = _deps(_router(actor.tenant_id, updates, daimon_managed="true"), db_session_factory)
    with pytest.raises(AgentEditError) as raised:
        await agent_editing.update_agent(
            deps,
            actor=actor,
            ma_agent_id="ag_x",
            patch=AgentPatch(system="x"),
            expected_version=None,
        )
    assert raised.value.kind == "forbidden", "a seeded agent is edited by forking it"
    assert updates == [], "nothing is written"


async def test_update_agent_refuses_a_stale_version(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, admin=True)
    updates: list[dict[str, Any]] = []
    deps = _deps(_router(actor.tenant_id, updates, version=5), db_session_factory)
    with pytest.raises(AgentEditError) as raised:
        await agent_editing.update_agent(
            deps,
            actor=actor,
            ma_agent_id="ag_x",
            patch=AgentPatch(description="x"),
            expected_version=3,
        )
    assert raised.value.kind == "stale", "an edit begun on version 3 does not overwrite 5"
    assert updates == [], "nothing is written"


async def test_update_agent_refuses_turning_on_a_forbidden_builtin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, admin=True)
    updates: list[dict[str, Any]] = []
    deps = _deps(_router(actor.tenant_id, updates), db_session_factory)
    tools = agent_editing.with_builtin([_TOOLSET], "bash", True)
    with pytest.raises(AgentEditError) as raised:
        await agent_editing.update_agent(
            deps,
            actor=actor,
            ma_agent_id="ag_x",
            patch=AgentPatch(tools=tools, merge="replace"),
            expected_version=None,
        )
    assert raised.value.kind == "forbidden", "a built-in the operator denied stays off"
    assert updates == [], "nothing is written"


def test_default_changes_name_the_channels_gained_and_lost_and_ignore_web() -> None:
    before = [PlaceEdit("c1", True), PlaceEdit("c2"), PlaceEdit("web", True)]
    after = [PlaceEdit("c2", True), PlaceEdit("c3", True), PlaceEdit("web")]
    assert agent_editing.default_changes(before, after) == (("c2", "c3"), ("c1",)), (
        "c1 stops being a default, c2 and c3 become one, web has no default"
    )
    assert agent_editing.default_changes(before, [PlaceEdit("c2")]) == ((), ("c1",)), (
        "a removed place stops being a default"
    )


def test_places_changed_sees_a_place_or_a_default_but_not_order() -> None:
    a = [PlaceEdit("c1", True), PlaceEdit("web")]
    assert not agent_editing.places_changed(a, list(reversed(a))), "order is not a change"
    assert agent_editing.places_changed(a, [PlaceEdit("c1"), PlaceEdit("web")]), "a default"
    assert agent_editing.places_changed(a, [PlaceEdit("c1", True)]), "a place"


def test_places_refusal_names_a_default_outside_the_agents_rule() -> None:
    policy = TenantAccessPolicy()
    ruled = agent_editing.places_policy(
        policy, names=("helper",), agent_name="helper", runs_in=("c1",)
    )
    refusal = agent_editing.places_refusal(
        ruled, names=("helper",), agent_name="helper", turn_on=["c9"], turn_off=[]
    )
    assert refusal is not None, "an agent routed outside its rule would refuse every turn there"
    assert (
        agent_editing.places_refusal(
            ruled, names=("helper",), agent_name="helper", turn_on=["c1"], turn_off=[]
        )
        is None
    ), "a channel inside the rule is fine"
    kept = TenantAccessPolicy(channel_rules={"c1": ChannelRule(readers="own", writers="own")})
    cleared = agent_editing.places_refusal(
        kept, names=("helper",), agent_name="helper", turn_on=[], turn_off=["c1"]
    )
    assert cleared is not None, "a channel kept to its own agents keeps one as its default"


async def test_set_places_adds_a_default_to_an_unrestricted_agent_and_keeps_it_unrestricted(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, admin=True)
    deps = _deps(_router(actor.tenant_id, []), db_session_factory)
    agent = await agent_editing.load_editable_agent(deps, actor=actor, ma_agent_id="ag_x")
    await agent_editing.set_places(
        deps,
        actor=actor,
        agent=agent,
        before=[PlaceEdit("web")],
        after=[PlaceEdit("c1", True), PlaceEdit("web")],
    )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        scope = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=actor.tenant_id, channel_id="c1")
        )
    assert "helper" not in policy.agent_rules, "no rule is written, so DMs and channels still work"
    assert scope is not None and scope.agent_name == "helper", "c1 now answers with helper"


async def test_set_places_removes_a_place_and_restricts_an_unrestricted_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, admin=True)
    deps = _deps(_router(actor.tenant_id, []), db_session_factory)
    agent = await agent_editing.load_editable_agent(deps, actor=actor, ma_agent_id="ag_x")
    both = [PlaceEdit("c1", True), PlaceEdit("web")]
    await agent_editing.set_places(deps, actor=actor, agent=agent, before=[], after=both)
    await agent_editing.set_places(
        deps, actor=actor, agent=agent, before=both, after=[PlaceEdit("web")]
    )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        scope = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=actor.tenant_id, channel_id="c1")
        )
    assert policy.agent_rules["helper"].runs_in == ("web",), "the removal restricts the agent"
    assert scope is None or scope.agent_name is None, "and c1 is no longer helper's default"


async def test_set_places_rewrites_the_rule_of_a_restricted_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, admin=True)
    deps = _deps(_router(actor.tenant_id, []), db_session_factory)
    agent = await agent_editing.load_editable_agent(deps, actor=actor, ma_agent_id="ag_x")
    both = [PlaceEdit("c1", True), PlaceEdit("web")]
    await agent_editing.set_places(deps, actor=actor, agent=agent, before=[], after=both)
    await agent_editing.set_places(
        deps, actor=actor, agent=agent, before=both, after=[PlaceEdit("web")]
    )
    await agent_editing.set_places(
        deps,
        actor=actor,
        agent=agent,
        before=[PlaceEdit("web")],
        after=[PlaceEdit("web"), PlaceEdit("c2", True)],
    )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
    assert policy.agent_rules["helper"].runs_in == ("web", "c2"), (
        "an add on a restricted agent rewrites its rule to the listed places"
    )


async def test_set_places_refuses_an_owner_who_is_not_an_admin_and_writes_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, owner=True)
    deps = _deps(_router(actor.tenant_id, []), db_session_factory)
    agent = await agent_editing.load_editable_agent(deps, actor=actor, ma_agent_id="ag_x")
    with pytest.raises(AgentEditError) as refused:
        await agent_editing.set_places(
            deps, actor=actor, agent=agent, before=[], after=[PlaceEdit("c1", True)]
        )
    assert refused.value.kind == "forbidden", "where an agent answers is an admin's to set"
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
    assert "helper" not in policy.agent_rules, "nothing was written"
