"""J4 end to end, offline: "make a Flash agent and a luna agent and fleet them".

A member creates both agents from chat with the real create_agent tool, naming Gemini
and OpenAI models directly (no Claude-first, no switch afterwards): each is created on
its own provider in that one call. The root then runs a fleet of the two through the
production path (grant -> worker admission -> spawn -> native dispatch, fake provider
transports), and each worker runs on the provider and model it was created on, never on
the Claude record its definition is saved with. Any refusal here is a product bug.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import httpx
import pytest
from daimon.adapters.mcp.tools.agents import (
    _build_create_spec,  # pyright: ignore[reportPrivateUsage]
    _create_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _create_model,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import list_response

from packages.adapters.mcp.tests.tools.test_fleet import AUTH, runtime
from packages.adapters.mcp.tests.tools.test_j4_named_agents_e2e import _run_named_fleet
from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import j10_root as j10_root

#: What the member asks for, by the names people use.
ASKED = {"flash-agent": "flash", "luna-agent": "luna"}


async def create_from_chat(runtime_, auth, origin, name: str, model: str):
    """create_agent exactly as the tool runs it."""
    source_model, backend = _create_model(model)
    spec = _build_create_spec(
        name=name,
        model=source_model,
        description=f"{name}, made from chat",
        system=f"You are {name}.",
        tools=None,
        mcp_servers=None,
        skill_repos=None,
    )
    return await _create_agent_impl(runtime_, auth, spec, str(origin.id), backend=backend)


def stateful_agents(transport, root_agent: dict, environment_of) -> dict[str, dict]:
    """Managed Agents as one stateful fake in front of the root's routes: created
    agents are listed, read and archived like the real API, and the deployment's
    default environment is found by its tag (as an agent-level read looks it up)."""
    created: dict[str, dict] = {}
    # The root's own environment: the one the deployment's default name finds.
    environment = environment_of.model_dump(mode="json")

    def create(call, match):
        payload = json.loads(call.content)
        if isinstance(payload.get("model"), str):
            # The API answers with the model as an object.
            payload["model"] = {"id": payload["model"], "speed": None}
        identity = f"agent_created_{len(created) + 1}"
        created[identity] = {
            **root_agent,
            **payload,
            "id": identity,
            "version": 1,
            "archived_at": None,
        }
        return httpx.Response(200, json=created[identity])

    def archive(call, match):
        created[match.group(1)]["archived_at"] = "2026-10-24T00:00:00Z"
        return httpx.Response(200, json=created[match.group(1)])

    def live():
        return [root_agent, *(a for a in created.values() if a["archived_at"] is None)]

    from mux.testing.drivers.anthropic.ma import (
        FakeMemoryStoreState,
        make_fake_memory_store_handler,
    )

    memory = make_fake_memory_store_handler(FakeMemoryStoreState())
    transport.router.routes[:0] = [
        ("POST", re.compile(r"/v1/agents/([^/]+)/archive"), archive),
        ("POST", re.compile(r"/v1/agents$"), create),
        (
            "GET",
            re.compile(r"/v1/agents/(agent_created_[^/?]+)"),
            lambda call, match: httpx.Response(200, json=created[match.group(1)]),
        ),
        ("GET", re.compile(r"/v1/agents"), lambda call, match: list_response(live())),
        ("POST", re.compile(r"/v1/memory_stores"), lambda call, match: memory(call)),
        (
            "GET",
            re.compile(rf"/v1/environments/{re.escape(environment['id'])}"),
            lambda call, match: httpx.Response(200, json=environment),
        ),
        (
            "GET",
            re.compile(r"/v1/environments$"),
            lambda call, match: list_response([environment]),
        ),
    ]
    return created


def member_of(deps, origin, prepared):
    """The member asking in that chat, with the chat turn's verified identity."""
    tenant_id = prepared.admission.grant.tenant_id
    auth = replace(
        AUTH,
        tenant_id=tenant_id,
        account_id=origin.account_id,
        role=Role.USER,
        is_admin=False,
        chat_agent_id=derive_agent_uuid(
            tenant_id=tenant_id, ma_agent_id=prepared.admission.agent.id
        ),
    )
    return (
        replace(
            runtime(),
            session_factory=deps.sessionmaker,
            client=deps.anthropic,
            # As deployed: the default agent and its named environment.
            deployment_default=DeploymentDefault(
                agent_name=prepared.admission.agent.name,
                environment_name=(prepared.admission.environment.metadata or {})["daimon_name"],
            ),
        ),
        auth,
    )


