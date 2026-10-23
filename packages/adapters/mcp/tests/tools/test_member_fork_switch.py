"""A member copies the agent answering their turn, owns the copy, and switches it.

TARGET-V2 J10 policy: any member may fork the agent they are talking to (within its
default caps); the copy is theirs, so they may switch its provider. The agent's MCP
tools are called as its model would call them; the provider edge is a fake.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import McpSettings, Settings, TurnSettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_thread_session,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from mux.testing.drivers.anthropic.ma import MARouter, list_response
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from sqlalchemy.ext.asyncio import async_sessionmaker

TOOLSET = [
    {
        "type": "mcp_toolset",
        "mcp_server_name": "daimon-mcp",
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        # As the deployed agent has it: a provider switch always asks first (the card).
        "configs": [
            {
                "name": "request_agent_backend_switch",
                "enabled": True,
                "permission_policy": {"type": "always_ask"},
            }
        ],
    }
]
MCP_SERVERS = [{"name": "daimon-mcp", "type": "url", "url": "https://mcp.invalid"}]


@dataclass
class FakeMA:
    """Managed Agents as one stateful fake: agents are listed, read and created."""

    tenant_id: uuid.UUID
    agents: dict[str, dict] = field(default_factory=dict)
    created: list[dict] = field(default_factory=list)
    sessions: dict[str, dict] = field(default_factory=dict)

    def add(self, agent) -> None:
        self.agents[agent.id] = agent.model_dump(mode="json")

    def router(self, environment) -> MARouter:
        router = MARouter()
        router.add(
            "GET", r"/v1/agents", lambda call, match: list_response(list(self.agents.values()))
        )
        router.add("POST", r"/v1/agents", self._create)
        router.add("GET", r"/v1/agents/([^/]+)", self._get)
        router.add(
            "GET",
            rf"/v1/environments/{re.escape(environment.id)}",
            lambda call, match: httpx.Response(200, json=environment.model_dump(mode="json")),
        )
        router.add(
            "GET",
            r"/v1/environments",
            lambda call, match: list_response([environment.model_dump(mode="json")]),
        )
        router.add(
            "GET",
            r"/v1/sessions/([^/]+)",
            lambda call, match: httpx.Response(200, json=self.sessions[match.group(1)]),
        )
        return router

    def _get(self, call, match):
        raw = self.agents.get(match.group(1))
        if raw is None:
            return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error"}})
        return httpx.Response(200, json=raw)

    def _create(self, call, match):
        """The created agent answers with what was asked, like the real API."""
        payload = json.loads(call.content)
        self.created.append(payload)
        base = next(iter(self.agents.values()))
        identity = f"agent_copy_{len(self.created)}"
        self.agents[identity] = {
            **base,
            **payload,
            "id": identity,
            "version": 1,
            "archived_at": None,
        }
        return httpx.Response(200, json=self.agents[identity])


@dataclass
class Chat:
    runtime: McpRuntime
    auth: AuthIdentity
    origin: object
    ma: FakeMA
    trace: ScriptedTransport


@pytest.fixture(params=("member", "admin"))
async def fresh_chat(request, db_preflight, db_nullpool_engine, monkeypatch):
    """The built-in Daimon answering a person's first message in a fresh public channel."""
    from daimon.core.provider_keys import DEFAULT_KEY_ENV

    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    admin = request.param == "admin"
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, tenant=tenant, account=account, platform="discord", external_id="123"
        )
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("10"))
        await make_thread_session(
            session,
            tenant=tenant,
            account=account,
            thread_id="thread",
            channel_id="fresh-channel",
            ma_session_id="sess_daimon",
            ma_agent_id="agent_daimon",
        )
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id="fresh-channel",
            thread_id="thread",
            responder_ma_agent_id="agent_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN if admin else Role.USER,
            sender_foreign=False,
            shared_channel=False,
            now=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    ma = FakeMA(tenant.id)
    daimon = ma_agent(
        id="agent_daimon",
        tenant_id=str(tenant.id),
        name="Daimon",
        tools=TOOLSET,
        mcp_servers=MCP_SERVERS,
    )
    ma.add(daimon)
    ma.sessions["sess_daimon"] = ma_session(
        id="sess_daimon",
        agent=daimon,
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_account": str(account.id),
            "daimon_channel": "fresh-channel",
            "daimon_thread": "thread",
        },
    ).model_dump(mode="json")
    environment = ma_environment(tenant_id=tenant.id)
    environment.metadata["daimon_name"] = "default"
    trace = ScriptedTransport(router=ma.router(environment))
    async with trace.client() as client:
        settings = Settings(
            database={"url": "postgresql+asyncpg://fixture:fixture@localhost/fixture"},
            anthropic={"api_key": "test-fixture-key"},
            turn=TurnSettings(path="mux", channel_backends=True, agent_backends=True),
            mcp=McpSettings(public_url="https://mcp.invalid"),
        )
        runtime = McpRuntime(
            sessions,
            client,
            settings,
            DeploymentDefault(agent_name="Daimon", environment_name="default"),
        )
        auth = AuthIdentity(
            tenant_id=tenant.id,
            account_id=account.id,
            role=Role.ADMIN if admin else Role.USER,
            is_admin=admin,
            platform="discord",
            platform_user_id="123",
            chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_daimon"),
        )
        yield Chat(runtime, auth, origin, ma, trace)


async def test_a_member_copies_the_answering_agent_and_switches_their_copy(fresh_chat):
    from daimon.core.channel_backend import AgentBackendService, load_backend_agent
    from daimon.core.stores import agent_owners

    chat = fresh_chat
    # "Make a copy of yourself": always a NEW copy, owned by whoever asked.
    copy = await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
    )
    assert copy.name == "Daimon-2" and len(chat.ma.created) == 1
    copy_id = derive_agent_uuid(tenant_id=chat.auth.tenant_id, ma_agent_id=copy.id)
    async with chat.runtime.session_factory() as session:
        owner = await agent_owners.owner_of(session, chat.auth.tenant_id, copy_id)
    assert owner == chat.auth.account_id
    # Switch the copy to GPT: preview, then the confirmed call (what the card runs).
    preview = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend="openai",
        origin_context_id=str(chat.origin.id),
    )
    assert preview.status == "preview"
    changed = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend="openai",
        origin_context_id=str(chat.origin.id),
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert changed.status == "changed"
    service = AgentBackendService(chat.runtime.session_factory, enabled=True)
    switched = await load_backend_agent(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        agent_name=copy.name,
    )
    assert (await service.current(switched)).backend == "openai"
    # The server's default agent is untouched, for everyone.
    daimon = await load_backend_agent(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        agent_name="Daimon",
    )
    current = await service.current(daimon)
    assert current is None or current.backend in (None, "anthropic")
