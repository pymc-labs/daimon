"""DM routines through real admission, session assembly, prepared turns, the
unattended tool gate and the Discord owner-DM poster, against fake Managed Agents
and Discord."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.sessions.beta_managed_agents_agent_mcp_tool_use_event import (
    BetaManagedAgentsAgentMCPToolUseEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_requires_action import (
    BetaManagedAgentsSessionRequiresAction,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from cryptography.fernet import Fernet
from daimon.adapters.discord.routine_delivery import make_discord_routine_poster
from daimon.adapters.scheduler.main import _build_fire, _CapsAdapter
from daimon.core import direct_messages
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import DirectMessagePolicy, RecruitmentSettings, Settings
from daimon.core.direct_messages import (
    admit_cold_dm,
    reply_to_dm_result,
    start_cold_dm,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.mcp_vault import add_external_mcp_credential
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.mux_backend import resource_scope
from daimon.core.routine_delivery import poll_deliveries_once
from daimon.core.scheduler import RoutineDispatcher, run_one_tick
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.credential_requests import create_credential_request
from daimon.core.stores.direct_messages import (
    get_conversation,
    set_dm_agent_name,
    set_dm_enabled,
)
from daimon.core.stores.domain import Role, RoutineRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.mcp_oauth_flows import create_flow, mark_flow_completed
from daimon.core.stores.routines import create_routine, get_routine
from daimon.core.stores.tenant_ledger import get_channel_spend
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn import unattended
from daimon.core.turn.deps import build_turn_deps
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_ledger_entry, make_tenant, make_tenant_config
from daimon.testing.ma import (
    FakeMAState,
    MARouter,
    build_fake_anthropic,
    combine_handlers,
    make_fake_memory_store_handler,
    not_found_response,
    sse_response,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.turn_router import turn_events
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD = 700000101
OWNER_A, OWNER_B = "999", "888"
ROUTE = "555"
BUDGET = "456"
SLACK_TOOLSET = {
    "type": "mcp_toolset",
    "mcp_server_name": "slack",
    "configs": [],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}
SLACK_SERVER = {"type": "url", "name": "slack", "url": "https://slack.example/mcp"}

Script = Callable[["Upstream", str], AsyncIterator[bytes]]


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk


class Upstream:
    """Managed Agents, as far as a turn sees it: sessions, events and streams.

    Each opened stream takes the next queued script (`answer` when none is
    queued). A stream keeps "running" until its script ends, so a turn that
    stops reading leaves the upstream turn going until something interrupts it.
    """

    def __init__(self, agent: Any, environment: Any) -> None:
        self.state = FakeSessionsState(FakeMAState())
        self.state.ma.agents[agent.id] = agent.model_dump(mode="json")
        self.crypto_key = Fernet.generate_key()
        self.crypto = Fernet(self.crypto_key)
        self.vaults: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, dict[str, dict[str, Any]]] = {}
        self.encrypted_tokens: dict[str, bytes] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.dead: set[str] = set()
        self.kill_first_send = False
        self.interrupt_entered = asyncio.Event()
        self.interrupt_gate: asyncio.Event | None = None
        self.interrupt_error = False
        self.discovery_error = False
        self.create_pause_at: int | None = None
        self.create_entered = asyncio.Event()
        self.create_gate = asyncio.Event()
        self.creates = 0
        self.create_agents: list[Any] = []
        self.replacement_server_url: str | None = None
        self.file_listing_error = False
        self.file_listing_hook: Any = None
        self.memory_outage = False
        self.download_hook: Any = None
        self.pause_call = (
            "slack",
            "send_message",
            {"channel": "#general", "text": "my personal notes"},
        )
        self.scripts: list[Script] = []
        self.changed = asyncio.Condition()
        roster = MARouter()
        roster.add_agent_list(agent)
        roster.add_environment_list(environment)
        roster.add_environment(environment)
        fallback = combine_handlers(
            make_fake_sessions_handler(self.state),
            make_fake_memory_store_handler(),
            roster.dispatch,
        )

        async def dispatch(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.strip("/").split("/")
            if self.memory_outage and "memory_stores" in request.url.path:
                return httpx.Response(
                    503, json={"error": {"type": "api_error", "message": "mount offline"}}
                )
            if self.download_hook is not None and request.url.path.endswith("/content"):
                await self.download_hook()
            if len(parts) >= 2 and parts[1] == "vaults":
                return self.vault_request(request, parts)
            if (
                request.method == "GET"
                and request.url.path == "/v1/sessions"
                and self.discovery_error
            ):
                return httpx.Response(
                    503, json={"error": {"type": "api_error", "message": "offline"}}
                )
            if (
                request.method == "GET"
                and request.url.path.endswith("/files")
                and self.file_listing_hook is not None
            ):
                await self.file_listing_hook()
            if (
                request.method == "GET"
                and request.url.path.endswith("/files")
                and self.file_listing_error
            ):
                return httpx.Response(
                    503, json={"error": {"type": "api_error", "message": "offline"}}
                )
            if request.method == "POST" and request.url.path == "/v1/sessions":
                self.create_agents.append(json.loads(request.content)["agent"])
                response = fallback(request)
                self.creates += 1
                if self.creates == self.create_pause_at:
                    self.create_entered.set()
                    await self.create_gate.wait()
                return response
            if request.method == "GET" and request.url.path == f"/v1/agents/{agent.id}":
                return httpx.Response(200, json=self.state.ma.agents[agent.id])
            if request.method == "POST" and parts[-1] == "events" and parts[1] == "sessions":
                sid = parts[2]
                if self.kill_first_send and not self.dead:
                    self.dead.add(sid)
                    if self.replacement_server_url is not None:
                        for server in self.state.ma.agents[agent.id]["mcp_servers"]:
                            if server["name"] == "slack":
                                server["url"] = self.replacement_server_url
                if sid in self.dead:
                    return not_found_response(f"no such session: {sid}")
                body = json.loads(request.content)
                kinds = {event["type"] for event in body["events"]}
                if "user.interrupt" in kinds:
                    self.interrupt_entered.set()
                    if self.interrupt_gate is not None:
                        await self.interrupt_gate.wait()
                    if self.interrupt_error:
                        return httpx.Response(
                            503, json={"error": {"type": "api_error", "message": "offline"}}
                        )
                self.state.sessions[sid].status = "idle" if "user.interrupt" in kinds else "running"
                async with self.changed:
                    self.sent.extend((sid, event) for event in body["events"])
                    self.changed.notify_all()
            if request.method == "POST" and len(parts) == 3 and parts[1] == "sessions":
                self.updates.append((parts[2], json.loads(request.content)))
            if request.method == "GET" and request.url.path.endswith("/events/stream"):
                script = self.scripts.pop(0) if self.scripts else answer("private answer")
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_Stream(script(self, parts[2])),
                )
            return fallback(request)

        self.client = build_fake_anthropic(dispatch)

    def vault_request(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        now = "2026-10-19T00:00:00Z"
        if len(parts) == 2:
            if request.method == "GET":
                return httpx.Response(
                    200, json={"data": list(self.vaults.values()), "has_more": False}
                )
            body = json.loads(request.content)
            vid = f"vlt_{len(self.vaults)}"
            vault = {
                "id": vid,
                "type": "vault",
                "created_at": now,
                "archived_at": None,
                "metadata": body.get("metadata"),
                "display_name": body["display_name"],
            }
            self.vaults[vid] = vault
            self.credentials[vid] = {}
            return httpx.Response(200, json=vault)
        vid = parts[2]
        if parts[-1] == "credentials" and request.method == "GET":
            return httpx.Response(
                200, json={"data": list(self.credentials[vid].values()), "has_more": False}
            )
        if request.method == "DELETE":
            self.credentials[vid].pop(parts[-1])
            return httpx.Response(200, json={"id": parts[-1], "type": "vault_credential_deleted"})
        body = json.loads(request.content)
        cid = parts[-1] if len(parts) == 5 else f"vcrd_{uuid.uuid4().hex}"
        auth = dict(body["auth"])
        token = auth.pop("token", None)
        if token is not None:
            self.encrypted_tokens[cid] = self.crypto.encrypt(token.encode())
        credential = {
            "id": cid,
            "type": "vault_credential",
            "vault_id": vid,
            "metadata": body.get("metadata") or {},
            "auth": auth,
            "created_at": now,
            "updated_at": now,
        }
        self.credentials[vid][cid] = credential
        return httpx.Response(200, json=credential)

    def events(self, sid: str, kind: str) -> list[dict[str, Any]]:
        return [event for session, event in self.sent if session == sid and event["type"] == kind]

    async def wait(self, predicate: Callable[[], bool], timeout: float = 20) -> None:
        async with self.changed:
            await asyncio.wait_for(self.changed.wait_for(predicate), timeout)


def answer(text: str) -> Script:
    async def script(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
        events = turn_events(agent_text=text, event_id_suffix=f"_{sid}")
        upstream.state.events.setdefault(sid, []).extend(events)
        yield sse_response(events).content

    return script


async def hang(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
    """Accept the message and keep running until interrupted."""
    await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
    await upstream.wait(lambda: bool(upstream.events(sid, "user.interrupt")), timeout=60)
    yield sse_response(
        [
            event
            for event in turn_events(agent_text="", event_id_suffix=f"_stop_{sid}")
            if event["type"] == "session.status_idle"
        ]
    ).content


async def lost(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
    """The connection drops while the upstream turn carries on."""
    await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
    if upstream.events(sid, "user.interrupt"):
        yield sse_response(
            [
                event
                for event in turn_events(agent_text="", event_id_suffix=f"_stop_{sid}")
                if event["type"] == "session.status_idle"
            ]
        ).content
    else:
        raise httpx.RemoteProtocolError("peer closed connection")


async def pause_on_public_send(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
    """A third-party send that pauses, then whatever the confirmation decided."""
    await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
    now = datetime.now(UTC)
    pause = [
        BetaManagedAgentsAgentMCPToolUseEvent(
            id=f"tu_send_{sid}",
            type="agent.mcp_tool_use",
            name=upstream.pause_call[1],
            input=upstream.pause_call[2],
            mcp_server_name=upstream.pause_call[0],
            processed_at=now,
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id=f"evt_pause_{sid}",
            type="session.status_idle",
            stop_reason=BetaManagedAgentsSessionRequiresAction(
                type="requires_action", event_ids=[f"tu_send_{sid}"]
            ),
            processed_at=now,
        ),
    ]
    yield sse_response([event.model_dump(mode="json") for event in pause]).content
    await upstream.wait(lambda: bool(upstream.events(sid, "user.tool_confirmation")))
    events = turn_events(agent_text="finished", event_id_suffix=f"_{sid}")
    upstream.state.events.setdefault(sid, []).extend(events)
    yield sse_response(events).content


class Harness:
    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant: Any, upstream: Upstream):
        self.factory = factory
        self.tenant = tenant
        self.upstream = upstream
        self.members: dict[str, bool] = {OWNER_A: True, OWNER_B: True}

        async def membership(_tenant: object, _guild: str, user_id: str) -> Membership:
            return Membership(is_member=self.members.get(user_id, False))

        self.resolver = CachedMembershipResolver(membership)
        self.gated: list[str] = []
        self.settings = Settings(
            _env_file=None,
            database={"url": factory.kw["bind"].url.render_as_string(hide_password=False)},
            mcp={"public_url": "https://mcp.example/mcp", "jwt_secret": "t" * 32},
            crypto={"keys": [upstream.crypto_key.decode()]},
            turn={"path": "mux"},
        )
        self.deps = replace(
            build_turn_deps(
                self.settings,
                upstream.client,
                factory,
                deployment_default=DeploymentDefault(),
                resolver_cache=new_resolver_cache(),
                billing_config=None,
            ),
            membership_resolver=self.resolver,
        )
        self.dispatcher = RoutineDispatcher(2)

    def with_safety(self, policy: ToolSafetyPolicy) -> None:
        self.deps = replace(self.deps, tool_safety=policy)
        self.settings = self.settings.model_copy(update={"tool_safety": policy})

    async def routine(self, owner: str = OWNER_A, **overrides: Any) -> Any:
        row = await _routine_row(self, owner=owner)
        await self.fire(row)
        after = await self.reload(row)
        assert after is not None
        if after.last_error:
            raise RuntimeError(after.last_error)
        session_id = next(reversed(self.upstream.state.sessions))
        return SimpleNamespace(
            text=after.last_result_tail, session_id=session_id, routine_id=row.id
        )

    async def fire(self, row: RoutineRow) -> None:
        fire = await _build_fire(
            client=self.upstream.client,
            sm=self.factory,
            settings=self.settings,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            membership_resolver=self.resolver,
            dm_turn_deps=self.deps,
        )
        await run_one_tick(
            now=datetime.now(UTC),
            sm=self.factory,
            caps=_CapsAdapter(self.factory, billing_config=None),
            fire=fire,
            max_age=timedelta(minutes=5),
            max_concurrent_fires=2,
            dispatch_timeout_s=120,
            dispatcher=self.dispatcher,
            wait_for_completion=True,
        )

    async def reload(self, row: RoutineRow) -> RoutineRow | None:
        async with self.factory() as db:
            return await get_routine(db, row.id, tenant_id=row.tenant_id)

    async def deliver(
        self, *, fetch_channel: Callable[..., Any], open_dm: Callable[..., Any], files: bool = True
    ) -> None:
        """One Discord delivery poll through the real owner-DM poster."""
        poster = make_discord_routine_poster(
            self.factory,
            recruitment=RecruitmentSettings(),
            fetch_channel=fetch_channel,
            open_dm=open_dm,
            dm_policy=lambda _: DirectMessagePolicy(),
            membership_resolver=self.resolver,
            output_client=self.upstream.client if files else None,
        )
        await poll_deliveries_once(
            self.factory, platform="discord", post=poster, now=datetime.now(UTC)
        )

    async def live_dm(self, text: str, message_id: str, *, start: bool = True) -> str:
        if start:
            admission = await admit_cold_dm(
                self.deps,
                tenant_id=self.tenant.id,
                external_user_id=OWNER_A,
                channel_id=ROUTE,
                role=Role.USER,
                role_ids=(),
                verified_membership=True,
            )
            await start_cold_dm(
                self.deps,
                admission,
                tenant_id=self.tenant.id,
                workspace_id=self.tenant.external_id,
                route_key=ROUTE,
                external_user_id=OWNER_A,
            )
        conversation = await self.conversation()
        assert conversation is not None
        reply = await reply_to_dm_result(
            self.deps,
            platform="discord",
            route_key=ROUTE,
            external_user_id=OWNER_A,
            message_id=message_id,
            expected_scope_id=conversation.scope_id,
            text=text,
            role=Role.USER,
            verified_membership=True,
        )
        assert reply is not None
        return reply.text

    async def conversation(self):
        async with self.factory() as session:
            return await get_conversation(
                session, platform="discord", route_key=ROUTE, external_user_id=OWNER_A
            )

    async def live_session_id(self) -> str:
        conversation = await self.conversation()
        assert conversation is not None
        async with self.factory() as session:
            mapping = await get_live_thread_session(
                session,
                tenant_id=self.tenant.id,
                platform="discord",
                thread_id=conversation.scope_id,
                account_id=conversation.account_id,
            )
        assert mapping is not None
        return mapping.ma_session_id

    async def manifest(self, sid: str) -> dict[str, Any]:
        """What a session runs with: memory, tools, servers, credentials."""
        session = self.upstream.state.sessions[sid]
        volatile = {"id", "created_at", "updated_at"}
        from daimon.core.turn.unattended_capabilities import constrain_capabilities

        constrained = await constrain_capabilities(
            session,
            self.deps.tool_safety,
            public_url=self.deps.public_url,
            session_factory=self.factory,
            tenant_id=self.tenant.id,
            allow_skills=True,
        )
        if session.metadata.get("daimon_private_dm", "").startswith("routine:"):
            assert [
                tool.model_dump(mode="json") for tool in session.agent.tools
            ] == constrained.send.tools
        tools = constrained.send.tools
        # Preview now isolates each native session's identity grant, while
        # personal OAuth credentials remain in the owner's shared agent vault.
        chat_session = session.metadata["daimon_chat_session_id"]
        servers = [dict(server) for server in constrained.send.mcp_servers]
        for server in servers:
            if server["name"] == "daimon-mcp":
                assert server["url"] == f"https://mcp.example/mcp?daimon_session={chat_session}"
                server["url"] = "https://mcp.example/mcp"
        vaults = []
        for vid in session.vault_ids:
            grants = [
                credential
                for credential in self.upstream.credentials[vid].values()
                if credential["metadata"].get("daimon_chat_session")
            ]
            if grants:
                assert len(grants) == 1
                assert grants[0]["metadata"]["daimon_chat_session"] == chat_session
                vaults.append("session identity")
            else:
                vaults.append(vid)
        assert vaults.count("session identity") == 1
        return {
            "resources": sorted(
                (
                    {k: v for k, v in r.model_dump(mode="json").items() if k not in volatile}
                    for r in self.upstream.state.resources.get(sid, [])
                ),
                key=json.dumps,
            ),
            "tools": tools,
            "mcp_servers": servers,
            "vault_ids": sorted(vaults),
            "agent": session.agent.id,
            "environment": session.environment_id,
        }

    def memory_stores(self, sid: str) -> dict[str, str]:
        return {
            resource.memory_store_id: getattr(resource, "access", "")
            for resource in self.upstream.state.resources.get(sid, [])
            if resource.type == "memory_store"
        }


@pytest.fixture
async def harness(
    db_engine: Any, db_schema: str, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Harness]:
    execution_engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False),
        db_schema,
        pool_size=6,
        max_overflow=0,
        pool_timeout=10,
    )
    db_session_factory = async_sessionmaker(execution_engine, expire_on_commit=False)
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id=str(GUILD))
        await make_tenant_config(
            session, tenant=tenant, agent_name="daimon", environment_name="default"
        )
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("100"))
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="daimon")
    agent = ma_agent(
        id="ag_daimon",
        name="daimon",
        tenant_id=tenant.id,
        tools=[
            SLACK_TOOLSET,
            *[
                {**SLACK_TOOLSET, "mcp_server_name": name}
                for name in ("private_a", "private_b", "daimon-mcp")
            ],
        ],
        mcp_servers=[
            SLACK_SERVER,
            {"type": "url", "name": "private_a", "url": "https://a.example/mcp"},
            {"type": "url", "name": "private_b", "url": "https://b.example/mcp"},
            {"type": "url", "name": "daimon-mcp", "url": "https://mcp.example/mcp"},
        ],
    )
    environment = ma_environment(id="env_default", name="default", tenant_id=tenant.id)
    upstream = Upstream(agent, environment)
    built = Harness(db_session_factory, tenant, upstream)
    built.accounts = {}
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    for owner, url in ((OWNER_A, "https://a.example/mcp"), (OWNER_B, "https://b.example/mcp")):
        async with db_session_factory.begin() as db:
            principal = await get_or_create_platform_principal(
                db, tenant_id=tenant.id, platform="discord", external_id=owner
            )
            built.accounts[owner] = principal.account_id
            state = f"grant_{owner}_{uuid.uuid4().hex}"
            await create_credential_request(
                db,
                token=state,
                kind="mcp",
                tenant_id=tenant.id,
                account_id=principal.account_id,
                agent_id=agent_uuid,
                target=f"private_{owner}",
                mcp_server_url=url,
                requester_platform_user_id=owner,
                channel_id=ROUTE,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                idempotency_key=uuid.uuid4(),
                target_ma_agent_id=agent.id,
                target_name=agent.name,
                requested_work=None,
            )
            await create_flow(
                db,
                state=state,
                request_token=state,
                tenant_id=tenant.id,
                account_id=principal.account_id,
                agent_id=agent_uuid,
                server_name=f"private_{owner}",
                mcp_server_url=url,
                redirect_uri="https://mcp.example/oauth",
                code_verifier="verifier",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await mark_flow_completed(db, state=state, now=datetime.now(UTC))
        await add_external_mcp_credential(
            upstream.client,
            account_id=principal.account_id,
            agent_id=agent_uuid,
            mcp_server_url=url,
            token=f"personal-secret-{owner}",
            jwt_secret=b"t" * 32,
            public_url="https://mcp.example/mcp",
            now=datetime.now(UTC),
            session_factory=db_session_factory,
            scope=resource_scope(
                tenant_id=str(tenant.id),
                account_id=str(principal.account_id),
                authorization_id="test-personal-connect",
            ),
        )
    real_gate = unattended.gate_session_snapshot

    async def gate(client: Any, session: Any, *args: Any, **kwargs: Any) -> None:
        built.gated.append(session.id)
        await real_gate(client, session, *args, **kwargs)

    monkeypatch.setattr(unattended, "gate_session_snapshot", gate)
    try:
        yield built
    finally:
        await built.dispatcher.close()
        await upstream.client.close()
        await execution_engine.dispose()


@pytest.mark.parametrize("read_only", [False, True], ids=["writable", "dm_memory_read_only"])
async def test_routine_session_is_the_owners_live_dm_session(harness: Harness, read_only: bool):
    if read_only:
        async with harness.factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=harness.tenant.id,
                policy=TenantAccessPolicy(dm_memory_read_only=True),
            )
    assert await harness.live_dm("hello", "m1") == "private answer"
    live = await harness.live_session_id()
    routine_a = await harness.routine(OWNER_A)
    routine_b = await harness.routine(OWNER_B)

    assert routine_a.text == "private answer"
    assert await harness.manifest(routine_a.session_id) == await harness.manifest(live), (
        "same memory, permitted MCP tools, servers and owner credentials as the live DM"
    )
    stores_a = harness.memory_stores(routine_a.session_id)
    stores_b = harness.memory_stores(routine_b.session_id)
    assert len(stores_a) == 2, "shared and personal memory are both mounted"
    assert len(set(stores_a) & set(stores_b)) == 1, "only the shared store is common"
    assert set(stores_a.values()) == {"read_only" if read_only else "read_write"}


@pytest.mark.parametrize(
    ("policy", "allowed"),
    [
        (ToolSafetyPolicy(enabled=True), False),
        (ToolSafetyPolicy(enabled=True, unattended_writes=("slack/send_message",)), True),
        (ToolSafetyPolicy(enabled=False), True),
    ],
    ids=["safety_on_refused", "allow_listed", "safety_off"],
)
async def test_a_paused_public_send_is_decided_unattended(
    harness: Harness, policy: ToolSafetyPolicy, allowed: bool
):
    harness.with_safety(policy)
    harness.upstream.scripts = [pause_on_public_send]
    reply = await harness.routine()

    (decision,) = harness.upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == ("allow" if allowed else "deny")
    assert reply.text == "finished", "the run goes on and reports back; it never waits"
    toolset = next(
        tool
        for tool in harness.upstream.state.sessions[reply.session_id].agent.tools
        if tool.mcp_server_name == "slack"
    )
    assert toolset.model_dump(mode="json")["default_config"]["permission_policy"] == {
        "type": "always_ask"
    }, "the session runs under the current policy's gate"
    assert harness.gated == [reply.session_id] * 2, (
        "gated before message and both sides of confirmation decision"
    )


async def _routine_row(harness: Harness, *, owner: str = OWNER_A) -> RoutineRow:
    async with harness.factory.begin() as session:
        return await create_routine(
            session,
            tenant_id=harness.tenant.id,
            created_by_user_id=owner,
            agent_id="ag_daimon",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="what is on my plate?",
            destination_kind="dm",
            channel_id=BUDGET,
            source_provenance_known=True,
            next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
        )


async def _reclassify(db: AsyncSession, row: RoutineRow, change: str, **back: Any) -> None:
    """Move `row` to agent "b", or retarget it to the budget channel and, for "aba", back."""
    from daimon.core.stores.routines import update_routine

    if change == "agent":
        await update_routine(
            db,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            agent_id="ag_b",
            agent_name="b",
        )
        return
    await update_routine(
        db,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=row.tenant_id,
        destination_kind="channel",
        destination_id=BUDGET,
        channel_id=BUDGET,
    )
    if change == "aba":
        await update_routine(
            db, row.id, edited_by=row.created_by_user_id, tenant_id=row.tenant_id, **back
        )


async def test_routine_answer_reaches_only_the_owners_dm_and_bills_its_budget(harness: Harness):
    row = await _routine_row(harness)
    await harness.fire(row)
    sent: list[tuple[int, int, str]] = []

    async def open_dm(guild_id: int, user_id: int) -> Any:
        class _Dm:
            async def send(self, *, content: str, allowed_mentions: object) -> None:
                sent.append((guild_id, user_id, content))

        return _Dm()

    async def no_channel(channel_id: int) -> object:
        raise AssertionError("a DM routine never posts to a channel")

    await harness.deliver(fetch_channel=no_channel, open_dm=open_dm, files=False)

    assert [(guild, user) for guild, user, _ in sent] == [(GUILD, int(OWNER_A))]
    assert "private answer" in sent[0][2]
    async with harness.factory() as session:
        spent = await get_channel_spend(
            session, tenant_id=harness.tenant.id, channel_id=BUDGET, since=None, until=None
        )
    assert spent > 0, "the run is billed to the routine's budget channel"


async def test_revoked_owner_gets_no_run_and_no_delivery(harness: Harness):
    row = await _routine_row(harness)
    harness.members[OWNER_A] = False
    await harness.fire(row)
    assert not harness.upstream.sent, "nothing reached the model"
    after = await harness.reload(row)
    assert after is not None and after.last_error and after.delivery_status is None


async def test_a_provider_native_agent_is_refused_before_any_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    real_admit = direct_messages.admit

    async def native_admit(*args: Any, **kwargs: Any) -> Any:
        admission = await real_admit(*args, **kwargs)
        revision = type("Revision", (), {"profile": "openai.responses"})()
        return replace(admission, backend_revision=revision, backend_revision_origin="agent")

    monkeypatch.setattr(direct_messages, "admit", native_admit)
    row = await _routine_row(harness)
    await harness.fire(row)
    assert not harness.upstream.state.sessions, "no session was created"
    after = await harness.reload(row)
    assert after is not None
    assert after.last_error == "DM routines need a Managed Agents agent"
    assert after.delivery_status is None
    with pytest.raises(RuntimeError, match="Managed Agents"):
        await harness.routine()


async def test_attached_owner_credentials_are_nonempty_distinct_and_scope_checked(harness: Harness):
    import jwt
    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier, token_row_refusal
    from daimon.adapters.mcp.runtime import McpRuntime
    from daimon.adapters.mcp.tools.routines import _get_routine_impl
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.stores.mcp_tokens import get_mcp_token
    from fastmcp.exceptions import ToolError

    a = await harness.routine(OWNER_A)
    b = await harness.routine(OWNER_B)
    sessions = harness.upstream.state.sessions
    vaults_a = sessions[a.session_id].vault_ids
    vaults_b = sessions[b.session_id].vault_ids
    assert len(vaults_a) == len(vaults_b) == 2
    assert set(vaults_a).isdisjoint(vaults_b)
    upstream = harness.upstream
    credential_a = next(
        c
        for vault in vaults_a
        for c in upstream.credentials[vault].values()
        if c["auth"].get("mcp_server_url") == "https://a.example/mcp"
    )
    credential_b = next(
        c
        for vault in vaults_b
        for c in upstream.credentials[vault].values()
        if c["auth"].get("mcp_server_url") == "https://b.example/mcp"
    )
    assert (
        upstream.crypto.decrypt(upstream.encrypted_tokens[credential_a["id"]])
        == f"personal-secret-{OWNER_A}".encode()
    )
    assert (
        upstream.crypto.decrypt(upstream.encrypted_tokens[credential_b["id"]])
        == f"personal-secret-{OWNER_B}".encode()
    )
    assert (
        upstream.encrypted_tokens[credential_a["id"]]
        != upstream.encrypted_tokens[credential_b["id"]]
    )
    servers_a = {s.name for s in sessions[a.session_id].agent.mcp_servers}
    servers_b = {s.name for s in sessions[b.session_id].agent.mcp_servers}
    assert "private_a" in servers_a and "private_b" not in servers_a
    assert "private_b" in servers_b and "private_a" not in servers_b
    agent_uuid = derive_agent_uuid(tenant_id=harness.tenant.id, ma_agent_id="ag_daimon")
    for owner, sid in ((OWNER_A, a.session_id), (OWNER_B, b.session_id)):
        chat = next(
            credential
            for vault in sessions[sid].vault_ids
            for credential in upstream.credentials[vault].values()
            if credential["metadata"].get("daimon_chat_session")
        )
        assert (
            chat["metadata"]["daimon_chat_session"]
            == sessions[sid].metadata["daimon_chat_session_id"]
        )
        token = upstream.crypto.decrypt(upstream.encrypted_tokens[chat["id"]]).decode()
        scope = resource_scope(
            tenant_id=str(harness.tenant.id),
            account_id=str(harness.accounts[owner]),
            authorization_id="test-chat",
        )
        verifier = DaimonJWTVerifier(secret=b"t" * 32, sessionmaker=harness.factory)
        access = await verifier.verify_token(token)
        assert access is not None and access.claims["sub"] == scope.account_id
        assert access.claims["chat_agent_id"] == str(agent_uuid)
        assert access.claims["chat_session_id"] == sessions[sid].metadata["daimon_chat_session_id"]
        assert access.claims["tenant_id"] == str(harness.tenant.id)
        auth = AuthIdentity(
            account_id=uuid.UUID(access.claims["sub"]),
            tenant_id=uuid.UUID(access.claims["tenant_id"]),
            role=Role(access.claims["role"]),
            platform=access.claims["platform"],
            external_id=access.claims["external_id"],
            platform_user_id=access.claims["platform_user_id"],
            chat_agent_id=uuid.UUID(access.claims["chat_agent_id"]),
            token_kind="agent",
            token_jti=uuid.UUID(chat["metadata"]["daimon_chat_jti"]),
        )
        runtime = McpRuntime(
            session_factory=harness.factory,
            client=upstream.client,
            settings=harness.settings,
            deployment_default=DeploymentDefault(),
        )
        mine, theirs = (a, b) if owner == OWNER_A else (b, a)
        own = await _get_routine_impl(runtime, auth, routine_id=mine.routine_id)
        assert own.created_by_user_id == owner and own.last_result_tail == "private answer"
        with pytest.raises(ToolError, match="routine not found"):
            await _get_routine_impl(runtime, auth, routine_id=theirs.routine_id)
        claims = jwt.decode(token, b"t" * 32, algorithms=["HS256"])
        other = OWNER_B if owner == OWNER_A else OWNER_A
        async with harness.factory() as db:
            identity = await get_account_with_tenant(db, account_id=harness.accounts[other])
            registry = await get_mcp_token(db, jti=uuid.UUID(claims["jti"]))
        assert identity is not None and registry is not None
        assert (
            token_row_refusal(registry, claims=claims, identity=identity, now=datetime.now(UTC))
            == "account_mismatch"
        )


@pytest.mark.parametrize("enabled", [False, True])
async def test_protected_final_action_is_refused_through_the_driver(
    harness: Harness, enabled: bool
):
    harness.with_safety(ToolSafetyPolicy(enabled=enabled))
    harness.upstream.pause_call = ("daimon-mcp", "microsoft_send", {"action": "send_mail"})
    harness.upstream.scripts = [pause_on_public_send]
    reply = await harness.routine()
    (decision,) = harness.upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == "deny"


async def test_endpoint_bound_rule_allows_the_unattended_pause(harness: Harness):
    from daimon.core.authz import Subject
    from daimon.core.stores.agent_tool_rules import set_rule

    harness.with_safety(ToolSafetyPolicy(enabled=True, agent_rules=True))
    async with harness.factory.begin() as db:
        await set_rule(
            db,
            tenant_id=harness.tenant.id,
            agent_id="ag_daimon",
            agent_name="daimon",
            tool_key="slack/send_message",
            server_url=SLACK_SERVER["url"],
            rule="allow",
            scope="chat_and_routines",
            set_by=OWNER_A,
            actor=Subject(is_admin=True),
        )
    harness.upstream.scripts = [pause_on_public_send]
    reply = await harness.routine()
    (decision,) = harness.upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == "allow"


@pytest.mark.parametrize("rule_endpoint,rule", [("original", "allow"), ("replacement", "deny")])
async def test_replacement_endpoint_cannot_reuse_predecessor_allow(
    harness: Harness, rule_endpoint: str, rule: str
):
    from daimon.core.authz import Subject
    from daimon.core.stores.agent_tool_rules import set_rule

    harness.with_safety(ToolSafetyPolicy(enabled=True, agent_rules=True))
    upstream = harness.upstream
    # Already gated custom agent: no hidden servers or tool overrides at creation.
    agent = upstream.state.ma.agents["ag_daimon"]
    agent["tools"] = [
        {
            **SLACK_TOOLSET,
            "default_config": {"enabled": True, "permission_policy": {"type": "always_ask"}},
        }
    ]
    agent["mcp_servers"] = [dict(SLACK_SERVER)]
    upstream.kill_first_send = True
    upstream.replacement_server_url = "https://replacement.example/mcp"
    async with harness.factory.begin() as db:
        await set_rule(
            db,
            tenant_id=harness.tenant.id,
            agent_id="ag_daimon",
            agent_name="daimon",
            tool_key="slack/send_message",
            server_url=(
                SLACK_SERVER["url"]
                if rule_endpoint == "original"
                else upstream.replacement_server_url
            ),
            rule=rule,
            scope="chat_and_routines",
            set_by=OWNER_A,
            actor=Subject(is_admin=True),
        )
    upstream.scripts = [pause_on_public_send, pause_on_public_send]
    row = await _routine_row(harness)
    await harness.fire(row)
    after = await harness.reload(row)
    assert after is not None and after.last_error is None
    assert after.delivery_payload is not None
    assert len(upstream.state.sessions) == 2
    assert upstream.create_agents == ["ag_daimon", "ag_daimon"]
    first, replacement = upstream.state.sessions.values()
    assert {s.name: s.url for s in first.agent.mcp_servers} == {"slack": SLACK_SERVER["url"]}
    assert {s.name: s.url for s in replacement.agent.mcp_servers} == {
        "slack": upstream.replacement_server_url,
    }
    (decision,) = upstream.events(replacement.id, "user.tool_confirmation")
    assert decision["result"] == "deny", "replacement endpoint cannot borrow old rules"


async def test_trusted_hint_is_read_over_the_real_catalogue_path(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    harness.with_safety(ToolSafetyPolicy(enabled=True, trusted_annotations=("slack",)))
    harness.upstream.pause_call = ("slack", "memory_flag_list", {})
    calls = []

    def catalogue(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SLACK_SERVER["url"]
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = (
            {
                "tools": [
                    {
                        "name": "memory_flag_list",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
            if body["method"] == "tools/list"
            else {}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real_init = httpx.AsyncClient.__init__

    def external_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("transport", httpx.MockTransport(catalogue))
        kwargs["trust_env"] = False
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", external_init)
    harness.upstream.scripts = [pause_on_public_send]
    reply = await harness.routine()
    (decision,) = harness.upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == "allow"
    assert calls == ["initialize", "notifications/initialized", "tools/list"]


async def test_mount_outage_matches_live_dm_without_another_owners_memory(harness: Harness):
    harness.upstream.memory_outage = True
    await harness.live_dm("hello without memory", "outage-m1")
    live = await harness.live_session_id()
    reply = await harness.routine()
    assert reply.text == "private answer"
    assert harness.memory_stores(live) == harness.memory_stores(reply.session_id) == {}
    assert await harness.manifest(live) == await harness.manifest(reply.session_id)


@pytest.mark.parametrize("change", ["retarget", "clear"])
@pytest.mark.parametrize("stage", ["run", "refusal", "collector"])
async def test_private_failure_after_destination_change_does_not_publish_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, change: str, stage: str
):
    from daimon.core.stores.routines import update_routine

    entered, release = asyncio.Event(), asyncio.Event()
    canary = "private-provider-filename-canary.txt"

    async def fail() -> None:
        entered.set()
        await release.wait()
        if stage == "refusal":
            raise direct_messages.DirectMessageAccessRevoked("private_refusal", message=canary)
        raise RuntimeError(canary)

    async def failing_run(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        entered.set()
        await release.wait()
        raise RuntimeError(canary)
        yield b""  # pragma: no cover -- make this a streaming provider response

    if stage == "collector":
        # Pause the real collector at the provider file-list transport boundary.
        harness.upstream.file_listing_hook = fail
    elif stage == "refusal":
        gate = unattended.gate_session_snapshot

        async def refused(*args: Any, **kwargs: Any) -> Any:
            await gate(*args, **kwargs)
            await fail()

        monkeypatch.setattr(unattended, "gate_session_snapshot", refused)
    else:
        harness.upstream.scripts = [failing_run]
    row = await _routine_row(harness)
    task = asyncio.create_task(harness.fire(row))
    await asyncio.wait_for(entered.wait(), 10)
    async with harness.factory.begin() as db:
        await update_routine(
            db,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            **(
                {"clear_destination": True}
                if change == "clear"
                else {
                    "destination_kind": "channel",
                    "destination_id": BUDGET,
                    "channel_id": BUDGET,
                }
            ),
        )
    release.set()
    await asyncio.wait_for(task, 20)
    after = await harness.reload(row)
    assert after is not None and after.last_error is None
    assert after.last_result_tail is None and after.delivery_payload is None
    from daimon.adapters.discord.routines_panel.subviews import build_last_output_container
    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.runtime import McpRuntime
    from daimon.adapters.mcp.tools.routines import _get_routine_impl, _list_routines_impl

    runtime = McpRuntime(
        session_factory=harness.factory,
        client=harness.upstream.client,
        settings=harness.settings,
        deployment_default=DeploymentDefault(),
    )
    admin = AuthIdentity(
        account_id=harness.accounts[OWNER_B],
        tenant_id=row.tenant_id,
        role=Role.ADMIN,
        platform="discord",
        external_id=str(GUILD),
        platform_user_id=OWNER_B,
        is_admin=True,
    )
    shared = await _get_routine_impl(runtime, admin, routine_id=row.id)
    listed = await _list_routines_impl(runtime, admin)
    assert shared.last_error is None
    assert next(r for r in listed if r.id == row.id).last_error is None
    container = build_last_output_container(shared)
    assert canary not in str(container.to_component_dict())
    assert "private_refusal" not in str(container.to_component_dict())


async def test_another_tenants_active_dm_is_untouched(harness: Harness):
    async with harness.factory.begin() as db:
        tenant = await make_tenant(db, workspace_id=str(GUILD + 1))
        await make_tenant_config(db, tenant=tenant, agent_name="daimon", environment_name="default")
        await make_ledger_entry(db, tenant=tenant, delta_usd=Decimal("100"))
        await set_dm_enabled(db, tenant_id=tenant.id, enabled=True)
        await set_dm_agent_name(db, tenant_id=tenant.id, agent_name="daimon")
    other_upstream = Upstream(
        ma_agent(id="ag_other", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_other", name="default", tenant_id=tenant.id),
    )
    other = Harness(harness.factory, tenant, other_upstream)
    try:
        await other.live_dm("other tenant private", "other-m1")
        before = await other.conversation()
        sid = await other.live_session_id()
        result = await harness.routine()
        assert await other.conversation() == before
        assert await other.live_session_id() == sid
        assert set(other.memory_stores(sid)).isdisjoint(harness.memory_stores(result.session_id))
        assert not other_upstream.events(sid, "user.interrupt")
    finally:
        await other.dispatcher.close()
        await other_upstream.client.close()


@pytest.mark.parametrize("change", ["revoke", "retarget", "agent"])
@pytest.mark.parametrize("stage", ["chunk", "file"])
async def test_owner_and_destination_are_rechecked_during_real_delivery(
    harness: Harness, change: str, stage: str
):
    from daimon.core.stores.routines import update_routine

    async def generated(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
        upstream.state.write_output(sid, "private.txt", b"personal output")
        async for data in answer("private answer " * (600 if stage == "chunk" else 1))(
            upstream, sid
        ):
            yield data

    harness.upstream.scripts = [generated]
    row = await _routine_row(harness)
    await harness.fire(row)
    sent, files = [], []

    async def changed() -> None:
        if change == "revoke":
            harness.members[OWNER_A] = False
        elif change == "agent":
            async with harness.factory.begin() as db:
                await update_routine(
                    db,
                    row.id,
                    edited_by=row.created_by_user_id,
                    tenant_id=row.tenant_id,
                    agent_id="ag_b",
                    agent_name="b",
                )
        else:
            async with harness.factory.begin() as db:
                await update_routine(
                    db,
                    row.id,
                    edited_by=row.created_by_user_id,
                    tenant_id=row.tenant_id,
                    destination_kind="channel",
                    destination_id=BUDGET,
                    channel_id=BUDGET,
                )

    async def open_dm(guild: int, owner: int) -> Any:
        class Destination:
            async def send(
                self,
                *,
                content: str | None = None,
                file: Any = None,
                allowed_mentions: object = None,
                **kwargs: Any,
            ) -> None:
                if file is not None:
                    files.append(file)
                else:
                    sent.append(content)
                    if stage == "chunk":
                        await changed()

        return Destination()

    async def no_channel(channel: int) -> Any:
        raise AssertionError("private output cannot be retargeted to a public channel")

    if stage == "file":
        harness.upstream.download_hook = changed
    await harness.deliver(fetch_channel=no_channel, open_dm=open_dm)
    assert len(sent) == 1
    assert not files, (
        "revocation/retarget during text or file delivery prevents later private output"
    )


async def test_busy_live_owner_skips_slot_without_touching_the_live_dm(harness: Harness):
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
        entered.set()
        await release.wait()
        async for chunk in answer("live answer")(upstream, sid):
            yield chunk

    harness.upstream.scripts = [waiting]
    live = asyncio.create_task(harness.live_dm("owner is here", "busy-live"))
    await asyncio.wait_for(entered.wait(), 15)
    before = await harness.conversation()
    row = await _routine_row(harness)
    await harness.fire(row)
    after = await harness.reload(row)
    assert after is not None and after.last_error == "owner_busy"
    assert after.delivery_payload is None and after.next_fire_at is not None
    assert len(harness.upstream.state.sessions) == 1
    release.set()
    assert await live == "live answer"
    current = await harness.conversation()
    assert current is not None and before is not None and current.scope_id == before.scope_id
    assert [t["text"] for t in current.history if t["role"] == "user"] == ["owner is here"]


@pytest.mark.parametrize("change", ["agent", "retarget", "aba"])
@pytest.mark.parametrize("stage", ["running", "pending"])
async def test_private_result_cannot_survive_reclassification(
    harness: Harness, change: str, stage: str
):
    entered, release = asyncio.Event(), asyncio.Event()
    secret = "old-agent-private-result"

    async def waiting(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
        entered.set()
        await release.wait()
        upstream.state.write_output(sid, "old-agent.txt", b"private old producer output")
        async for chunk in answer(secret)(upstream, sid):
            yield chunk

    harness.upstream.scripts = [waiting]
    row = await _routine_row(harness)
    task = asyncio.create_task(harness.fire(row))
    await asyncio.wait_for(entered.wait(), 15)
    if stage == "pending":
        release.set()
        await task
        pending = await harness.reload(row)
        assert (
            pending is not None and pending.delivery_payload and secret in pending.delivery_payload
        )
    async with harness.factory.begin() as db:
        await _reclassify(db, row, change, destination_kind="dm", destination_id=OWNER_A)
    release.set()
    await task
    sent = []

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        sent.append(args)
        raise AssertionError("invalidated content must never open any destination")

    await harness.deliver(fetch_channel=forbidden, open_dm=forbidden)
    after = await harness.reload(row)
    assert after is not None
    assert after.last_result_tail is None and after.last_error is None
    assert after.delivery_payload is None and after.delivery_status != "pending"
    assert not sent


@pytest.mark.parametrize("recovery", [False, True])
async def test_cached_agent_hints_cannot_authorize_actual_session_endpoint(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, recovery: bool
):
    from daimon.core import ma_resolver
    from daimon.core.authz import Subject
    from daimon.core.stores.agent_tool_rules import set_rule

    harness.with_safety(
        ToolSafetyPolicy(enabled=True, agent_rules=True, trusted_annotations=("slack",))
    )
    upstream = harness.upstream
    agent = upstream.state.ma.agents["ag_daimon"]
    agent["tools"] = [
        {
            **SLACK_TOOLSET,
            "default_config": {"enabled": True, "permission_policy": {"type": "always_ask"}},
        }
    ]
    agent["mcp_servers"] = [dict(SLACK_SERVER)]
    cached = BetaManagedAgentsAgent.model_validate(agent)
    actual_url = "https://actual.example/mcp"
    for server in agent["mcp_servers"]:
        server["url"] = actual_url
    # Exercise the resolver's stale cache seam, retaining real admission and assembly.
    real_cached = ma_resolver.retrieve_agent_cached

    async def stale(*args: Any, **kwargs: Any) -> Any:
        await real_cached(*args, **kwargs)
        return cached

    monkeypatch.setattr("daimon.core.turn.admission.retrieve_agent_cached", stale)
    calls = []

    def catalogue(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = (
            {
                "tools": [
                    {
                        "name": "memory_flag_list",
                        "inputSchema": {"type": "object"},
                        "annotations": {
                            "readOnlyHint": str(request.url) != actual_url,
                            "destructiveHint": str(request.url) == actual_url,
                        },
                    }
                ]
            }
            if body["method"] == "tools/list"
            else {}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    real_init = httpx.AsyncClient.__init__

    def external_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("transport", httpx.MockTransport(catalogue))
        kwargs["trust_env"] = False
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", external_init)
    async with harness.factory.begin() as db:
        await set_rule(
            db,
            tenant_id=harness.tenant.id,
            agent_id="ag_daimon",
            agent_name="daimon",
            tool_key="slack/memory_flag_list",
            server_url=SLACK_SERVER["url"],
            rule="allow",
            scope="chat_and_routines",
            set_by=OWNER_A,
            actor=Subject(is_admin=True),
        )
    upstream.kill_first_send = recovery
    upstream.pause_call = ("slack", "memory_flag_list", {})
    upstream.scripts = [pause_on_public_send] * (2 if recovery else 1)
    reply = await harness.routine()
    (decision,) = upstream.events(reply.session_id, "user.tool_confirmation")
    assert decision["result"] == "deny"
    assert calls and set(calls) == {actual_url}
    assert upstream.create_agents == ["ag_daimon"] * (2 if recovery else 1)


async def test_channel_routine_stays_headless_without_personal_memory(harness: Harness):
    async with harness.factory.begin() as db:
        row = await create_routine(
            db,
            tenant_id=harness.tenant.id,
            created_by_user_id=OWNER_A,
            agent_id="ag_daimon",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="channel work",
            destination_kind="channel",
            destination_id=BUDGET,
            channel_id=BUDGET,
            source_provenance_known=True,
            next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    await harness.fire(row)
    (sid,) = harness.upstream.state.sessions
    assert len(harness.memory_stores(sid)) == 1
    assert set(harness.memory_stores(sid).values()) == {"read_only"}
    assert harness.upstream.state.sessions[sid].metadata["daimon_private_dm"] == f"routine:{row.id}"


@pytest.mark.parametrize("gate", ["role", "admin_switch", "dm_disabled", "recipient", "source"])
async def test_current_authority_refuses_before_session_allocation(harness: Harness, gate: str):

    row = await _routine_row(harness)
    if gate == "role":
        policy = TenantAccessPolicy(invoker_role_ids=("123",))
    elif gate == "admin_switch":

        async def admin(*args: Any, **kwargs: Any) -> Membership:
            return Membership(is_member=True, is_admin=True)

        harness.resolver = CachedMembershipResolver(admin)
        policy = TenantAccessPolicy(invoker_user_ids=(OWNER_B,), invoker_admin_bypass=False)
    elif gate == "dm_disabled":
        async with harness.factory.begin() as db:
            await set_dm_enabled(db, tenant_id=row.tenant_id, enabled=False)
        policy = TenantAccessPolicy()
    elif gate == "recipient":
        harness.settings = harness.settings.model_copy(
            update={
                "direct_message_policies": {row.tenant_id: DirectMessagePolicy(mode="disabled")}
            }
        )
        policy = TenantAccessPolicy()
    else:
        # Legacy unknown provenance must never become a source-free private run.
        async with harness.factory.begin() as db:
            await db.execute(
                text("UPDATE routines SET source_provenance_known = false WHERE id = :id"),
                {"id": row.id},
            )
        policy = TenantAccessPolicy()
    async with harness.factory.begin() as db:
        await set_access_policy(db, tenant_id=row.tenant_id, policy=policy)
    await harness.fire(row)
    assert not harness.upstream.state.sessions
    after = await harness.reload(row)
    assert after is not None and after.last_error and after.delivery_payload is None


@pytest.mark.parametrize("replacement", [False, True])
async def test_shutdown_uses_live_dm_best_effort_on_actual_scope(
    harness: Harness, replacement: bool
):
    harness.upstream.kill_first_send = replacement
    harness.upstream.scripts = [hang, hang] if replacement else [hang]
    row = await _routine_row(harness)
    task = asyncio.create_task(harness.fire(row))
    await harness.upstream.wait(
        lambda: any(event["type"] == "user.message" for _, event in harness.upstream.sent)
    )
    sid = next(sid for sid, event in harness.upstream.sent if event["type"] == "user.message")
    harness.deps.running_turns.begin_shutdown()
    await harness.dispatcher.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.upstream.events(sid, "user.interrupt")
    after = await harness.reload(row)
    assert after is not None and after.delivery_payload is None


async def test_minimum_work_pool_retains_turn_headroom(harness: Harness, db_schema: str):
    engine = build_test_engine(
        harness.factory.kw["bind"].url.render_as_string(hide_password=False),
        db_schema,
        pool_size=4,
        max_overflow=0,
        pool_timeout=5,
    )
    small = Harness(
        async_sessionmaker(engine, expire_on_commit=False), harness.tenant, harness.upstream
    )
    try:
        a = await _routine_row(small, owner=OWNER_A)
        b = await _routine_row(small, owner=OWNER_B)
        await asyncio.wait_for(small.fire(a), 50)
        async with small.factory() as db:
            results = [await get_routine(db, row.id, tenant_id=row.tenant_id) for row in (a, b)]
        assert all(row is not None and row.last_result_tail == "private answer" for row in results)
    finally:
        await small.dispatcher.close()
        await engine.dispose()


async def test_routine_origin_cannot_promote_handoffs_to_attended(harness: Harness):
    from daimon.core.stores.turn_origins import get_active_origin
    from daimon.core.turn_origin import current_origin_id

    observed = []

    async def inspect(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        origin_id = current_origin_id.get()
        assert origin_id is not None
        async with harness.factory() as db:
            origin = await get_active_origin(
                db,
                origin_id=origin_id,
                tenant_id=harness.tenant.id,
                account_id=harness.accounts[OWNER_A],
                platform="discord",
                now=datetime.now(UTC),
            )
        assert origin is not None
        observed.append(origin.attended)
        async for chunk in answer("unattended parent")(upstream, sid):
            yield chunk

    harness.upstream.scripts = [inspect]
    result = await harness.routine()
    assert result.text == "unattended parent" and observed == [False]


@pytest.mark.parametrize("change", ["agent", "retarget"])
@pytest.mark.parametrize("stage", ["running", "pending"])
async def test_stale_public_edit_cannot_reclassify_concurrent_private_fire(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, change: str, stage: str
):
    from daimon.core.errors import StoreError
    from daimon.core.stores import routines

    # Independent transactions: T1 reads public, T2 converts to DM, then a
    # real scheduler fire uses private memory before T1 attempts its write.
    async with harness.factory.begin() as db:
        public = await routines.create_routine(
            db,
            tenant_id=harness.tenant.id,
            created_by_user_id=OWNER_A,
            agent_id="ag_daimon",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="private work",
            destination_kind="channel",
            destination_id=BUDGET,
            channel_id=BUDGET,
            source_provenance_known=True,
            next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    read, release_edit = asyncio.Event(), asyncio.Event()
    entered, release_fire = asyncio.Event(), asyncio.Event()
    original_get = routines.get_routine
    edits = (
        {"agent_id": "ag_b", "agent_name": "b"}
        if change == "agent"
        else {"destination_kind": "channel", "destination_id": "789", "channel_id": "789"}
    )

    async def stale_edit() -> None:
        from sqlalchemy.sql.dml import Update

        async with harness.factory.begin() as db:
            execute = db.execute

            async def paused_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
                if isinstance(statement, Update) and statement.table.name == "routines":
                    # Pause AFTER every read/validation, immediately before the
                    # final write. The old implementation would retain T2's
                    # private output using its cached public classification.
                    read.set()
                    await release_edit.wait()
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(db, "execute", paused_execute)
            await routines.update_routine(
                db,
                public.id,
                edited_by=public.created_by_user_id,
                tenant_id=public.tenant_id,
                **edits,
            )

    async def private_answer(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
        entered.set()
        await release_fire.wait()
        upstream.state.write_output(sid, "secret.txt", b"private producer A file")
        async for chunk in answer("private producer A text")(upstream, sid):
            yield chunk

    harness.upstream.scripts = [private_answer]
    edit_task = asyncio.create_task(stale_edit())
    await asyncio.wait_for(read.wait(), 15)
    async with harness.factory.begin() as db:
        private = await routines.update_routine(
            db,
            public.id,
            edited_by=public.created_by_user_id,
            tenant_id=public.tenant_id,
            destination_kind="dm",
        )
    assert private is not None
    fire_task = asyncio.create_task(harness.fire(private))
    await asyncio.wait_for(entered.wait(), 15)
    if stage == "pending":
        release_fire.set()
        await fire_task
        async with harness.factory() as db:
            pending = await original_get(db, public.id, tenant_id=public.tenant_id)
        assert pending is not None and pending.delivery_status == "pending"
        assert pending.delivery_payload and "secret.txt" in pending.delivery_payload
    release_edit.set()
    with pytest.raises(StoreError, match="changed while being edited"):
        await edit_task
    async with harness.factory() as db:
        unchanged = await original_get(db, public.id, tenant_id=public.tenant_id)
    assert unchanged is not None
    assert unchanged.updated_at == private.updated_at
    assert unchanged.destination_kind == "dm" and unchanged.agent_id == "ag_daimon"

    # An explicit retry reads the DM revision, so erasure and the new
    # classification happen together. Late enqueue also compares that revision.
    async with harness.factory.begin() as db:
        changed = await routines.update_routine(
            db, public.id, edited_by=public.created_by_user_id, tenant_id=public.tenant_id, **edits
        )
    assert changed is not None and changed.updated_at > private.updated_at
    release_fire.set()
    await fire_task
    async with harness.factory() as db:
        final = await original_get(db, public.id, tenant_id=public.tenant_id)
    assert final is not None
    assert final.last_result_tail is None and final.last_error is None
    assert final.delivery_payload is None and final.delivery_lease_owner is None
    assert final.delivery_status != "pending"

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("reclassified private text/files must not open any destination")

    await harness.deliver(fetch_channel=forbidden, open_dm=forbidden)


@pytest.mark.parametrize("origin", ["cleared_dm", "legacy"])
@pytest.mark.parametrize("change", ["agent", "retarget", "aba"])
@pytest.mark.parametrize("stage", ["running", "pending", "error"])
async def test_destination_free_fire_has_no_personal_memory_or_reclassified_result(
    harness: Harness, origin: str, change: str, stage: str
):
    from daimon.core.stores import routines
    from daimon.core.stores.agent_memory_stores import get_personal_memory_store_id

    sentinel = "OWNER_PERSONAL_MEMORY_SENTINEL"
    async with harness.factory.begin() as db:
        row = await create_routine(
            db,
            tenant_id=harness.tenant.id,
            created_by_user_id=OWNER_A,
            agent_id="ag_daimon",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="private work",
            destination_kind="dm",
            channel_id=None,
            source_provenance_known=True,
            next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    # Seed a real owner's memory-bearing session and a persisted private result.
    harness.upstream.scripts = [answer(sentinel)]
    await harness.fire(row)
    dm_sid = next(reversed(harness.upstream.state.sessions))
    async with harness.factory() as db:
        personal = await get_personal_memory_store_id(
            db,
            tenant_id=row.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=row.tenant_id, ma_agent_id=row.agent_id),
            account_id=harness.accounts[OWNER_A],
        )
    assert personal is not None and personal in harness.memory_stores(dm_sid)
    async with harness.factory.begin() as db:
        if origin == "cleared_dm":
            row = await routines.update_routine(
                db,
                row.id,
                edited_by=row.created_by_user_id,
                tenant_id=row.tenant_id,
                clear_destination=True,
                next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
            )
            assert row is not None and row.last_result_tail is None
        else:
            await routines.delete_routine(db, row.id, tenant_id=row.tenant_id)
            row = await create_routine(
                db,
                tenant_id=harness.tenant.id,
                created_by_user_id=OWNER_A,
                agent_id="ag_daimon",
                agent_name="daimon",
                cron_expr="0 9 * * *",
                timezone_="UTC",
                trigger_message="legacy work",
                source_provenance_known=True,
                next_fire_at=datetime.now(UTC) - timedelta(seconds=1),
            )
    assert row.destination_kind is None and row.channel_id is None
    entered, release = asyncio.Event(), asyncio.Event()

    async def headless(upstream: Upstream, sid: str) -> AsyncIterator[bytes]:
        await upstream.wait(lambda: bool(upstream.events(sid, "user.message")))
        assert personal not in harness.memory_stores(sid)
        assert len(harness.memory_stores(sid)) == 1
        assert set(harness.memory_stores(sid).values()) == {"read_only"}
        entered.set()
        await release.wait()
        if stage == "error":
            raise RuntimeError("old producer raw diagnostic")
        async for chunk in answer("shared-only result")(upstream, sid):
            yield chunk

    harness.upstream.scripts = [headless]
    task = asyncio.create_task(harness.fire(row))
    await asyncio.wait_for(entered.wait(), 15)
    if stage == "pending":
        release.set()
        await task
        completed = await harness.reload(row)
        assert completed is not None and completed.last_result_tail == "shared-only result"
    async with harness.factory.begin() as db:
        await _reclassify(db, row, change, clear_destination=True)
    release.set()
    await task
    final = await harness.reload(row)
    assert final is not None and final.last_result_tail is None and final.last_error is None
    assert final.delivery_payload is None and final.delivery_status != "pending"

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("old output must not open an agent or destination")

    await harness.deliver(fetch_channel=forbidden, open_dm=forbidden)


@pytest.mark.parametrize("safety_on", [False, True])
async def test_dm_routine_disables_sandbox_and_gates_all_mcp_calls(harness: Harness, safety_on):
    harness.with_safety(ToolSafetyPolicy(enabled=safety_on))
    agent = harness.upstream.state.ma.agents["ag_daimon"]
    agent["skills"] = [{"type": "custom", "skill_id": "skill_routine", "version": "1"}]
    agent["tools"].append(
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {"name": "bash", "enabled": True, "permission_policy": {"type": "always_allow"}}
            ],
        }
    )
    reply = await harness.routine()
    snapshot = harness.upstream.state.sessions[reply.session_id]
    assert snapshot.agent.skills[0].model_dump(mode="json")["skill_id"] == "skill_routine"
    for tool in snapshot.agent.tools:
        body = tool.model_dump(mode="json")
        if tool.type == "agent_toolset_20260401":
            assert body["default_config"]["enabled"] is False
            assert all(config["enabled"] is False for config in body["configs"])
        elif tool.type == "mcp_toolset":
            assert body["default_config"]["permission_policy"] == {"type": "always_ask"}
            assert all(
                config["permission_policy"] == {"type": "always_ask"} for config in body["configs"]
            )
    assert reply.text == "private answer"


@pytest.mark.parametrize("safety_on", [False, True])
@pytest.mark.parametrize("shape", ["coordinator", "unknown"])
async def test_dm_routine_refuses_unreviewed_session_before_first_send(
    harness: Harness, safety_on, shape
):
    from anthropic.types.beta import BetaManagedAgentsSession

    harness.with_safety(ToolSafetyPolicy(enabled=safety_on))
    upstream = harness.upstream
    upstream.create_pause_at = 1
    row = await _routine_row(harness)
    task = asyncio.create_task(harness.fire(row))
    await asyncio.wait_for(upstream.create_entered.wait(), timeout=20)
    sid = next(reversed(upstream.state.sessions))
    snapshot = upstream.state.sessions[sid].model_dump(mode="json")
    if shape == "coordinator":
        child = {key: value for key, value in snapshot["agent"].items() if key != "multiagent"}
        snapshot["agent"]["multiagent"] = {"type": "coordinator", "agents": [child]}
    else:
        snapshot["agent"]["unreviewed_capability"] = {}
    upstream.state.sessions[sid] = BetaManagedAgentsSession.model_validate(snapshot)
    upstream.create_gate.set()
    await task
    after = await harness.reload(row)
    assert after is not None and after.last_error == "backend_unsupported"
    assert upstream.sent == [], "the actual DM session must be checked before its first send"