async def test_j4_a_flash_agent_and_a_luna_agent_made_from_chat_run_as_a_fleet(
    j10_root, monkeypatch
):
    deps, prepared, *_ = j10_root
    transport = j10_root[7]
    made: dict[str, object] = {}

    async def save(j10_root, origin):
        stateful_agents(
            transport,
            prepared.admission.agent.model_dump(mode="json"),
            prepared.admission.environment,
        )
        runtime_, auth = member_of(deps, origin, prepared)
        for name, model in ASKED.items():
            made[name] = await create_from_chat(runtime_, auth, origin, name, model)

    replies, settled, collected, *_ = await _run_named_fleet(
        j10_root, monkeypatch, names=tuple(ASKED), save=save
    )
    # Each was created on its own provider in the create call, and says so.
    flash, luna = made["flash-agent"], made["luna-agent"]
    assert flash.model == "gemini-3.8-flash" and luna.model == "gpt-6-luna"
    assert "flash-agent runs on Gemini 3.8 Flash" in (flash.applies or "")
    assert "luna-agent runs on GPT-6 Luna" in (luna.applies or "")
    # Each worker ran on that provider and model: the Claude record its definition is
    # saved with is never the model a turn runs on.
    assert [reply["status"] for reply in replies] == ["pending"] * 2
    assert [(row.role, row.backend, row.model, row.status) for row in settled] == [
        ("flash-agent", "gemini", "gemini-3.8-flash", "succeeded"),
        ("luna-agent", "openai", "gpt-6-luna", "succeeded"),
    ]
    assert collected["status"] == "ready" and len(collected["results"]) == 2


async def test_a_create_whose_provider_start_fails_leaves_no_live_agent(j10_root, monkeypatch):
    """The record exists before its backend is set: a refusal there archives it, so no
    agent is left on the Claude record it was only saved with."""
    from daimon.core import channel_backend
    from daimon.core.turn_origin import turn_origin

    deps, prepared, _, _, seed, *_ = j10_root
    transport = j10_root[7]
    created = stateful_agents(
        transport,
        prepared.admission.agent.model_dump(mode="json"),
        prepared.admission.environment,
    )

    async def refused(self, *args, **kwargs):
        raise channel_backend.AgentBackendRefused("stale_revision")

    monkeypatch.setattr(channel_backend.AgentBackendService, "set_backend", refused)
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id=seed.parent_channel_id,
        thread_id=seed.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        runtime_, auth = member_of(deps, origin, prepared)
        with pytest.raises(ToolError, match="Nothing was created"):
            await create_from_chat(runtime_, auth, origin, "flash-agent", "flash")
    assert created and all(agent["archived_at"] is not None for agent in created.values())


async def test_a_provider_this_deployment_has_off_is_refused_before_anything_is_made(
    j10_root,
):
    """The switch's gates, before a record exists: with provider choice off, only Claude
    models can be created, and nothing is made."""
    from daimon.core.config import TurnSettings
    from daimon.core.turn_origin import turn_origin

    deps, prepared, _, _, seed, *_ = j10_root
    transport = j10_root[7]
    created = stateful_agents(
        transport,
        prepared.admission.agent.model_dump(mode="json"),
        prepared.admission.environment,
    )
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id=seed.parent_channel_id,
        thread_id=seed.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        runtime_, auth = member_of(deps, origin, prepared)
        off = replace(
            runtime_,
            settings=runtime_.settings.model_copy(
                update={"turn": TurnSettings(path="mux", channel_backends=False)}
            ),
        )
        with pytest.raises(ToolError, match="Nothing was created"):
            await create_from_chat(off, auth, origin, "flash-agent", "flash")
    assert created == {}


async def test_a_member_cannot_create_on_a_provider_this_server_turned_off(j10_root):
    """The switch's own policy gate: with Gemini off for the tenant, a member's Flash
    agent is refused, and the record made for it doesn't stay live."""
    from daimon.core.stores.access_policy import load_access_policy, set_access_policy
    from daimon.core.turn_origin import turn_origin

    deps, prepared, _, _, seed, *_ = j10_root
    transport = j10_root[7]
    tenant_id = prepared.admission.grant.tenant_id
    async with deps.sessionmaker.begin() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        await set_access_policy(
            session,
            tenant_id=tenant_id,
            policy=policy.model_copy(
                update={"enabled_backends": ("anthropic", "openai"), "backends_explicit": True}
            ),
        )
    created = stateful_agents(
        transport,
        prepared.admission.agent.model_dump(mode="json"),
        prepared.admission.environment,
    )
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id=seed.parent_channel_id,
        thread_id=seed.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        runtime_, auth = member_of(deps, origin, prepared)
        with pytest.raises(ToolError, match="Nothing was created"):
            await create_from_chat(runtime_, auth, origin, "flash-agent", "flash")
    assert all(agent["archived_at"] is not None for agent in created.values())


def test_create_names_models_on_any_provider_and_never_says_claude_first():
    """Natural names reach their own provider; the old 'create on Claude first' advice
    is gone from the tool's guidance."""
    from daimon.adapters.mcp.tools.agents import register_agent_tools
    from fastmcp import FastMCP

    # Cheap models only (RULES.md): the resolution is the same for every model.
    assert _create_model("flash")[1] == ("gemini", "gemini-3.8-flash")
    assert _create_model("gemini-3.8-flash")[1] == ("gemini", "gemini-3.8-flash")
    assert _create_model("luna")[1] == ("openai", "gpt-6-luna")
    assert _create_model("claude-haiku-5-5") == ("claude-haiku-5-5", None)

    mcp = FastMCP("guard")
    register_agent_tools(mcp, runtime())
    import asyncio

    tool = asyncio.run(mcp.get_tool("create_agent"))
    assert "never create it on Claude first" in (tool.description or "")
