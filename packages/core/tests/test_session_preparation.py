"""What a bind does to a caller's session when their configuration moved.

Every test drives the real `prepare_session_for_turn` against real Postgres and
the stateful MA sessions fake, with `turn.prepare`'s own primitives injected —
the same wiring `bind_session` builds. Nothing is stubbed at a seam daimon owns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsAnthropicSkill,
    BetaManagedAgentsCustomSkill,
)
from anthropic.types.beta.beta_managed_agents_custom_tool import BetaManagedAgentsCustomTool
from anthropic.types.beta.beta_managed_agents_custom_tool_input_schema import (
    BetaManagedAgentsCustomToolInputSchema,
)
from anthropic.types.beta.session_create_params import Resource
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._ctx import _admission_recheck
from daimon.adapters.mcp.tools.agent_chat import _continue_turn_impl
from daimon.core._models import TenantLedger, UsageEvent
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import McpSettings, load_turn_settings
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ResolvedConfig
from daimon.core.session_preparation import (
    PreparationBusy,
    PreparationDeferred,
    PreparationFailure,
    PreparedReplacement,
    SessionOps,
    prepare_session_for_turn,
)
from daimon.core.session_snapshot import hash_env_bytes, hash_tools
from daimon.core.skills.unresolved import SkillUnavailable
from daimon.core.stores import usage_events
from daimon.core.stores.access_policy import lock_access_policy, set_access_policy
from daimon.core.stores.agent_files import list_agent_files, put_agent_file
from daimon.core.stores.agent_memory_stores import (
    get_memory_store_id,
    get_personal_memory_store_id,
)
from daimon.core.stores.channel_skills import add_channel_skill
from daimon.core.stores.domain import AccountRow, Role, TenantRow, ThreadSessionRow
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.stores.thread_session_lineage import request_fresh_start
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
    mark_turn_active,
    record_session_seals,
    set_pending_unsaved_work,
    update_mutable_fingerprint,
)
from daimon.core.tool_safety import ToolSafetyPolicy, has_confirmation_gate
from daimon.core.turn.admission import Admission
from daimon.core.turn.ceiling import TURN_CEILING_S
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import SessionAgentMismatch, SessionBusyError
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.prepare import (
    ContinuityOutcome,
    PreparedTurn,
    bind_recorder,
    create_fresh_session,
)
from daimon.core.turn_hold import RequesterOrg, TurnHold
from daimon.core.usage_sweep import settle_stamped_session
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    MARouter,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy import Connection, event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import QueuePool
from structlog.testing import capture_logs

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
_AGENT_ID = "ag_prep"
_ENV_ID = "env_prep"
_MODEL = "claude-sonnet-4-6"


def _agent(
    *,
    agent_id: str = _AGENT_ID,
    model_id: str = _MODEL,
    tools: list[BetaManagedAgentsCustomTool] | None = None,
) -> BetaManagedAgentsAgent:
    return ma_agent(
        id=agent_id,
        name="daimon",
        model=model_id,
        tools=list(tools) if tools else [],
        created_at=_NOW,
    )


def _admission(
    *,
    account: AccountRow,
    agent: BetaManagedAgentsAgent | None = None,
    thread_binding_id: uuid.UUID | None = None,
) -> Admission:
    return Admission(
        account_id=account.id,
        agent=agent if agent is not None else _agent(),
        environment=ma_environment(id=_ENV_ID, name="default", created_at=_NOW.isoformat()),
        config=ResolvedConfig(
            agent_name="daimon", environment_name="default", thread_binding_id=thread_binding_id
        ),
    )


def _register(state: FakeSessionsState, agent: BetaManagedAgentsAgent) -> None:
    """Mirror an agent into the fake's store, so a created session freezes it."""
    state.ma.agents[agent.id] = {
        "id": agent.id,
        "type": "agent",
        "name": agent.name,
        "version": agent.version,
        "model": {"id": agent.model.id},
        "system": agent.system,
        "description": None,
        "metadata": {},
        "mcp_servers": [server.model_dump(mode="json") for server in agent.mcp_servers],
        "tools": [tool.model_dump(mode="json") for tool in agent.tools],
        "skills": [],
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }


class _Transport:
    """The MA surface a bind touches, with every request path recorded."""

    def __init__(self) -> None:
        self.state = FakeSessionsState(ma=FakeMAState())
        self.calls: list[tuple[str, str]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail_session_create = False
        self.fail_message = "boom"
        # A skill id the provider refuses to resolve on any create that sends it.
        self.refuse_skill: str | None = None
        self.fail_shared_memory_create = False
        self.retrieve_error: int | None = None
        _register(self.state, _agent())

    @property
    def creates(self) -> int:
        return sum(1 for method, path in self.calls if (method, path) == ("POST", "/v1/sessions"))

    def paths(self, method: str, fragment: str) -> list[str]:
        return [p for m, p in self.calls if m == method and fragment in p]

    def client(self) -> AsyncAnthropic:
        def _record(request: httpx.Request) -> httpx.Response:
            self.calls.append((request.method, request.url.path))
            path = request.url.path
            if (
                self.retrieve_error is not None
                and request.method == "GET"
                and path in {f"/v1/sessions/{sid}" for sid in self.state.sessions}
            ):
                return httpx.Response(
                    self.retrieve_error,
                    json={
                        "type": "error",
                        "error": {"type": "api_error", "message": "unavailable"},
                    },
                )
            if (
                request.method == "POST"
                and path.startswith("/v1/sessions/")
                and path.count("/") == 3
            ):
                self.updates.append(json.loads(request.content))
            if (
                self.fail_shared_memory_create
                and (request.method, path) == ("POST", "/v1/memory_stores")
                and not json.loads(request.content)["name"].startswith("personal ")
            ):
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "outage"},
                    },
                )
            if self.refuse_skill is not None and (request.method, path) == ("POST", "/v1/sessions"):
                agent = json.loads(request.content)["agent"]
                sent = agent.get("skills", []) if isinstance(agent, dict) else []
                if any(skill["skill_id"] == self.refuse_skill for skill in sent):
                    return httpx.Response(
                        400,
                        json={
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": "Could not resolve one or more skills: "
                                f'skill "{self.refuse_skill}"',
                            },
                        },
                    )
            if self.fail_session_create and (request.method, request.url.path) == (
                "POST",
                "/v1/sessions",
            ):
                # 400, not 500: the SDK retries 5xx, and this test counts
                # preparation attempts, not HTTP attempts.
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": self.fail_message,
                        },
                    },
                )
            raise NotHandled

        return build_fake_anthropic(
            combine_handlers(
                _record,
                make_fake_sessions_handler(self.state),
                make_fake_memory_store_handler(),
                make_fake_ma_handler(self.state.ma),
            )
        )


async def _settle_every_session(
    deps: TurnDeps, transport: Any, sessionmaker: async_sessionmaker[AsyncSession]
) -> int:
    """Settle every session the fake holds by its stamps; the calls debited."""
    debited = 0
    for session in list(transport.state.sessions.values()):
        result = await settle_stamped_session(
            deps.anthropic, sessionmaker, session, markup=Decimal("1"), max_pages=None
        )
        debited += result.debited if result is not None else 0
    return debited


def _deps(sessionmaker: async_sessionmaker[AsyncSession], transport: _Transport) -> TurnDeps:
    return TurnDeps(
        anthropic=transport.client(),
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1.0"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )


_PUBLIC_URL = "https://daimon.example/mcp"


def _gated_deps(sessionmaker: async_sessionmaker[AsyncSession], transport: _Transport) -> TurnDeps:
    """A deployment with tool safety on and its own MCP server, as production runs it."""
    return replace(
        _deps(sessionmaker, transport),
        mcp=McpSettings(public_url=_PUBLIC_URL),  # pyright: ignore[reportArgumentType]
        public_url=_PUBLIC_URL,
        tool_safety=ToolSafetyPolicy(enabled=True),
    )


def _toolset(server: str, *configs: str) -> dict[str, Any]:
    allow = {"type": "always_allow"}
    return {
        "type": "mcp_toolset",
        "mcp_server_name": server,
        "default_config": {"enabled": True, "permission_policy": allow},
        "configs": [
            {"name": name, "enabled": True, "permission_policy": allow} for name in configs
        ],
    }


def _agent_with_servers(*extra: BetaManagedAgentsCustomTool) -> BetaManagedAgentsAgent:
    """Daimon's own server and a third-party one, both stored `always_allow` on the agent."""
    return ma_agent(
        id=_AGENT_ID,
        name="daimon",
        model=_MODEL,
        created_at=_NOW,
        mcp_servers=[
            {"type": "url", "name": "daimon-mcp", "url": _PUBLIC_URL},
            {"type": "url", "name": "linear", "url": "https://linear.example/mcp"},
        ],
        tools=[_toolset("daimon-mcp"), _toolset("linear", "create_issue"), *extra],
    )


def _gate(tools: list[dict[str, Any]]) -> tuple[bool, str, list[str]]:
    """Whether add_skill asks, and the third-party toolset's default and per-tool policies."""
    linear = next(tool for tool in tools if tool.get("mcp_server_name") == "linear")
    return (
        has_confirmation_gate(tools, tool_name="add_skill"),
        linear["default_config"]["permission_policy"]["type"],
        [config["permission_policy"]["type"] for config in linear["configs"]],
    )


def _session_gate(transport: _Transport, session_id: str) -> tuple[bool, str, list[str]]:
    agent = transport.state.sessions[session_id].agent
    return _gate([tool.model_dump(mode="json") for tool in agent.tools])


_GATED = (True, "always_ask", ["always_ask"])


async def test_a_tool_safety_session_is_not_drifted_on_its_next_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bind compares the session against the arrays create_session sent, gated."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    agent = _agent_with_servers()
    _register(transport.state, agent)
    deps = _gated_deps(db_session_factory, transport)

    first = await _prepare(
        deps, _admission(account=account, agent=agent), tenant=tenant, account=account
    )
    assert isinstance(first, PreparedTurn)
    assert _session_gate(transport, first.ma_session_id) == _GATED, (
        "the created session asks before add_skill and before a third-party write"
    )

    second = await _prepare(
        deps, _admission(account=account, agent=agent), tenant=tenant, account=account
    )

    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id == first.ma_session_id
    assert second.continuity == ContinuityOutcome(), "an unchanged agent is not a tools change"
    assert transport.updates == [], "and nothing writes the agent's always_allow over the session"
    assert _session_gate(transport, first.ma_session_id) == _GATED


async def test_an_in_place_tools_update_keeps_the_tool_safety_gate(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A real tools change is pushed gated, then reads as current on the turn after."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    _register(transport.state, _agent_with_servers())
    deps = _gated_deps(db_session_factory, transport)
    first = await _prepare(
        deps,
        _admission(account=account, agent=_agent_with_servers()),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn)

    search = BetaManagedAgentsCustomTool(
        description="search the corpus",
        input_schema=BetaManagedAgentsCustomToolInputSchema(type="object"),
        name="search",
        type="custom",
    )
    changed = _agent_with_servers(search)
    _register(transport.state, changed)
    second = await _prepare(
        deps, _admission(account=account, agent=changed), tenant=tenant, account=account
    )

    assert isinstance(second, PreparedTurn)
    assert second.continuity.applied == ("tools",)
    assert [_gate(update["agent"]["tools"]) for update in transport.updates] == [_GATED], (
        "the update carries add_skill's always_ask and the third-party toolset's"
    )
    third = await _prepare(
        deps, _admission(account=account, agent=changed), tenant=tenant, account=account
    )
    assert isinstance(third, PreparedTurn)
    assert third.continuity == ContinuityOutcome(), "the recorded tools are the gated ones"
    assert len(transport.updates) == 1, "so the update is not repeated every turn"


_OPS = SessionOps(
    read_live_row=get_live_thread_session,
    create_fresh=create_fresh_session,
    bind_record=bind_recorder,
)


async def _prepare(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant: TenantRow,
    account: AccountRow,
    thread_id: str = "thread-1",
    transfer: Any = None,
    now: datetime = _NOW,
) -> PreparedTurn | PreparationDeferred | PreparationBusy | PreparationFailure:
    return await prepare_session_for_turn(
        deps,
        admission,
        ops=_OPS,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id=thread_id,
        session_account_id=account.id,
        reuse_existing=True,
        transfer=transfer,
        now=lambda: now,
    )


async def _prepare_message(
    deps: TurnDeps,
    base: PreparedTurn,
    *,
    tenant: TenantRow,
    account: AccountRow,
    thread_id: str = "thread-1",
) -> PreparedTurn:
    """Prepare one message the way production does: its own admission, its own turn.

    A `PreparedTurn` is single-use on the mux path: its admission carries the
    turn observation whose id keys the send operation (`turn/binding.py:46`), so
    two distinct messages sent through one prepared turn collide on that key and
    the second is refused as `OperationConflict`. Adapters admit and prepare per
    message, so a fresh observation is what each message would have been given.
    """
    prepared = await _prepare(
        deps,
        replace(base.admission, observation=None),
        tenant=tenant,
        account=account,
        thread_id=thread_id,
    )
    assert isinstance(prepared, PreparedTurn)
    return prepared


@dataclass
class _SendGates:
    """Which tasks reached the gate where concurrent sends on one session queue.

    Legacy sends queue on the session-mutation fence (a Postgres advisory lock).
    Mux sends queue first on the binding lease (`PostgresStateStore.acquire_lease`,
    polled with nothing held), then cross the same fence. `reached` is set once
    `threshold` distinct tasks have arrived at either gate; `lease_tasks` is how a
    test proves the mux turn I/O ran at all.
    """

    threshold: int
    arrived: set[asyncio.Task[Any] | None] = field(default_factory=set)
    lease_tasks: set[asyncio.Task[Any] | None] = field(default_factory=set)
    reached: asyncio.Event = field(default_factory=asyncio.Event)

    def note(self, *, lease: bool) -> None:
        task = asyncio.current_task()
        self.arrived.add(task)
        if lease:
            self.lease_tasks.add(task)
        if len(self.arrived) >= self.threshold:
            self.reached.set()

    def assert_turn_path(self, deps: TurnDeps, *, prepared_entry: bool) -> None:
        """A prepared turn took the lease exactly when the mux path ran it."""
        if prepared_entry:
            on_mux = (deps.turn_path or load_turn_settings().path) == "mux"
            assert bool(self.lease_tasks) == on_mux, (
                f"turn path {deps.turn_path or load_turn_settings().path!r}: "
                f"{len(self.lease_tasks)} sends took the binding lease"
            )


def _observe_send_gates(monkeypatch: pytest.MonkeyPatch, *, threshold: int) -> _SendGates:
    from daimon.core import session_mutation

    gates = _SendGates(threshold=threshold)
    original_lock = session_mutation.lock_session_mutation
    original_lease = PostgresStateStore.acquire_lease

    async def observed_lock(*args: Any, **kwargs: Any) -> Any:
        gates.note(lease=False)
        return await original_lock(*args, **kwargs)

    async def observed_lease(self: PostgresStateStore, *args: Any, **kwargs: Any) -> Any:
        gates.note(lease=True)
        return await original_lease(self, *args, **kwargs)

    monkeypatch.setattr(session_mutation, "lock_session_mutation", observed_lock)
    monkeypatch.setattr(PostgresStateStore, "acquire_lease", observed_lease)
    return gates


async def _live_row(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant: TenantRow,
    account: AccountRow,
    thread_id: str = "thread-1",
) -> ThreadSessionRow | None:
    async with sessionmaker() as session:
        return await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=thread_id,
            account_id=account.id,
        )


async def _count_live_rows(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant: TenantRow, account: AccountRow
) -> int:
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT count(*) FROM thread_sessions "
                "WHERE tenant_id = :tenant AND account_id = :account AND status = 'live'"
            ),
            {"tenant": tenant.id, "account": account.id},
        )
        return int(result.scalar_one())


async def test_a_foreign_daimon_server_is_healed_at_the_bind_and_in_the_update(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An agent whose `daimon-mcp` points elsewhere gets sessions pointed at this
    deployment: the next bind reads that as current, and a real tools change
    pushes the healed server, so neither updates the session on every turn."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()

    def foreign(*extra: BetaManagedAgentsCustomTool) -> BetaManagedAgentsAgent:
        payload = _agent_with_servers(*extra).model_dump(mode="json")
        payload["mcp_servers"][0]["url"] = "https://elsewhere.example/mcp"
        return BetaManagedAgentsAgent.model_validate(payload)

    agent = foreign()
    _register(transport.state, agent)
    deps = _gated_deps(db_session_factory, transport)

    async def prepare(current: BetaManagedAgentsAgent) -> PreparedTurn:
        prepared = await _prepare(
            deps, _admission(account=account, agent=current), tenant=tenant, account=account
        )
        assert isinstance(prepared, PreparedTurn)
        return prepared

    first = await prepare(agent)
    session_servers = transport.state.sessions[first.ma_session_id].agent.mcp_servers
    assert [server.url for server in session_servers if server.name == "daimon-mcp"] == [
        _PUBLIC_URL
    ], "the session runs this deployment's server"
    second = await prepare(agent)
    assert (second.continuity, transport.updates) == (ContinuityOutcome(), []), (
        "the healed server reads as current on the next bind"
    )

    search = BetaManagedAgentsCustomTool(
        description="search the corpus",
        input_schema=BetaManagedAgentsCustomToolInputSchema(type="object"),
        name="search",
        type="custom",
    )
    changed = foreign(search)
    _register(transport.state, changed)
    third = await prepare(changed)
    assert third.continuity.applied == ("tools",), "one in-place update"
    [update] = transport.updates
    assert [
        server["url"] for server in update["agent"]["mcp_servers"] if server["name"] == "daimon-mcp"
    ] == [_PUBLIC_URL], "the update pushes the healed server"
    fourth = await prepare(changed)
    assert fourth.continuity == ContinuityOutcome(), "and that reads as current after"


@pytest.mark.parametrize("first_held", [False, True])
async def test_a_held_turn_never_shares_a_session_with_an_unheld_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    first_held: bool,
) -> None:
    """The session mounts a Daimon credential bound to one hold, so a turn whose
    hold differs (held after unheld, unheld after held) gets a new session; the same
    hold reuses it."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    hold = TurnHold(platform="slack", channel_id="C-SYN-1", client=True)
    plain = _admission(account=account)
    held = replace(plain, hold=hold)
    before, after = (held, plain) if first_held else (plain, held)

    first = await _prepare(deps, before, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    same = await _prepare(deps, before, tenant=tenant, account=account)
    assert isinstance(same, PreparedTurn) and same.ma_session_id == first.ma_session_id

    other = await _prepare(deps, after, tenant=tenant, account=account)

    assert isinstance(other, PreparedTurn)
    assert other.ma_session_id != first.ma_session_id
    assert other.continuity.state == "replaced"
    # The hold also moves the memory: a held turn mounts its channel's store alone.
    assert other.continuity.applied == (
        ("memory_store", "credential_hold", "memory_scope")
        if first_held
        else ("memory_store", "personal_memory_store", "credential_hold", "memory_scope")
    )


async def test_a_compatible_session_is_reused_without_touching_ma(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = _admission(account=account)

    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    before = len(transport.calls)

    second = await _prepare(deps, admission, tenant=tenant, account=account)

    assert isinstance(second, PreparedTurn), "an unchanged configuration reuses the session"
    assert second.ma_session_id == first.ma_session_id, "and reuses the same MA session"
    assert second.reused is True
    assert second.continuity == ContinuityOutcome(), (
        "nothing changed, so the turn reports a session that simply continued"
    )
    assert transport.calls[before:] == [], (
        "a compatible session must cost no MA call at all on the reuse path"
    )


async def test_missed_usage_after_budget_channel_change_is_recovered_to_current_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id="channel-a"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn)
    second = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id="channel-b"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(second, PreparedTurn)
    assert not second.reused and second.ma_session_id != first.ma_session_id
    assert second.continuity.applied == ("budget_channel",)

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_missed_after_channel_change",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=ma_model_usage(input_tokens=1000, output_tokens=100),
        processed_at=_NOW,
        type="span.model_request_end",
    )
    # The MA event exists, but the live recorder never saw it.
    transport.state.events[second.ma_session_id].append(event.model_dump(mode="json"))
    assert await _settle_every_session(deps, transport, db_session_factory) == 1
    async with db_session_factory() as session:
        usage = (await session.scalars(select(UsageEvent))).one()
        debit = (await session.scalars(select(TenantLedger))).one()
    assert usage.channel_id == "channel-b"
    assert debit.channel_id == "channel-b"


@pytest.mark.parametrize(
    "source,admitted,replace_session",
    [(None, None, False), ("channel-a", "channel-a", False), ("channel-a", "channel-b", True)],
)
async def test_legacy_budget_stamp_uses_ma_before_deciding_replacement(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    source: str | None,
    admitted: str | None,
    replace_session: bool,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id=source),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn) and first.mapping_id is not None
    async with db_session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE thread_sessions SET budget_channel_id = '' WHERE id = :id"),
            {"id": first.mapping_id},
        )

    second = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id=admitted),
        tenant=tenant,
        account=account,
    )
    assert isinstance(second, PreparedTurn)
    assert (second.ma_session_id != first.ma_session_id) is replace_session
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None and row.budget_channel_id == admitted
    async with db_session_factory() as session:
        predecessor = await get_thread_session_by_id(session, id=first.mapping_id)
    assert predecessor is not None and predecessor.budget_channel_id == source
    third = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id=admitted),
        tenant=tenant,
        account=account,
    )
    assert isinstance(third, PreparedTurn) and third.ma_session_id == second.ma_session_id


@pytest.mark.parametrize("missing_snapshot", [True, False], ids=["reuse", "replace"])
async def test_legacy_budget_stamp_does_not_block_independent_writes(
    db_session: AsyncSession,
    db_committing_engine: AsyncEngine,
    missing_snapshot: bool,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    deps = _deps(factory, _Transport())
    first = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id="channel-a"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn) and first.mapping_id is not None
    async with factory() as session:
        original = await get_thread_session_by_id(session, id=first.mapping_id)
    assert original is not None and original.effective_config is not None
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE thread_sessions SET budget_channel_id = '', "
                "effective_config = CASE WHEN :missing THEN NULL ELSE effective_config END "
                "WHERE id = :id"
            ),
            {"id": first.mapping_id, "missing": missing_snapshot},
        )
    async with factory() as session:
        legacy = await get_thread_session_by_id(session, id=first.mapping_id)
    assert legacy is not None and (legacy.effective_config is None) is missing_snapshot

    admitted = "channel-a" if missing_snapshot else "channel-b"

    def bound_lock_wait(connection: Connection) -> None:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")

    # A stamp held by preparation must not block a later independent write.
    # Bound actual database lock waits, rather than healthy I/O under host load.
    event.listen(db_committing_engine.sync_engine, "begin", bound_lock_wait)
    try:
        second = await _prepare(
            deps,
            replace(_admission(account=account), budget_channel_id=admitted),
            tenant=tenant,
            account=account,
        )
    finally:
        event.remove(db_committing_engine.sync_engine, "begin", bound_lock_wait)
    assert isinstance(second, PreparedTurn)
    assert (second.ma_session_id == first.ma_session_id) is missing_snapshot
    async with factory() as session:
        predecessor = await get_thread_session_by_id(session, id=first.mapping_id)
    assert predecessor is not None and predecessor.budget_channel_id == "channel-a"
    if missing_snapshot:
        assert predecessor.effective_config is not None
    else:
        assert predecessor.status == "superseded"


async def test_legacy_budget_stamp_read_failure_keeps_mapping_unknown(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id="channel-a"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn) and first.mapping_id is not None
    async with db_session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE thread_sessions SET budget_channel_id = '' WHERE id = :id"),
            {"id": first.mapping_id},
        )
    transport.retrieve_error = 400
    second = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id="channel-b"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id == first.ma_session_id
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None and row.budget_channel_id == ""


async def test_replacement_checkpoint_live_and_recovered_debits_use_old_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.workspace_transfer import WorkspaceTransferRunner, _bind_checkpoint_recorder

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id="channel-a"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn)
    observation = TurnObservation(db_session_factory, tenant.id, "discord", "channel-b", "thread-1")
    observation.session_id = first.ma_session_id

    async def checkpoint(self: WorkspaceTransferRunner, **kwargs: Any) -> PreparedReplacement:
        old_session_id = kwargs["old_session_id"]
        snapshot = kwargs["old_snapshot"]
        event = BetaManagedAgentsSpanModelRequestEndEvent(
            id="evt_checkpoint_live",
            is_error=False,
            model_request_start_id="start_live",
            model_usage=ma_model_usage(input_tokens=1000, output_tokens=100),
            processed_at=_NOW,
            type="span.model_request_end",
        )
        recorder = _bind_checkpoint_recorder(
            self.sessionmaker,
            tenant_id=self.tenant_id,
            external_user_id=self.external_user_id,
            ma_session_id=old_session_id,
            model_id=snapshot.model_id,
            markup=self.markup,
            channel_id=self.channel_id,
        )
        observation.note_usage(event, metered=True)
        await recorder(event=event)
        missed = event.model_copy(update={"id": "evt_checkpoint_missed"})
        transport.state.events[old_session_id].append(missed.model_dump(mode="json"))
        return PreparedReplacement(
            extra_resources=(), transfer_file_id=None, transfer_kind="history", user_prefix=""
        )

    monkeypatch.setattr(WorkspaceTransferRunner, "__call__", checkpoint)
    runner = WorkspaceTransferRunner(
        anthropic=deps.anthropic,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        external_user_id="user-1",
        markup=Decimal("1"),
        channel_id="channel-b",
    )
    with observation.activate():
        second = await _prepare(
            deps,
            replace(_admission(account=account), budget_channel_id="channel-b"),
            tenant=tenant,
            account=account,
            transfer=runner,
        )
    assert isinstance(second, PreparedTurn) and second.ma_session_id != first.ma_session_id
    assert observation._samples[(first.ma_session_id, "evt_checkpoint_live")].cost is not None
    assert observation.model_by_session[second.ma_session_id] == _MODEL
    new_event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_successor_live",
        is_error=False,
        model_request_start_id="start_successor",
        model_usage=ma_model_usage(input_tokens=1000, output_tokens=100),
        processed_at=_NOW,
        type="span.model_request_end",
    )
    observation.session_id = second.ma_session_id
    observation.note_usage(new_event, metered=True)
    await second._record(event=new_event)
    transport.state.events[second.ma_session_id].append(new_event.model_dump(mode="json"))
    assert observation._samples[(second.ma_session_id, new_event.id)].cost is not None
    assert await _settle_every_session(deps, transport, db_session_factory) == 1
    async with db_session_factory() as session:
        rows = (await session.scalars(select(UsageEvent).order_by(UsageEvent.id))).all()
        debits = (await session.scalars(select(TenantLedger).order_by(TenantLedger.id))).all()
    assert len(rows) == len(debits) == 3
    assert {row.channel_id for row in rows} == {"channel-a", "channel-b"}
    assert {row.channel_id for row in debits} == {"channel-a", "channel-b"}
    assert sum(row.channel_id == "channel-a" for row in debits) == 2
    assert sum(row.channel_id == "channel-b" for row in debits) == 1
    assert all(row.delta_usd < 0 for row in debits)


async def test_budget_channel_change_waits_for_active_session_before_replacement(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    deps = _deps(db_session_factory, _Transport())
    first = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id="channel-a"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn) and first.mapping_id is not None
    async with db_session_factory() as session, session.begin():
        await mark_turn_active(
            session, id=first.mapping_id, active_turn_message_id="in-flight", now=_NOW
        )

    second = await _prepare(
        deps,
        replace(_admission(account=account), budget_channel_id="channel-b"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(second, PreparationBusy)
    assert second.pending_reasons == ("budget_channel",)


async def test_seal_channel_backfill_does_not_change_the_budget_stamp(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    deps = _deps(db_session_factory, _Transport())
    admission = _admission(account=account)
    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    async with db_session_factory() as session, session.begin():
        await record_session_seals(
            session,
            tenant_id=tenant.id,
            ma_session_id=first.ma_session_id,
            seals=frozenset(),
            channel_id="origin-channel",
        )

    second = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(second, PreparedTurn)
    assert second.reused and second.ma_session_id == first.ma_session_id


async def test_compatible_bind_releases_connection_during_vault_io(
    db_engine: AsyncEngine,
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daimon.core.session_preparation as preparation

    _ = db_clean
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await session.commit()
    deps = _deps(sm, _Transport())
    admission = _admission(account=account)
    await _prepare(deps, admission, tenant=tenant, account=account)

    engine = db_engine
    checked_out_at: dict[int, float] = {}
    held_s = 0.0

    def checkout(dbapi: object, record: object, proxy: object) -> None:
        checked_out_at[id(record)] = time.monotonic()

    def checkin(dbapi: object, record: object) -> None:
        nonlocal held_s
        held_s += time.monotonic() - checked_out_at.pop(id(record))

    started = asyncio.Event()
    release = asyncio.Event()
    original = preparation.apply_update_ops

    async def slow_vault(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(preparation, "apply_update_ops", slow_vault)
    event.listen(engine.sync_engine.pool, "checkout", checkout)
    event.listen(engine.sync_engine.pool, "checkin", checkin)
    try:
        task = asyncio.create_task(_prepare(deps, admission, tenant=tenant, account=account))
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0.2)
        checkedout_during_io = cast(QueuePool, engine.sync_engine.pool).checkedout()
        release.set()
        assert isinstance(await task, PreparedTurn)
        assert held_s < 0.1, f"compatible bind held {held_s:.3f} connection-seconds"
        assert checkedout_during_io == 0
    finally:
        release.set()
        event.remove(engine.sync_engine.pool, "checkout", checkout)
        event.remove(engine.sync_engine.pool, "checkin", checkin)


async def test_compatible_bind_rechecks_mapping_after_vault_io(
    db_engine: AsyncEngine,
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daimon.core.session_preparation as preparation

    _ = db_clean
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await session.commit()
    deps = _deps(sm, _Transport())
    admission = _admission(account=account)
    await _prepare(deps, admission, tenant=tenant, account=account)

    started = asyncio.Event()
    release = asyncio.Event()
    original = preparation.apply_update_ops

    async def slow_vault(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(preparation, "apply_update_ops", slow_vault)
    task = asyncio.create_task(_prepare(deps, admission, tenant=tenant, account=account))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        row = await _live_row(sm, tenant=tenant, account=account)
        assert row is not None
        async with sm.begin() as session:
            await request_fresh_start(session, id=row.id, at=_NOW)
        release.set()
        with pytest.raises(SessionBusyError):
            await task
    finally:
        release.set()


async def test_a_new_key_is_swapped_into_the_live_session_without_replacing_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = _admission(account=account)

    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)

    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT_ID)
    async with db_session_factory() as session, session.begin():
        await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=agent_uuid,
            key="TOGGL_TOKEN",
            content="tok",
            set_by_account_id=None,
        )

    second = await _prepare(deps, admission, tenant=tenant, account=account)

    assert isinstance(second, PreparedTurn), "a key change never costs the caller their session"
    assert second.ma_session_id == first.ma_session_id, "the same session picks up the new .env"
    assert second.continuity.state == "updated"
    assert second.continuity.applied == ("env_file",)
    assert transport.creates == 1, "only the first bind may create a session"
    assert len(transport.paths("POST", "/resources")) == 1, "the new .env is mounted once"

    async with db_session_factory() as session:
        rows = await list_agent_files(session, tenant_id=tenant.id, agent_id=agent_uuid)
    refreshed = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert refreshed is not None and refreshed.effective_config is not None
    assert refreshed.effective_config.env_sha256 == hash_env_bytes(assemble_env_bytes(rows)), (
        "the row must record the secrets the session now mounts, or the next bind redoes it"
    )
    assert refreshed.id == row.id, "an in-place refresh is the same row, not a new one"


async def test_a_tool_added_to_the_agent_is_pushed_to_the_live_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)

    tool = BetaManagedAgentsCustomTool(
        description="search the corpus",
        input_schema=BetaManagedAgentsCustomToolInputSchema(type="object"),
        name="search",
        type="custom",
    )
    updated_agent = _agent(tools=[tool])
    _register(transport.state, updated_agent)

    second = await _prepare(
        deps, _admission(account=account, agent=updated_agent), tenant=tenant, account=account
    )

    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id == first.ma_session_id, "tools are updatable in place"
    assert second.continuity.applied == ("tools",), (
        "only the axis that actually differed is reported as applied"
    )
    assert transport.paths("POST", f"/v1/sessions/{first.ma_session_id}") != [], (
        "the tool change must reach MA as a sessions.update"
    )
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None and row.effective_config is not None
    assert row.effective_config.tools_sha256 == hash_tools([tool])


async def test_an_in_place_change_is_deferred_while_a_turn_is_running(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = _admission(account=account)

    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None

    async with db_session_factory() as session, session.begin():
        await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT_ID),
            key="KEY",
            content="v",
            set_by_account_id=None,
        )
        await mark_turn_active(session, id=row.id, active_turn_message_id="msg-in-flight", now=_NOW)
    before = len(transport.calls)

    deferred = await _prepare(deps, admission, tenant=tenant, account=account)

    assert isinstance(deferred, PreparationDeferred), (
        "swapping a running session's .env is a race we have no reason to run"
    )
    assert deferred.pending_reasons == ("env_file",)
    assert deferred.prepared.ma_session_id == first.ma_session_id, "the turn runs as it is"
    assert deferred.prepared.continuity.pending == ("env_file",), (
        "the deferral must be visible on the prepared turn, for the adapter to say so"
    )
    assert transport.calls[before:] == [], "a deferred change touches MA not at all"


async def test_a_replacement_is_deferred_while_a_turn_is_running(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None
    async with db_session_factory() as session, session.begin():
        await mark_turn_active(session, id=row.id, active_turn_message_id="msg-in-flight", now=_NOW)

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    deferred = await _prepare(
        deps, _admission(account=account, agent=moved), tenant=tenant, account=account
    )

    assert isinstance(deferred, PreparationDeferred), (
        "a replacement must never interrupt work already in flight"
    )
    assert deferred.pending_reasons == ("model",)
    assert deferred.prepared.ma_session_id == first.ma_session_id, (
        "a change that keeps the same responder still runs on the caller's own session"
    )
    assert transport.creates == 1, "no successor may be created while the old turn runs"
    still_live = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert still_live is not None and still_live.id == row.id


async def test_a_model_change_replaces_the_session_and_bills_the_new_model(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    old_row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert old_row is not None

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    second = await _prepare(
        deps, _admission(account=account, agent=moved), tenant=tenant, account=account
    )

    assert isinstance(second, PreparedTurn), "a model change is applied by replacement"
    assert second.ma_session_id != first.ma_session_id, (
        "the frozen session cannot run the new model"
    )
    assert second.continuity.state == "replaced"
    assert second.continuity.applied == ("model",)
    assert transport.creates == 2

    superseded = await get_thread_session_by_id(db_session, id=old_row.id)
    assert superseded is not None
    assert superseded.status == "superseded", "the old row hands its task on, it is not deleted"
    assert superseded.replaced_by_id == second.mapping_id, "and names its successor"
    assert await _count_live_rows(db_session_factory, tenant=tenant, account=account) == 1, (
        "a caller has exactly one live session at a time"
    )
    successor = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert successor is not None and successor.predecessor_id == old_row.id

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_replaced",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=ma_model_usage(input_tokens=1000, output_tokens=0),
        processed_at=_NOW,
        type="span.model_request_end",
    )
    await second._record(event=event)  # pyright: ignore[reportPrivateUsage]
    async with db_session_factory() as session:
        rows = await usage_events.list_for_tenant(session, tenant_id=tenant.id)
    assert [row.model for row in rows] == ["claude-opus-5"], (
        "the recorder must bill the model the successor froze, not the one it replaced"
    )
    assert [row.managed_session_id for row in rows] == [second.ma_session_id]


async def test_a_channels_skills_are_added_at_create_and_read_as_current_on_the_next_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Create and the drift check build the same skill list; dropping one replaces the session.

    The list leads with a platform's built-in (Teams' Office skills), which
    rides the same override at its latest version.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    extra = (
        BetaManagedAgentsAnthropicSkill(type="anthropic", skill_id="docx", version="latest"),
        BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_team", version="v7"),
    )
    with_skill = replace(_admission(account=account), channel_skills=extra)

    first = await _prepare(deps, with_skill, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    frozen = transport.state.sessions[first.ma_session_id].agent.skills
    assert [(s.type, s.skill_id, s.version) for s in frozen] == [
        ("anthropic", "docx", "latest"),
        ("custom", "skill_team", "v7"),
    ], "the session runs the platform's skills, then the channel's"

    second = await _prepare(deps, with_skill, tenant=tenant, account=account)
    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id == first.ma_session_id, "the channel's skills are not drift"
    assert transport.creates == 1

    third = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(third, PreparedTurn)
    assert third.ma_session_id != first.ma_session_id, "a removed channel skill leaves the session"
    assert transport.state.sessions[third.ma_session_id].agent.skills == []


async def test_a_fresh_start_retires_the_old_row_and_carries_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = _admission(account=account)

    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    old_row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert old_row is not None
    async with db_session_factory() as session, session.begin():
        await request_fresh_start(session, id=old_row.id, at=_NOW)

    transfers: list[str] = []

    async def _transfer(*, old_session_id: str, **_kwargs: Any) -> PreparedReplacement:
        transfers.append(old_session_id)
        raise AssertionError("a fresh start must carry nothing")

    second = await _prepare(deps, admission, tenant=tenant, account=account, transfer=_transfer)

    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id != first.ma_session_id, "starting over means a new session"
    assert transfers == [], "nothing is carried into a session the caller asked to be empty"
    retired = await get_thread_session_by_id(db_session, id=old_row.id)
    assert retired is not None
    assert retired.status == "retired", "a fresh start retires the old row, it is not superseded"
    assert retired.fresh_start_requested_at is None, "the request is cleared once honoured"
    assert second.continuity.applied == (), "a fresh start is not a configuration change"


async def test_a_marked_active_session_is_not_returned_for_reuse(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    deps = _deps(db_session_factory, _Transport())
    admission = _admission(account=account)
    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn) and first.mapping_id is not None
    async with db_session_factory.begin() as session:
        await mark_turn_active(
            session, id=first.mapping_id, active_turn_message_id="running", now=_NOW
        )
        await request_fresh_start(session, id=first.mapping_id, at=_NOW)

    second = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(second, PreparationBusy)
    row = await get_thread_session_by_id(db_session, id=first.mapping_id)
    assert row is not None and row.fresh_start_requested_at is not None


@pytest.mark.parametrize("removed", [False, True])
async def test_reused_session_announces_new_rotation_exclusion_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    removed: bool,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    deps = _deps(db_session_factory, _Transport())
    admission = _admission(account=account)
    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn) and first.mapping_id is not None
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None and row.effective_config is not None
    url = "https://github.com/acme/refused"
    rotated = row.effective_config.model_copy(
        update={
            "unavailable_repos": () if removed else ((url, _NOW - timedelta(minutes=61)),),
            "unavailable_notice_pending": (url,),
        }
    )
    async with db_session_factory.begin() as session:
        await update_mutable_fingerprint(
            session,
            id=row.id,
            snapshot=rotated,
            mutable_fingerprint=row.mutable_fingerprint or "",
        )
    second = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(second, PreparedTurn)
    assert url in second.continuity.user_prefix
    third = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(third, PreparedTurn)
    assert "<github_access_notice>" not in third.continuity.user_prefix
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None and row.effective_config is not None
    assert row.effective_config.unavailable_notice_pending == ()
    assert row.effective_config.unavailable_notice_sent == (() if removed else (url,))


async def test_the_transfer_hook_is_given_the_old_session_and_its_result_is_mounted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)

    client = transport.client()
    bundle = await client.beta.files.upload(
        file=("daimon-handoff.tar.gz", b"tarball", "application/gzip")
    )
    seen: list[dict[str, Any]] = []

    async def _transfer(**kwargs: Any) -> PreparedReplacement:
        seen.append(kwargs)
        resource: Resource = {
            "type": "file",
            "file_id": bundle.id,
            "mount_path": "/daimon-handoff.tar.gz",
        }
        return PreparedReplacement(
            extra_resources=(resource,),
            transfer_file_id=bundle.id,
            transfer_kind="full",
            user_prefix="Picking up where the last session left off.",
        )

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    second = await _prepare(
        deps,
        _admission(account=account, agent=moved),
        tenant=tenant,
        account=account,
        transfer=_transfer,
    )

    assert isinstance(second, PreparedTurn)
    assert len(seen) == 1, "the hook runs once per replacement"
    assert seen[0]["old_session_id"] == first.ma_session_id, (
        "the hook must be pointed at the session holding the work"
    )
    assert seen[0]["destination_model_id"] == "claude-opus-5"
    assert seen[0]["old_snapshot"].model_id == _MODEL, (
        "and told what the old session was actually running"
    )
    assert (
        transport.state.sandbox[second.ma_session_id]["/mnt/session/uploads/daimon-handoff.tar.gz"]
        == b"tarball"
    ), "the carried bundle must be mounted in the successor"
    assert second.continuity.transfer_kind == "full"
    assert second.continuity.user_prefix.startswith("Picking up"), (
        "the framing the hook produced must reach the adapter"
    )
    successor = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert successor is not None
    assert successor.transfer_file_id == bundle.id, "the row records what was carried"
    assert successor.transfer_kind == "full"


async def test_a_crash_after_the_successor_exists_finishes_the_supersede_on_the_next_bind(
    db_session: AsyncSession,
    db_schema: str,
) -> None:
    """The successor is created and committed before the old row is closed, so
    a process that dies between them leaves two live rows and one usable
    session. The next bind must adopt that session, not pay for another."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    engine = create_async_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"],
        connect_args={"server_settings": {"search_path": f"{db_schema},public"}},
        pool_size=2,
        max_overflow=0,
        pool_timeout=0.05,
    )
    db_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        transport = _Transport()
        deps = _deps(db_session_factory, transport)
        admission = _admission(account=account)

        first = await _prepare(deps, admission, tenant=tenant, account=account)
        assert isinstance(first, PreparedTurn)
        old_row = await _live_row(db_session_factory, tenant=tenant, account=account)
        assert old_row is not None

        # The half-finished state: a successor exists, the old row is still live.
        successor = await create_fresh_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            session_account_id=account.id,
            predecessor_id=old_row.id,
        )
        assert await _count_live_rows(db_session_factory, tenant=tenant, account=account) == 2
        creates_before = transport.creates

        resumed = await _prepare(deps, admission, tenant=tenant, account=account)

        assert isinstance(resumed, PreparedTurn)
        assert resumed.ma_session_id == successor.ma_session_id, (
            "the session the interrupted replacement created is the one to use"
        )
        assert transport.creates == creates_before, "and no second session may be paid for"
        healed = await get_thread_session_by_id(db_session, id=old_row.id)
        assert healed is not None and healed.status == "superseded", (
            "the supersede the crash interrupted must be finished"
        )
        assert healed.replaced_by_id == successor.mapping_id
        assert await _count_live_rows(db_session_factory, tenant=tenant, account=account) == 1

    finally:
        await engine.dispose()


async def test_a_failed_replacement_backs_off_and_leaves_the_old_session_live(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    old_row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert old_row is not None

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    transport.fail_session_create = True

    failed = await _prepare(
        deps, _admission(account=account, agent=moved), tenant=tenant, account=account
    )
    assert isinstance(failed, PreparationFailure), "a failed create must not run the turn"
    assert failed.stage == "create"
    assert failed.reasons == ("model",)
    assert failed.preserved is True, "nothing was torn down, so nothing was lost"

    still_live = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert still_live is not None and still_live.id == old_row.id, (
        "the caller's session is exactly where they left it"
    )

    creates_after_failure = transport.creates
    again = await _prepare(
        deps, _admission(account=account, agent=moved), tenant=tenant, account=account
    )
    assert isinstance(again, PreparationFailure), "the retry is refused until the backoff expires"
    assert again.retry_after > _NOW, "and says when it may be tried again"
    assert transport.creates == creates_after_failure, (
        "a backed-off preparation must not hammer MA on every mention"
    )

    later = await _prepare(
        deps,
        _admission(account=account, agent=moved),
        tenant=tenant,
        account=account,
        now=_NOW + timedelta(hours=2),
    )
    assert isinstance(later, PreparationFailure)
    assert transport.creates == creates_after_failure + 1, (
        "once the backoff expires the replacement is attempted again"
    )


async def test_a_replacement_for_a_deleted_channel_skill_names_it_instead_of_retrying(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Finding #6: a channel skill that no longer exists fails the replacement.

    Asking again fails the same way, so the turn is refused with copy naming
    the skill and how to detach it, not "Ask again to retry".
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await add_channel_skill(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-1",
        skill_id="skill_gone",
        version="1791633132940669",
        name="qa-haiku-canary",
        owner_agent_name=None,
        actor_account_id=None,
    )
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)

    transport.fail_session_create = True
    transport.fail_message = (
        'Could not resolve one or more skills: skill "skill_gone" version '
        '"1791633132940669" not found'
    )
    extra = (
        BetaManagedAgentsCustomSkill(
            type="custom", skill_id="skill_gone", version="1791633132940669"
        ),
    )
    with pytest.raises(SkillUnavailable) as refused:
        await _prepare(
            deps,
            replace(_admission(account=account), channel_skills=extra),
            tenant=tenant,
            account=account,
        )

    message = str(refused.value)
    assert "this channel adds" in message
    assert '"qa-haiku-canary"' in message and "remove_channel_skill" in message
    assert "Ask again to retry" not in message
    still_live = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert still_live is not None and still_live.ma_session_id == first.ma_session_id


async def test_a_refused_platform_skill_is_left_out_without_a_message_or_a_replacement(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A built-in a platform adds that the provider refuses never fails the turn.

    The create is retried without it and the user sees nothing about channel
    skills; it stays out of new sessions for a while, so the next turn, whose
    admission leaves it out, reuses the session instead of replacing it.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    transport.refuse_skill = "docx"
    deps = _deps(db_session_factory, transport)
    team = BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_team", version="v7")
    docx = BetaManagedAgentsAnthropicSkill(type="anthropic", skill_id="docx", version="latest")
    admission = replace(
        _admission(account=account),
        channel_skills=(docx, team),
        platform_skill_ids=frozenset({"docx"}),
    )

    with capture_logs() as logs:
        first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn), "the turn runs"
    frozen = transport.state.sessions[first.ma_session_id].agent.skills
    assert [s.skill_id for s in frozen] == ["skill_team"], "it runs without the refused skill"
    assert transport.creates == 2, "one retry"
    assert [
        log["skill_id"] for log in logs if log["event"] == "session.platform_skill_refused"
    ] == ["docx"], "the operator is told once"
    assert deps.platform_skill_memory.is_refused(tenant.id, "docx", now=datetime.now(UTC)), (
        "admission leaves it out for a while"
    )

    without = replace(admission, channel_skills=(team,), platform_skill_ids=frozenset())
    second = await _prepare(deps, without, tenant=tenant, account=account)
    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id == first.ma_session_id, "no replacement on the next turn"
    assert transport.creates == 2


async def test_the_missing_skill_message_holds_through_the_replacement_backoff(
    db_session: AsyncSession, db_committing_engine: AsyncEngine
) -> None:
    """PR #857 review: a retry inside the backoff names the skill too, without calling MA.

    Separate connections, as in production: the failed preparation's stored
    reason must survive the first refusal being raised.
    """
    sessionmaker = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await add_channel_skill(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-1",
        skill_id="skill_gone",
        version="1",
        name="qa-haiku-canary",
        owner_agent_name=None,
        actor_account_id=None,
    )
    await db_session.commit()
    transport = _Transport()
    deps = _deps(sessionmaker, transport)
    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    transport.fail_session_create = True
    transport.fail_message = (
        'Could not resolve one or more skills: skill "skill_gone" version "1" not found'
    )
    admission = replace(
        _admission(account=account),
        channel_skills=(
            BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_gone", version="1"),
        ),
    )
    with pytest.raises(SkillUnavailable, match="qa-haiku-canary") as first_refusal:
        await _prepare(deps, admission, tenant=tenant, account=account)
    creates = transport.creates

    with pytest.raises(SkillUnavailable, match="qa-haiku-canary") as again:
        await _prepare(deps, admission, tenant=tenant, account=account)

    assert str(again.value) == str(first_refusal.value), "the same copy as the first refusal"
    assert transport.creates == creates, "the backoff still holds: no new create"
    live = await _live_row(sessionmaker, tenant=tenant, account=account)
    assert live is not None and live.ma_session_id == first.ma_session_id


async def test_two_concurrent_preparations_for_one_caller_create_exactly_one_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two mentions landing together must not mint two sessions for one task.

    The shared-connection test factory cannot express concurrency (asyncpg
    refuses two operations on one connection), so each caller gets its own
    engine pointed at this test's schema — which is also what the advisory
    lock has to work across in production: separate connections.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    schema = (await db_session.execute(text("SELECT current_schema()"))).scalar_one()
    dsn = os.environ["DAIMON_DATABASE__TEST_URL"]
    engines = [
        create_async_engine(
            dsn, connect_args={"server_settings": {"search_path": f"{schema},public"}}
        )
        for _ in range(2)
    ]
    transport = _Transport()
    admission = _admission(account=account)

    try:
        both = await asyncio.gather(
            *(
                _prepare(
                    _deps(async_sessionmaker(engine, expire_on_commit=False), transport),
                    admission,
                    tenant=tenant,
                    account=account,
                )
                for engine in engines
            )
        )
    finally:
        for engine in engines:
            await engine.dispose()

    assert all(isinstance(result, PreparedTurn) for result in both)
    session_ids = {result.ma_session_id for result in both if isinstance(result, PreparedTurn)}
    assert len(session_ids) == 1, (
        "the advisory lock is what stops two mentions minting two sessions for one task"
    )
    assert transport.creates == 1
    assert await _count_live_rows(db_session_factory, tenant=tenant, account=account) == 1


@pytest.mark.parametrize(
    ("pool_size", "gate_limit"),
    [(20, 6), (20, 1), (2, 1)],
    ids=["pre-fix-unbounded-reference", "bounded-same-pool", "bounded-burst"],
)
@pytest.mark.parametrize("replacement", [False, True], ids=["fresh", "replacement"])
async def test_fresh_burst_waits_before_pool_checkout(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
    pool_size: int,
    gate_limit: int,
    replacement: bool,
) -> None:
    """Fresh and replacement bursts leave room for short DB transactions."""
    import daimon.core.turn.prepare as turn_prepare
    from daimon.core.session_preparation_gate import PreparationGate, preparation_counts

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    engine = create_async_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"],
        connect_args={"server_settings": {"search_path": f"{db_schema},public"}},
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=0.05,
    )
    sm = async_sessionmaker(engine, expire_on_commit=False)
    transport = _Transport()
    deps = replace(_deps(sm, transport), preparation_gate=PreparationGate(gate_limit))
    admission = _admission(account=account)
    if replacement:
        for index in range(6):
            await _prepare(
                deps, admission, tenant=tenant, account=account, thread_id=f"fresh-{index}"
            )
        moved = _agent(model_id="claude-opus-5")
        _register(transport.state, moved)
        admission = _admission(account=account, agent=moved)
    original = turn_prepare.create_ma_session
    upstream_calls = 0
    checked_out_at: dict[int, float] = {}
    held_s = 0.0
    peak_checked_out = 0

    async def slow_create(*args: Any, **kwargs: Any) -> Any:
        nonlocal upstream_calls
        upstream_calls += 1
        assert cast(QueuePool, engine.sync_engine.pool).checkedout() >= 1
        await asyncio.sleep(0.12)
        return await original(*args, **kwargs)

    def checkout(dbapi: object, record: object, proxy: object) -> None:
        nonlocal peak_checked_out
        checked_out_at[id(record)] = time.monotonic()
        peak_checked_out = max(
            peak_checked_out, cast(QueuePool, engine.sync_engine.pool).checkedout()
        )

    def checkin(dbapi: object, record: object) -> None:
        nonlocal held_s
        held_s += time.monotonic() - checked_out_at.pop(id(record))

    monkeypatch.setattr(turn_prepare, "create_ma_session", slow_create)
    event.listen(engine.sync_engine.pool, "checkout", checkout)
    event.listen(engine.sync_engine.pool, "checkin", checkin)
    try:
        tasks = [
            asyncio.create_task(
                _prepare(
                    deps,
                    admission,
                    tenant=tenant,
                    account=account,
                    thread_id=f"fresh-{index}",
                )
            )
            for index in range(6)
        ]
        await asyncio.sleep(0.04)
        if gate_limit == 1:
            assert preparation_counts()["waiting"] >= 4
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert all(isinstance(result, PreparedTurn) for result in results)
        assert upstream_calls == 6
        assert peak_checked_out <= pool_size
        assert held_s >= 0.72, "the advisory-lock connection still spans upstream creation"
        logging.getLogger(__name__).info(
            "fresh_burst_measurement mode=%s connection_seconds=%.3f "
            "per_session=%.3f peak_checked_out=%d",
            f"bounded_pool_{pool_size}" if gate_limit == 1 else "unbounded_reference",
            held_s,
            held_s / 6,
            peak_checked_out,
        )
        assert preparation_counts() == {"waiting": 0, "active": 0}
    finally:
        event.remove(engine.sync_engine.pool, "checkout", checkout)
        event.remove(engine.sync_engine.pool, "checkin", checkin)
        await engine.dispose()


async def test_two_callers_in_one_thread_each_refresh_only_their_own_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    first_account = await make_account(db_session, tenant=tenant)
    second_account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    mine = await _prepare(
        deps, _admission(account=first_account), tenant=tenant, account=first_account
    )
    theirs = await _prepare(
        deps, _admission(account=second_account), tenant=tenant, account=second_account
    )
    assert isinstance(mine, PreparedTurn) and isinstance(theirs, PreparedTurn)
    assert mine.ma_session_id != theirs.ma_session_id, "a thread session is per caller"
    their_resources_before = list(transport.state.resources[theirs.ma_session_id])

    async with db_session_factory() as session, session.begin():
        await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT_ID),
            key="KEY",
            content="v",
            set_by_account_id=None,
        )

    refreshed = await _prepare(
        deps, _admission(account=first_account), tenant=tenant, account=first_account
    )

    assert isinstance(refreshed, PreparedTurn)
    assert refreshed.continuity.applied == ("env_file",)
    assert transport.state.resources[theirs.ma_session_id] == their_resources_before, (
        "one caller's key must never be mounted into another caller's session"
    )
    their_row = await _live_row(db_session_factory, tenant=tenant, account=second_account)
    assert their_row is not None and their_row.effective_config is not None
    assert their_row.effective_config.env_sha256 is None, (
        "the other caller's session is refreshed at their own next bind, not here"
    )


async def test_a_legacy_row_without_a_snapshot_is_backfilled_and_reused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    client = transport.client()
    created = await client.beta.sessions.create(agent=_AGENT_ID, environment_id=_ENV_ID)

    async with db_session_factory() as session, session.begin():
        legacy = await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=account.id,
            ma_session_id=created.id,
            ma_agent_id=_AGENT_ID,
        )
    assert legacy.effective_config is None, "the pre-continuity shape records no configuration"
    before = len(transport.paths("GET", f"/v1/sessions/{created.id}"))

    prepared = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)

    assert isinstance(prepared, PreparedTurn)
    assert prepared.ma_session_id == created.id, "a legacy row is still reused"
    retrieves = transport.paths("GET", f"/v1/sessions/{created.id}")
    assert len(retrieves) - before == 1, "a legacy row costs exactly one sessions.retrieve"
    backfilled = await get_thread_session_by_id(db_session, id=legacy.id)
    assert backfilled is not None and backfilled.effective_config is not None, (
        "what the session was found to be running must be written back to the row"
    )
    assert backfilled.effective_config.model_id == _MODEL
    assert backfilled.effective_config.github_mode == "app"


@pytest.mark.parametrize("unknown_identity", [False, True])
async def test_retired_github_key_session_starts_fresh_without_its_env_mount(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    unknown_identity: bool,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    client = transport.client()
    file = await client.beta.files.upload(
        file=("legacy.env", b"GH_TOKEN=synthetic\n", "text/plain")
    )
    created = await client.beta.sessions.create(
        agent=_AGENT_ID,
        environment_id=_ENV_ID,
        resources=[{"type": "file", "file_id": file.id, "mount_path": "/workspace/.env"}],
    )
    assert any(resource.type == "file" for resource in transport.state.resources[created.id])
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT_ID)
    async with db_session_factory() as session, session.begin():
        mapping = await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=account.id,
            ma_session_id=created.id,
            ma_agent_id=None if unknown_identity else _AGENT_ID,
        )
        await session.execute(
            text("""
                INSERT INTO agent_files (tenant_id, agent_id, key, content)
                VALUES (:tenant, :agent, 'GH_TOKEN', 'synthetic')
            """),
            {"tenant": tenant.id, "agent": agent_uuid},
        )
        # 0105 sets this marker and retires the DB key in one transaction.
        await session.execute(
            text("""
                UPDATE thread_sessions
                SET fresh_start_requested_at=now(), budget_channel_id=''
                WHERE id=:id
            """),
            {"id": mapping.id},
        )
        await session.execute(
            text("DELETE FROM agent_files WHERE tenant_id=:tenant AND agent_id=:agent"),
            {"tenant": tenant.id, "agent": agent_uuid},
        )

    if not unknown_identity:
        transport.retrieve_error = 400
        blocked = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
        assert isinstance(blocked, PreparationBusy), "a failed metadata read must not reuse the key"
        still_marked = await get_thread_session_by_id(db_session, id=mapping.id)
        assert still_marked is not None and still_marked.fresh_start_requested_at is not None
        transport.retrieve_error = None

    prepared = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(prepared, PreparedTurn)
    assert prepared.ma_session_id != created.id
    assert not any(
        resource.type == "file" for resource in transport.state.resources[prepared.ma_session_id]
    ), "the new session must not mount the retired token file"
    old = await get_thread_session_by_id(db_session, id=mapping.id)
    assert old is not None and old.status == "retired"


async def test_a_responder_change_without_a_handoff_binding_leaves_the_workspace_alone(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    other = _agent(agent_id="ag_other")
    _register(transport.state, other)

    with pytest.raises(SessionAgentMismatch):
        await _prepare(
            deps, _admission(account=account, agent=other), tenant=tenant, account=account
        )

    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None and row.ma_session_id == first.ma_session_id, (
        "one agent's workspace is not another's to take over"
    )
    assert row.status == "live"
    assert transport.creates == 1


async def test_a_responder_change_authorized_by_a_handoff_binding_replaces_the_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    old_row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert old_row is not None

    successor_agent = _agent(agent_id="ag_successor")
    _register(transport.state, successor_agent)
    async with db_session_factory() as session, session.begin():
        binding = await create_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel-1",
            thread_id="thread-1",
            responder_ma_agent_id="ag_successor",
            responder_name="research-bot",
            kind="handoff",
        )

    handed = await _prepare(
        deps,
        _admission(account=account, agent=successor_agent, thread_binding_id=binding.id),
        tenant=tenant,
        account=account,
    )

    assert isinstance(handed, PreparedTurn), "an authorized handoff continues the task"
    assert handed.ma_session_id != first.ma_session_id, "under the new agent's own session"
    assert handed.continuity.state == "replaced"
    assert handed.continuity.applied == ("agent_identity",)
    superseded = await get_thread_session_by_id(db_session, id=old_row.id)
    assert superseded is not None and superseded.status == "superseded"
    assert await _count_live_rows(db_session_factory, tenant=tenant, account=account) == 1


async def test_a_responder_change_does_not_run_the_new_agent_on_the_old_agents_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Observed on staging: `hand_off_task` dispatched its follow-up turn while
    the source agent's own turn marker was still fresh. The uniform deferral
    ran that turn on the CURRENT session — the source agent's workspace,
    credentials and memory — while the footer named the destination. A
    responder change is the one change that has nowhere safe to defer to."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    old_row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert old_row is not None

    successor_agent = _agent(agent_id="ag_successor")
    _register(transport.state, successor_agent)
    async with db_session_factory() as session, session.begin():
        binding = await create_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel-1",
            thread_id="thread-1",
            responder_ma_agent_id="ag_successor",
            responder_name="research-bot",
            kind="handoff",
        )
        await mark_turn_active(
            session, id=old_row.id, active_turn_message_id="msg-in-flight", now=_NOW
        )
    before = len(transport.calls)

    busy = await _prepare(
        deps,
        _admission(account=account, agent=successor_agent, thread_binding_id=binding.id),
        tenant=tenant,
        account=account,
    )

    assert isinstance(busy, PreparationBusy), (
        "the incoming responder must not be handed the outgoing responder's session"
    )
    assert busy.pending_reasons == ("agent_identity",)
    assert busy.retry_after > _NOW, "the caller is told when the switch can be made"
    assert transport.calls[before:] == [], "no turn is prepared, so MA is not touched at all"
    unchanged = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert unchanged is not None and unchanged.id == old_row.id, (
        "the in-flight session is left exactly as it was"
    )
    assert transport.creates == 1, "and no successor is created behind the running turn"


async def test_a_responder_change_proceeds_once_the_turn_marker_has_gone_stale(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A marker older than the per-turn ceiling belongs to a turn that died
    with its process, so it must not block the switch forever."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    old_row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert old_row is not None

    successor_agent = _agent(agent_id="ag_successor")
    _register(transport.state, successor_agent)
    async with db_session_factory() as session, session.begin():
        binding = await create_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel-1",
            thread_id="thread-1",
            responder_ma_agent_id="ag_successor",
            responder_name="research-bot",
            kind="handoff",
        )
        await mark_turn_active(
            session,
            id=old_row.id,
            active_turn_message_id="msg-abandoned",
            now=_NOW - timedelta(seconds=TURN_CEILING_S + 1),
        )

    handed = await _prepare(
        deps,
        _admission(account=account, agent=successor_agent, thread_binding_id=binding.id),
        tenant=tenant,
        account=account,
    )

    assert isinstance(handed, PreparedTurn), "an abandoned marker must not wedge the thread"
    assert handed.ma_session_id != first.ma_session_id
    assert handed.continuity.applied == ("agent_identity",)
    superseded = await get_thread_session_by_id(db_session, id=old_row.id)
    assert superseded is not None and superseded.status == "superseded"


async def test_the_callers_unsaved_work_answer_reaches_the_transfer_hook_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The question is answered in one turn and used in the next, so the answer
    rides the caller's row into the replacement it was given for — and is gone
    from the row afterwards, so it cannot govern a second one."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    old_row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert old_row is not None
    async with db_session_factory() as session, session.begin():
        await set_pending_unsaved_work(session, id=old_row.id, choice="leave")

    seen: list[Any] = []

    async def _transfer(**kwargs: Any) -> PreparedReplacement:
        seen.append(kwargs["unsaved_work"])
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="",
        )

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    second = await _prepare(
        deps,
        _admission(account=account, agent=moved),
        tenant=tenant,
        account=account,
        transfer=_transfer,
    )

    assert isinstance(second, PreparedTurn)
    assert seen == ["leave"], "the transfer decides what to capture from the caller's answer"
    closed = await get_thread_session_by_id(db_session, id=old_row.id)
    assert closed is not None and closed.pending_unsaved_work is None, (
        "one answer governs one replacement"
    )


async def test_a_caller_who_was_never_asked_carries_no_unsaved_work_answer(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)

    seen: list[Any] = []

    async def _transfer(**kwargs: Any) -> PreparedReplacement:
        seen.append(kwargs["unsaved_work"])
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="",
        )

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    await _prepare(
        deps,
        _admission(account=account, agent=moved),
        tenant=tenant,
        account=account,
        transfer=_transfer,
    )

    assert seen == [None], "an unanswered question is None, which the transfer reads as 'capture'"


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("fresh_start", [False, True])
async def test_tightening_memory_access_never_runs_the_writable_session(
    active: bool,
    fresh_start: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = _admission(account=account)
    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None
    if active:
        async with db_session_factory.begin() as session:
            await mark_turn_active(session, id=row.id, active_turn_message_id="in-flight", now=_NOW)

    async def forbidden_checkpoint(**kwargs: Any) -> Any:
        pytest.fail("a writable session must not execute a checkpoint after policy tightens")

    if fresh_start:
        async with db_session_factory.begin() as session:
            await request_fresh_start(session, id=row.id, at=_NOW)
    restricted = replace(admission, memory_read_only=True)
    second = await _prepare(
        deps, restricted, tenant=tenant, account=account, transfer=forbidden_checkpoint
    )
    if active:
        assert isinstance(second, PreparationBusy)
        assert second.pending_reasons == (() if fresh_start else ("memory_access",))
        assert transport.creates == 1
    else:
        assert isinstance(second, PreparedTurn)
        assert second.ma_session_id != first.ma_session_id
        assert second.continuity.applied == (() if fresh_start else ("memory_access",))
        current = await _live_row(db_session_factory, tenant=tenant, account=account)
        assert current is not None and current.effective_config is not None
        assert current.effective_config.memory_read_only
        third = await _prepare(deps, restricted, tenant=tenant, account=account)
        assert isinstance(third, PreparedTurn)
        assert third.ma_session_id == second.ma_session_id


async def _repoint_shared_store(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant: TenantRow, store_id: str
) -> None:
    """Point the agent's shared memory binding at another store, as a place moving
    to its own store would."""
    from daimon.core._models import AgentMemoryStore
    from sqlalchemy import update

    async with sessionmaker.begin() as session:
        await session.execute(
            update(AgentMemoryStore)
            .where(
                AgentMemoryStore.tenant_id == tenant.id,
                AgentMemoryStore.agent_id
                == derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_AGENT_ID),
            )
            .values(memory_store_id=store_id)
        )


@pytest.mark.parametrize("budget_moves", [False, True], ids=["store-only", "with-budget-channel"])
@pytest.mark.parametrize("active", [False, True], ids=["idle", "active"])
async def test_leaving_a_memory_store_carries_nothing_and_never_reruns_the_old_session(
    active: bool,
    budget_moves: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The old session has read the store it leaves. An idle one is replaced
    with nothing checkpointed or transferred into the new store; an active one
    refuses the turn rather than being handed back while replacement waits.
    A billing channel changing at the same time picks another replacement
    reason, and must not let the old store's work through either."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first_admission = replace(_admission(account=account), budget_channel_id="channel-a")
    admission = (
        replace(first_admission, budget_channel_id="channel-b") if budget_moves else first_admission
    )
    first = await _prepare(deps, first_admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None
    if active:
        async with db_session_factory.begin() as session:
            await mark_turn_active(session, id=row.id, active_turn_message_id="in-flight", now=_NOW)
    await _repoint_shared_store(db_session_factory, tenant=tenant, store_id="memstore_elsewhere")

    async def forbidden_transfer(**kwargs: Any) -> Any:
        pytest.fail("work read from the old store must not be carried into another one")

    second = await _prepare(
        deps, admission, tenant=tenant, account=account, transfer=forbidden_transfer
    )

    reason = "budget_channel" if budget_moves else "memory_store"
    if active:
        assert isinstance(second, PreparationBusy)
        assert reason in second.pending_reasons
        assert transport.creates == 1
    else:
        assert isinstance(second, PreparedTurn)
        assert second.ma_session_id != first.ma_session_id
        assert reason in second.continuity.applied
        mounted = transport.state.sessions[second.ma_session_id].resources
        stores = [r for r in mounted if r.type == "memory_store"]
        assert [getattr(r, "memory_store_id", None) for r in stores][:1] == ["memstore_elsewhere"]


@pytest.mark.parametrize(
    ("recorded", "desired", "leaves"),
    [
        (("memstore_a", None), ("memstore_b", None), True),
        ((None, None), ("memstore_b", None), False),
        (("memstore_a", "personal_a"), ("memstore_a", "personal_b"), True),
        (("memstore_a", "personal_a"), ("memstore_a", None), True),
        (("memstore_a", None), ("memstore_a", "personal_a"), False),
        (("memstore_a", "personal_a"), ("memstore_a", "personal_a"), False),
    ],
    ids=[
        "shared-changes",
        "shared-gained",
        "personal-changes",
        "personal-dropped",
        "personal-gained",
        "unchanged",
    ],
)
def test_only_a_mounted_store_that_changes_is_a_memory_boundary(
    recorded: tuple[str | None, str | None],
    desired: tuple[str | None, str | None],
    leaves: bool,
) -> None:
    from daimon.core.session_preparation import (
        _leaves_memory_store,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.session_snapshot import SessionSnapshot

    def snapshot(stores: tuple[str | None, str | None]) -> SessionSnapshot:
        return SessionSnapshot(
            ma_agent_id="agent_baseline",
            model_id="claude-sonnet-5",
            system_sha256="system-hash",
            skills_sha256="skills-hash",
            environment_id="env_baseline",
            repo_url=None,
            repo_branch=None,
            memory_store_id=stores[0],
            personal_memory_store_id=stores[1],
            vault_id=None,
            tools_sha256="tools-hash",
            mcp_servers_sha256="mcp-hash",
            env_sha256="env-hash",
            agent_version=1,
            agent_name="research-bot",
        )

    assert _leaves_memory_store(snapshot(recorded), snapshot(desired)) is leaves
    assert _leaves_memory_store(None, snapshot(desired)) is False


@pytest.mark.parametrize(
    ("sealed", "is_dm", "dm_read_only", "expected"),
    [
        (False, False, False, False),
        (True, False, False, True),
        (False, True, False, False),
        (False, True, True, True),
    ],
    ids=["open-channel", "sealed-thread", "open-dm", "restricted-dm"],
)
async def test_admission_origin_controls_the_actual_memory_mount(
    sealed: bool,
    is_dm: bool,
    dm_read_only: bool,
    expected: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.turn.admission import admit
    from daimon.testing.factories import make_ledger_entry, make_tenant_config
    from daimon.testing.ma import resolved_agent_env_router

    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("channel-1",) if sealed else (),
            dm_memory_read_only=dm_read_only,
        ),
    )
    await db_session.commit()
    agent = ma_agent(id=_AGENT_ID, name="daimon", tenant_id=tenant.id)
    environment = ma_environment(id=_ENV_ID, name="default", tenant_id=tenant.id)
    transport = _Transport()
    _register(transport.state, agent)
    deps = _deps(db_session_factory, transport)
    admission_deps = replace(
        deps, anthropic=build_fake_anthropic(resolved_agent_env_router(agent, environment).dispatch)
    )
    admission = await admit(
        admission_deps,
        tenant_id=tenant.id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="user-1",
        channel_id="channel-1",
        thread_id="thread-1",
        is_dm=is_dm,
        now=_NOW,
    )
    fresh = await create_fresh_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        session_account_id=admission.account_id,
    )
    assert fresh.snapshot.memory_store_id is not None
    assert fresh.snapshot.memory_read_only is expected
    observed = await deps.anthropic.beta.sessions.retrieve(fresh.ma_session_id)
    memory = next(r for r in observed.resources if r.type == "memory_store")
    assert memory.access == ("read_only" if expected else "read_write")


async def test_restricted_turn_replaces_a_missing_legacy_session_safely(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    legacy = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        account_id=account.id,
        ma_session_id="ses_gone",
    )
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = replace(_admission(account=account), memory_read_only=True)
    prepared = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(prepared, PreparedTurn)
    assert prepared.ma_session_id != legacy.ma_session_id
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None and row.effective_config is not None
    assert row.effective_config.memory_read_only


# --- the seal a successor inherits -------------------------------------------


def _sealed(
    admission: Admission, *, seal_id: str | None, also: frozenset[str] = frozenset()
) -> Admission:
    """A channel turn in thread-1 under vault, sealed by `seal_id` (or open)."""
    return replace(
        admission,
        origin_channel_id="vault",
        origin_thread_id="thread-1",
        origin_seal_ids=also | (frozenset() if seal_id is None else frozenset({seal_id})),
    )


def _sealed_stamp(transport: _Transport, session_id: str) -> str | None:
    return transport.state.sessions[session_id].metadata.get("daimon_sealed")


async def test_a_replacement_after_an_unseal_keeps_the_predecessors_seal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sealed turn, unseal, model change: the successor carries the old work
    and must stay under the seal it was written under."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(
        deps, _sealed(_admission(account=account), seal_id="vault"), tenant=tenant, account=account
    )
    assert isinstance(first, PreparedTurn)
    assert _sealed_stamp(transport, first.ma_session_id) == "vault"

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    second = await _prepare(
        deps,
        _sealed(_admission(account=account, agent=moved), seal_id=None),
        tenant=tenant,
        account=account,
    )

    assert isinstance(second, PreparedTurn)
    assert second.continuity.state == "replaced"
    assert _sealed_stamp(transport, second.ma_session_id) == "vault"


async def test_an_environment_switch_successor_keeps_the_predecessors_seal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A channel's environment changes under a sealed conversation: the session
    in the new environment carries the old work, so it keeps the old seal ids,
    even once the channel was unsealed in between."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    first = await _prepare(
        deps,
        _sealed(_admission(account=account), seal_id="vault", also=frozenset({"thread-1"})),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn)
    switched = replace(
        _sealed(_admission(account=account), seal_id=None),
        environment=ma_environment(id="env_gpu", name="gpu", created_at=_NOW.isoformat()),
    )
    second = await _prepare(deps, switched, tenant=tenant, account=account)

    assert isinstance(second, PreparedTurn)
    assert second.continuity.state == "replaced", "a new environment replaces the session"
    assert transport.state.sessions[second.ma_session_id].environment_id == "env_gpu", (
        "the successor runs in the channel's new environment"
    )
    assert _sealed_stamp(transport, second.ma_session_id) == "thread-1,vault", (
        "the successor inherits every seal id its predecessor ran under"
    )


async def test_a_transcript_or_bundle_transfer_keeps_the_predecessors_seal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(
        deps,
        _sealed(_admission(account=account), seal_id="thread-1"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn)

    async def _transfer(**_kwargs: Any) -> PreparedReplacement:
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="Carrying the transcript over.",
        )

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    second = await _prepare(
        deps,
        _sealed(_admission(account=account, agent=moved), seal_id="vault"),
        tenant=tenant,
        account=account,
        transfer=_transfer,
    )

    assert isinstance(second, PreparedTurn)
    assert second.continuity.transfer_kind == "transcript"
    assert _sealed_stamp(transport, second.ma_session_id) == "thread-1,vault", (
        "the thread seal it inherits is kept beside the channel seal of this turn"
    )


async def test_a_handoff_successor_keeps_the_predecessors_seal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(
        deps, _sealed(_admission(account=account), seal_id="vault"), tenant=tenant, account=account
    )
    assert isinstance(first, PreparedTurn)
    successor_agent = _agent(agent_id="ag_successor")
    _register(transport.state, successor_agent)
    async with db_session_factory() as session, session.begin():
        binding = await create_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="vault",
            thread_id="thread-1",
            responder_ma_agent_id="ag_successor",
            responder_name="research-bot",
            kind="handoff",
        )

    handed = await _prepare(
        deps,
        _sealed(
            _admission(account=account, agent=successor_agent, thread_binding_id=binding.id),
            seal_id=None,
        ),
        tenant=tenant,
        account=account,
    )

    assert isinstance(handed, PreparedTurn)
    assert handed.continuity.state == "replaced"
    assert _sealed_stamp(transport, handed.ma_session_id) == "vault"


async def test_a_sealed_turn_on_a_reused_session_keeps_its_narrower_seal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Thread sealed on its own, then its channel sealed: both stay recorded."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(
        deps,
        _sealed(_admission(account=account), seal_id="thread-1"),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn)

    from daimon.core.turn.prepare import (
        _stamp_reused_seal,  # pyright: ignore[reportPrivateUsage]
    )

    reused = await _prepare(
        deps, _sealed(_admission(account=account), seal_id="vault"), tenant=tenant, account=account
    )
    assert isinstance(reused, PreparedTurn) and reused.reused
    await _stamp_reused_seal(deps, reused, tenant_id=tenant.id, now=lambda: _NOW)
    await transport.client().beta.sessions.events.send(
        reused.ma_session_id, events=[{"type": "user.message", "content": []}]
    )

    assert _sealed_stamp(transport, reused.ma_session_id) == "thread-1,vault"


async def test_a_successor_of_an_unreadable_predecessor_is_sealed_to_its_thread(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    async with db_session_factory() as session, session.begin():
        gone = await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=account.id,
            ma_session_id="sess_gone",
            ma_agent_id=_AGENT_ID,
        )
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    fresh = await create_fresh_session(
        deps,
        _sealed(_admission(account=account), seal_id=None),
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        session_account_id=account.id,
        predecessor_id=gone.id,
    )

    assert _sealed_stamp(transport, fresh.ma_session_id) == "thread-1"


async def test_a_thread_sealed_with_its_parent_records_both_seals(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fresh and reused alike: unsealing the parent must leave the thread seal."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    both = _sealed(_admission(account=account), seal_id="vault", also=frozenset({"thread-1"}))

    first = await _prepare(deps, both, tenant=tenant, account=account)

    assert isinstance(first, PreparedTurn)
    assert _sealed_stamp(transport, first.ma_session_id) == "thread-1,vault"


# --- Same-thread handoff: the channel's agent changed under a running thread ---------


async def _changed_channel_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    sealed: bool = False,
) -> tuple[TenantRow, _Transport, TurnDeps, TurnDeps, Any]:
    """A thread whose first turn ran as `daimon`, then channel-1 switched to research-bot.

    Returns the tenant, the sessions transport, the prepare deps, the admission
    deps (whose MA serves both agents) and the first prepared turn.
    """
    from daimon.core.turn.admission import admit
    from daimon.testing.factories import make_ledger_entry, make_tenant_config
    from daimon.testing.ma import resolved_agent_env_router
    from mux.testing.drivers.anthropic.ma import MARouter

    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    if sealed:
        await set_access_policy(
            db_session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(sealed_channel_ids=("channel-1",)),
        )
    await db_session.commit()
    daimon = ma_agent(id=_AGENT_ID, name="daimon", tenant_id=tenant.id)
    research = ma_agent(id="ag_research", name="research-bot", tenant_id=tenant.id)
    environment = ma_environment(id=_ENV_ID, name="default", tenant_id=tenant.id)
    transport = _Transport()
    _register(transport.state, daimon)
    _register(transport.state, research)
    deps = _deps(db_session_factory, transport)
    router = MARouter()
    router.add_agent_list(daimon, research)
    resolved_agent_env_router(daimon, environment, router=router)
    router.add_agent(research)
    admission_deps = replace(deps, anthropic=build_fake_anthropic(router.dispatch))
    first_admission = await admit(
        admission_deps,
        tenant_id=tenant.id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="user-1",
        channel_id="channel-1",
        thread_id="thread-1",
        now=_NOW,
    )
    account = await _account_of(db_session_factory, first_admission.account_id)
    first = await _prepare(deps, first_admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="channel-1"),
            tenant_id=tenant.id,
            agent_name="research-bot",
        )
    return tenant, transport, deps, admission_deps, first


async def _account_of(
    sessionmaker: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> AccountRow:
    from daimon.core.stores.accounts import get_account

    async with sessionmaker() as session:
        account = await get_account(session, account_id)
    assert account is not None
    return account


async def _admit_next(admission_deps: TurnDeps, tenant: TenantRow) -> Admission:
    from daimon.core.turn.admission import admit

    return await admit(
        admission_deps,
        tenant_id=tenant.id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="user-1",
        channel_id="channel-1",
        thread_id="thread-1",
        now=_NOW,
    )


@pytest.mark.parametrize(
    ("old_seal", "carried"),
    [("vault", True), ("vault-elsewhere", False)],
    ids=["inside-the-seal", "outside-the-seal"],
)
async def test_a_handoff_carries_the_old_work_only_to_an_agent_inside_its_seal(
    old_seal: str,
    carried: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The old session ran under `old_seal`; the new agent runs in thread-1 under vault.
    Its transcript reaches the new agent only when the new agent could read it there."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(
        deps,
        replace(_sealed(_admission(account=account), seal_id=old_seal), origin_channel_id=old_seal),
        tenant=tenant,
        account=account,
    )
    assert isinstance(first, PreparedTurn)
    assert _sealed_stamp(transport, first.ma_session_id) == old_seal
    successor_agent = _agent(agent_id="ag_successor")
    _register(transport.state, successor_agent)
    async with db_session_factory() as session, session.begin():
        binding = await create_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="vault",
            thread_id="thread-1",
            responder_ma_agent_id="ag_successor",
            responder_name="research-bot",
            kind="handoff",
        )
    calls: list[str] = []

    async def _transfer(**kwargs: Any) -> PreparedReplacement:
        calls.append(kwargs["old_session_id"])
        return PreparedReplacement(
            extra_resources=(), transfer_file_id=None, transfer_kind="transcript", user_prefix="x"
        )

    handed = await _prepare(
        deps,
        _sealed(
            _admission(account=account, agent=successor_agent, thread_binding_id=binding.id),
            seal_id="vault",
        ),
        tenant=tenant,
        account=account,
        transfer=_transfer,
    )

    assert isinstance(handed, PreparedTurn)
    assert handed.continuity.state == "replaced"
    if carried:
        assert calls == [first.ma_session_id]
        assert handed.continuity.transfer_kind == "transcript"
    else:
        assert calls == [], "nothing from outside the new agent's seal is built or carried"
        assert handed.continuity.transfer_kind is None
        assert handed.continuity.user_prefix == ""


async def _handoff_policy(factory, tenant_id, value):
    async with factory.begin() as db:
        await lock_access_policy(db, tenant_id=tenant_id)
        await set_access_policy(db, tenant_id=tenant_id, policy=value)


@pytest.mark.parametrize("status", [400, 429, 500])
@pytest.mark.parametrize("reason", ["model", "memory_access"])
async def test_replacement_retries_when_source_status_is_unavailable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    status: int,
    reason: str,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    deps.anthropic.max_retries = 0
    admission = _admission(account=account)
    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    changed = (
        replace(admission, agent=_agent(model_id="claude-opus-5"))
        if reason == "model"
        else replace(admission, memory_read_only=True)
    )
    _register(transport.state, changed.agent)
    transport.retrieve_error = status
    busy = await _prepare(deps, changed, tenant=tenant, account=account)
    assert isinstance(busy, PreparationBusy)
    assert busy.pending_reasons == (reason,)
    assert busy.retry_after > _NOW
    live = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert live is not None and live.ma_session_id == first.ma_session_id
    assert transport.creates == 1
    transport.retrieve_error = None
    retried = await _prepare(deps, changed, tenant=tenant, account=account, now=busy.retry_after)
    assert isinstance(retried, PreparedTurn)
    assert retried.ma_session_id != first.ma_session_id


async def test_missing_legacy_source_is_not_retrieved_again_for_memory_replacement(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    async with db_session_factory.begin() as session:
        await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=account.id,
            ma_session_id="sess_missing",
        )
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = replace(_admission(account=account), memory_read_only=True)
    result = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(result, PreparedTurn)
    assert result.ma_session_id != "sess_missing"
    assert len(transport.paths("GET", "/v1/sessions/sess_missing")) == 1


@pytest.mark.parametrize("entry", ["handle", "prepared"])
async def test_concurrent_sends_and_replacement_preserve_pool_headroom(
    db_session, db_committing_engine, db_schema, monkeypatch, entry
):
    """Sol's paused production recheck probe, with preparation and replacement.

    Use the exact default 5+10 pool. Only scheduling and MA transport are
    controlled; ownership, authorization, retirement and PostgreSQL locks are
    real. Shorten the pool timeout so the original cycle fails quickly.
    """
    from daimon.core.session_mutation import SessionRetired
    from daimon.core.turn import run as turn_run
    from daimon.testing.db import build_test_engine
    from daimon.testing.turn_fakes import RecordingLifecycle

    seed_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, _, first = await _changed_channel_agent(db_session, seed_factory)
    account = await _account_of(seed_factory, first.admission.account_id)
    message_turns = [
        await _prepare_message(deps, first, tenant=tenant, account=account) for _ in range(15)
    ]
    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"],
        db_schema,
        pool_size=5,
        max_overflow=10,
        pool_timeout=2,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    deps = replace(deps, sessionmaker=factory)
    router = MARouter()
    router.add_agent_list(first.admission.agent)
    client = build_fake_anthropic(
        combine_handlers(make_fake_sessions_handler(transport.state), router.dispatch)
    )
    runtime = SimpleNamespace(
        client=client, session_factory=factory, settings=SimpleNamespace(mcp=deps.mcp)
    )
    auth = AuthIdentity(
        account_id=first.admission.account_id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="user-1",
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=first.admission.agent.id),
    )
    real_recheck = _admission_recheck(
        auth, sessionmaker=factory, tool_name="continue_turn", agent_names=None
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_recheck():
        entered.set()
        await release.wait()
        await real_recheck()

    original_decide = turn_run.decide_before_send
    decisions = 0

    def paused_decide(*args):
        nonlocal decisions
        decide = original_decide(*args)
        decisions += 1
        first_decision = decisions == 1

        async def checked():
            if first_decision:
                entered.set()
                await release.wait()
            await decide()

        return checked

    if entry == "prepared":
        monkeypatch.setattr(turn_run, "decide_before_send", paused_decide)

    async def send(index):
        if entry == "handle":
            return await _continue_turn_impl(
                runtime,
                auth,
                first.ma_session_id,
                f"message {index}",
                recheck=paused_recheck if index == 0 else real_recheck,
            )

        async def reseed():
            return f"message {index}"

        return await turn_run.run_prepared_turn(
            deps,
            message_turns[index],
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            external_user_id="user-1",
            user_message=f"message {index}",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            render_interval_s=0.001,
        )

    async def replacement():
        async with seed_factory.begin() as db:
            await request_fresh_start(db, id=first.mapping_id, at=_NOW)
        return await _prepare(deps, first.admission, tenant=tenant, account=account)

    async def compatible_preparation():
        try:
            return await _prepare(deps, first.admission, tenant=tenant, account=account)
        except SessionBusyError as error:
            # The fresh-start request can land after compatible preparation
            # releases its lock for vault I/O. Its final recheck must refuse
            # that stale result; retry against the now-current mapping.
            assert error.pending_reasons == ("session_changed",)
            return await _prepare(deps, first.admission, tenant=tenant, account=account)

    gates = _observe_send_gates(monkeypatch, threshold=15)
    tasks = []
    try:
        tasks.append(asyncio.create_task(send(0)))
        await asyncio.wait_for(entered.wait(), 5)
        tasks.extend(asyncio.create_task(send(i)) for i in range(1, 15))
        # All peers must reach their queue before releasing the holder: the
        # real PostgreSQL fence on legacy, the binding lease on mux. Try-lock
        # contenders now back off outside the pool/gate.
        await asyncio.wait_for(gates.reached.wait(), 3)
        tasks.append(asyncio.create_task(compatible_preparation()))
        tasks.append(asyncio.create_task(replacement()))
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
        assert not isinstance(results[0], BaseException), (
            "fence holder cannot recheck because waiters exhaust its shared pool",
            results[0],
        )
        assert all(
            not isinstance(r, BaseException) or isinstance(r, SessionRetired) for r in results[:15]
        ), results
        assert all(isinstance(r, PreparedTurn) for r in results[15:]), results[15:]
        assert transport.state.sessions[first.ma_session_id].archived_at is not None
        assert any(e["type"] == "user.message" for e in transport.state.events[first.ma_session_id])
        gates.assert_turn_path(deps, prepared_entry=entry == "prepared")
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        await deps.anthropic.close()
        await engine.dispose()


@pytest.mark.parametrize("entry", ["handle", "prepared"])
async def test_sends_complete_while_two_replacements_block_on_ma_io(
    db_session, db_committing_engine, db_schema, monkeypatch, entry
):
    """Two checkpoints, then two MA creates, must leave sends usable at 5+10."""
    from daimon.core.session_preparation_gate import PreparationGate
    from daimon.core.turn import run as turn_run
    from daimon.testing.db import build_test_engine
    from daimon.testing.turn_fakes import RecordingLifecycle

    seed_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, seed_factory
    )
    account = await _account_of(seed_factory, first.admission.account_id)
    send_turns = []
    for thread in ("replacement-2", "send-1", "send-2"):
        result = await _prepare(
            deps, first.admission, tenant=tenant, account=account, thread_id=thread
        )
        assert isinstance(result, PreparedTurn)
        if thread.startswith("send-"):
            send_turns.append(result)
    # One prepared turn per message sent, as adapters prepare: a turn is not reused.
    phase_turns = {
        phase: [
            await _prepare_message(
                deps, turn, tenant=tenant, account=account, thread_id=f"send-{index + 1}"
            )
            for index, turn in enumerate(send_turns)
        ]
        for phase in ("checkpoint", "create")
    }

    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"],
        db_schema,
        pool_size=5,
        max_overflow=10,
        pool_timeout=2,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    deps = replace(deps, sessionmaker=factory, preparation_gate=PreparationGate(2))
    changed_agent = first.admission.agent.model_copy(
        update={"model": _agent(model_id="claude-opus-5").model}
    )
    _register(transport.state, changed_agent)
    changed = replace(first.admission, agent=changed_agent)
    checkpoint_entered, create_entered = asyncio.Event(), asyncio.Event()
    checkpoint_release, create_release = asyncio.Event(), asyncio.Event()
    checkpoints = creates = 0
    original_create = deps.anthropic.beta.sessions.create

    async def blocked_create(*args, **kwargs):
        nonlocal creates
        creates += 1
        if creates == 2:
            create_entered.set()
        await create_release.wait()
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(deps.anthropic.beta.sessions, "create", blocked_create)

    async def checkpoint(**kwargs):
        nonlocal checkpoints
        await kwargs["before_send"]()
        checkpoints += 1
        if checkpoints == 2:
            checkpoint_entered.set()
        await checkpoint_release.wait()
        return PreparedReplacement(
            extra_resources=(), transfer_file_id=None, transfer_kind="transcript", user_prefix=""
        )

    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="user-1",
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=first.admission.agent.id),
    )
    runtime = SimpleNamespace(
        client=deps.anthropic, session_factory=factory, settings=SimpleNamespace(mcp=deps.mcp)
    )
    recheck = _admission_recheck(
        auth, sessionmaker=factory, tool_name="continue_turn", agent_names=None
    )

    async def send(index, phase):
        prepared = phase_turns[phase][index]
        message = f"send during {phase} {index}"
        if entry == "handle":
            return await _continue_turn_impl(
                runtime, auth, prepared.ma_session_id, message, recheck=recheck
            )

        async def reseed():
            return message

        return await turn_run.run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=f"send-{index + 1}",
            external_user_id="user-1",
            user_message=message,
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            render_interval_s=0.001,
        )

    gates = _observe_send_gates(monkeypatch, threshold=1)
    tasks = [
        asyncio.create_task(
            _prepare(
                deps, changed, tenant=tenant, account=account, thread_id=thread, transfer=checkpoint
            )
        )
        for thread in ("thread-1", "replacement-2")
    ]
    try:
        for phase, entered, release in (
            ("checkpoint", checkpoint_entered, checkpoint_release),
            ("create", create_entered, create_release),
        ):
            try:
                await asyncio.wait_for(entered.wait(), 5)
            except TimeoutError:
                assert not any(task.done() for task in tasks), [
                    task.result() for task in tasks if task.done()
                ]
                raise
            assert all(not task.done() for task in tasks)
            await asyncio.wait_for(asyncio.gather(*(send(i, phase) for i in range(2))), 2)
            for prepared in send_turns:
                assert any(
                    e["type"] == "user.message" and phase in json.dumps(e["content"])
                    for e in transport.state.events[prepared.ma_session_id]
                )
            assert all(not task.done() for task in tasks)
            release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert all(isinstance(result, PreparedTurn) for result in results)
        gates.assert_turn_path(deps, prepared_entry=entry == "prepared")
    finally:
        checkpoint_release.set()
        create_release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await deps.anthropic.close()
        await admission_deps.anthropic.close()
        await engine.dispose()


@pytest.mark.parametrize("entry", ["handle", "prepared"])
@pytest.mark.parametrize("contended", [False, True])
@pytest.mark.parametrize("blocked_phase", ["checkpoint", "create"])
async def test_independent_sends_during_replacements_with_old_session_waiters(
    db_session, db_committing_engine, db_schema, monkeypatch, entry, contended, blocked_phase
):
    """Two checkpoints, then two MA creates, must leave sends usable at 5+10."""
    from daimon.core.session_preparation_gate import PreparationGate
    from daimon.core.turn import run as turn_run
    from daimon.testing.db import build_test_engine
    from daimon.testing.turn_fakes import RecordingLifecycle

    seed_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, seed_factory
    )
    account = await _account_of(seed_factory, first.admission.account_id)
    send_turns = []
    replacement_turns = [first]
    for thread in ("replacement-2", "send-1", "send-2"):
        result = await _prepare(
            deps, first.admission, tenant=tenant, account=account, thread_id=thread
        )
        assert isinstance(result, PreparedTurn)
        if thread.startswith("send-"):
            send_turns.append(result)
        else:
            replacement_turns.append(result)
    # One prepared turn per message sent, as adapters prepare: a turn is not reused.
    phase_turns = {
        phase: [
            await _prepare_message(
                deps, turn, tenant=tenant, account=account, thread_id=f"send-{index + 1}"
            )
            for index, turn in enumerate(send_turns)
        ]
        for phase in ("checkpoint", "create")
    }
    predecessor_turns = [
        await _prepare_message(
            deps,
            replacement_turns[index % 2],
            tenant=tenant,
            account=account,
            thread_id="thread-1" if index % 2 == 0 else "replacement-2",
        )
        for index in range(5)
    ]

    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"],
        db_schema,
        pool_size=5,
        max_overflow=10,
        pool_timeout=2,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    deps = replace(deps, sessionmaker=factory, preparation_gate=PreparationGate(2))
    changed_agent = first.admission.agent.model_copy(
        update={"model": _agent(model_id="claude-opus-5").model}
    )
    _register(transport.state, changed_agent)
    changed = replace(first.admission, agent=changed_agent)
    checkpoint_entered, create_entered = asyncio.Event(), asyncio.Event()
    checkpoint_release, create_release = asyncio.Event(), asyncio.Event()
    checkpoints = creates = 0
    original_create = deps.anthropic.beta.sessions.create

    async def blocked_create(*args, **kwargs):
        nonlocal creates
        creates += 1
        if creates == 2:
            create_entered.set()
        await create_release.wait()
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(deps.anthropic.beta.sessions, "create", blocked_create)

    async def checkpoint(**kwargs):
        nonlocal checkpoints
        await kwargs["before_send"]()
        checkpoints += 1
        if checkpoints == 2:
            checkpoint_entered.set()
        await checkpoint_release.wait()
        return PreparedReplacement(
            extra_resources=(), transfer_file_id=None, transfer_kind="transcript", user_prefix=""
        )

    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="user-1",
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=first.admission.agent.id),
    )
    runtime = SimpleNamespace(
        client=deps.anthropic, session_factory=factory, settings=SimpleNamespace(mcp=deps.mcp)
    )
    recheck = _admission_recheck(
        auth, sessionmaker=factory, tool_name="continue_turn", agent_names=None
    )

    async def send(index, phase, retired=False):
        prepared = predecessor_turns[index] if retired else phase_turns[phase][index]
        message = f"send during {phase} {index}"
        if entry == "handle":
            return await _continue_turn_impl(
                runtime, auth, prepared.ma_session_id, message, recheck=recheck
            )

        async def reseed():
            return message

        return await turn_run.run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=("thread-1" if index % 2 == 0 else "replacement-2")
            if retired
            else f"send-{index + 1}",
            external_user_id="user-1",
            user_message=message,
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            render_interval_s=0.001,
        )

    gates = _observe_send_gates(monkeypatch, threshold=5)
    waiters = []
    predecessor_waiters = []
    tasks = [
        asyncio.create_task(
            _prepare(
                deps, changed, tenant=tenant, account=account, thread_id=thread, transfer=checkpoint
            )
        )
        for thread in ("thread-1", "replacement-2")
    ]
    try:
        for phase, entered, release in (
            ("checkpoint", checkpoint_entered, checkpoint_release),
            ("create", create_entered, create_release),
        ):
            try:
                await asyncio.wait_for(entered.wait(), 5)
            except TimeoutError:
                assert not any(task.done() for task in tasks), [
                    task.result() for task in tasks if task.done()
                ]
                raise
            assert all(not task.done() for task in tasks)
            if contended and phase == blocked_phase:
                predecessor_waiters.extend(
                    asyncio.create_task(send(i, phase, retired=True)) for i in range(5)
                )
                waiters.extend(predecessor_waiters)
                await asyncio.wait_for(gates.reached.wait(), 5)
                # Let every observed acquisition reach PostgreSQL. On the old
                # implementation all five now block holding mutation permits.
                await asyncio.sleep(0.1)
            independent = [asyncio.create_task(send(i, phase)) for i in range(2)]
            waiters.extend(independent)
            completed, pending = await asyncio.wait(
                independent, timeout=3 if contended and phase == blocked_phase else 1
            )
            print(
                f"entry={entry} contended={contended} phase={phase}: independent completed={len(completed)}, pending={len(pending)}, checkedout={engine.pool.checkedout()}, replacements still blocked={all(not t.done() for t in tasks)}",
                flush=True,
            )
            if pending:
                from daimon.core.session_mutation import SessionRetired

                print(
                    f"Unrelated sends still blocked beyond pool_timeout=2s with {15 - engine.pool.checkedout()} unused total connections",
                    flush=True,
                )
                checkpoint_release.set()
                create_release.set()
                replacement_results = await asyncio.wait_for(asyncio.gather(*tasks), 8)
                waiter_results = await asyncio.wait_for(
                    asyncio.gather(*waiters, return_exceptions=True), 8
                )
                assert all(isinstance(r, PreparedTurn) for r in replacement_results)
                assert all(not isinstance(r, BaseException) for r in waiter_results[-2:]), (
                    waiter_results
                )
                assert all(isinstance(r, SessionRetired) for r in waiter_results[-7:-2]), (
                    waiter_results
                )
                print(
                    f"entry={entry} phase={phase}: after MA release both independent sends complete; all five predecessor sends refused SessionRetired",
                    flush=True,
                )
            assert not pending, (
                "unrelated sends starve behind predecessor waiters occupying all mutation slots"
            )
            await asyncio.gather(*independent)
            for prepared in send_turns:
                assert any(
                    e["type"] == "user.message" and phase in json.dumps(e["content"])
                    for e in transport.state.events[prepared.ma_session_id]
                )
            assert all(not task.done() for task in tasks)
            release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert all(isinstance(result, PreparedTurn) for result in results)
        # On mux the retired senders queue on the old binding's lease and learn
        # they are retired one at a time, each after a lease poll of up to
        # `LeaseWait.poll_max_s` (1 s): five of them can take about 5 s.
        retired = await asyncio.wait_for(
            asyncio.gather(*predecessor_waiters, return_exceptions=True), 10
        )
        from daimon.core.session_mutation import SessionRetired

        assert all(isinstance(result, SessionRetired) for result in retired), retired
        gates.assert_turn_path(deps, prepared_entry=entry == "prepared")
    finally:
        checkpoint_release.set()
        create_release.set()
        for task in tasks + waiters:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *waiters, return_exceptions=True)
        await deps.anthropic.close()
        await admission_deps.anthropic.close()
        await engine.dispose()


def _advance_fence_clock(monkeypatch):
    """Advance monotonic time without spending seconds asleep."""
    loop = asyncio.get_running_loop()
    original_time = loop.time
    offset = 0.0

    def time_now():
        return original_time() + offset

    def advance(seconds):
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", time_now)
    return advance


async def test_replacement_prelock_ma_work_exceeds_old_fence_budget(
    db_session, db_session_factory, monkeypatch
):
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    # Legacy rows require an MA identity read before taking the mutation fence.
    async with db_session_factory.begin() as db:
        await db.execute(
            text("UPDATE thread_sessions SET ma_agent_id = NULL WHERE id = :id"),
            {"id": first.mapping_id},
        )
    advance = _advance_fence_clock(monkeypatch)
    original_retrieve = deps.anthropic.beta.sessions.retrieve
    advanced = False

    async def slow_read(*args, **kwargs):
        nonlocal advanced
        result = await original_retrieve(*args, **kwargs)
        if not advanced:
            advanced = True
            advance(6)
        return result

    monkeypatch.setattr(deps.anthropic.beta.sessions, "retrieve", slow_read)
    try:
        result = await _prepare(
            deps, _admission(account=account, agent=moved), tenant=tenant, account=account
        )
        assert isinstance(result, PreparedTurn)
        assert result.ma_session_id != first.ma_session_id
    finally:
        await deps.anthropic.close()


@pytest.mark.parametrize("queue", ["gate", "same_session"])
async def test_turn_queued_behind_long_replacements_waits_then_runs(
    db_session, db_committing_engine, db_schema, monkeypatch, queue
):
    from daimon.core.session_preparation_gate import PreparationGate, preparation_counts
    from daimon.core.turn import run as turn_run
    from daimon.testing.db import build_test_engine
    from daimon.testing.turn_fakes import RecordingLifecycle

    seed_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, seed_factory
    )
    account = await _account_of(seed_factory, first.admission.account_id)
    await _prepare(deps, first.admission, tenant=tenant, account=account, thread_id="replacement-2")
    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, pool_size=5, max_overflow=10
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    deps = replace(deps, sessionmaker=factory, preparation_gate=PreparationGate(2))
    moved = first.admission.agent.model_copy(
        update={"model": _agent(model_id="claude-opus-5").model}
    )
    _register(transport.state, moved)
    changed = replace(first.admission, agent=moved)
    entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rollback_entered, allow_rollback = asyncio.Event(), asyncio.Event()
    retry_backoff, resume_retry = asyncio.Event(), asyncio.Event()
    count = 0

    async def checkpoint(**kwargs):
        nonlocal count
        await kwargs["before_send"]()
        count += 1
        if count == 2:
            entered.set()
        await release.wait()
        return PreparedReplacement(
            extra_resources=(), transfer_file_id=None, transfer_kind="transcript", user_prefix=""
        )

    from daimon.core import session_preparation_stages
    from daimon.core.session_fence_retry import FenceUnavailable
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction

    original_lock = session_preparation_stages.try_fence
    original_exit = AsyncSessionTransaction.__aexit__
    original_sleep = asyncio.sleep
    rollback_finished = asyncio.Event()

    async def observed_lock(*args, **kwargs):
        if asyncio.current_task() is waiter:
            attempted.set()
        return await original_lock(*args, **kwargs)

    async def delayed_rollback(self, exc_type, exc, tb):
        if (
            queue == "same_session"
            and asyncio.current_task() is waiter
            and exc_type is FenceUnavailable
        ):
            rollback_entered.set()
            await allow_rollback.wait()
            result = await original_exit(self, exc_type, exc, tb)
            rollback_finished.set()
            return result
        return await original_exit(self, exc_type, exc, tb)

    async def observed_sleep(delay, *args, **kwargs):
        if (
            queue == "same_session"
            and asyncio.current_task() is waiter
            and rollback_finished.is_set()
        ):
            retry_backoff.set()
            await resume_retry.wait()
        return await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(session_preparation_stages, "try_fence", observed_lock)
    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", delayed_rollback)
    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    advance = _advance_fence_clock(monkeypatch)
    tasks = [
        asyncio.create_task(
            _prepare(
                deps, changed, tenant=tenant, account=account, thread_id=thread, transfer=checkpoint
            )
        )
        for thread in ("thread-1", "replacement-2")
    ]
    waiter = None

    async def queued_turn():
        prepared = await _prepare(deps, changed, tenant=tenant, account=account)
        assert isinstance(prepared, PreparedTurn)

        async def reseed():
            return "queued message"

        await turn_run.run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            external_user_id="user-1",
            user_message="queued message",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            render_interval_s=0.001,
        )
        return prepared

    try:
        await asyncio.wait_for(entered.wait(), 2)
        if queue == "same_session":
            # Let this waiter reach the PostgreSQL preparation fence instead of the gate.
            deps = replace(deps, preparation_gate=PreparationGate(3))
            # Also provide room at the connection headroom gate for its try-lock.
            from daimon.core import session_preparation_gate

            session_preparation_gate._preparation_pool_gates[engine.pool].release()
        waiter = asyncio.create_task(queued_turn())
        if queue == "same_session":
            await asyncio.wait_for(attempted.wait(), 2)
            # A try-lock attempt is not a rollback. Delay the transaction exit to
            # reproduce that gap, then wait for the retry sleep, which is entered
            # only after transaction and permit teardown.
            await asyncio.wait_for(rollback_entered.wait(), 2)
            assert engine.pool.checkedout() == 3
            allow_rollback.set()
            await asyncio.wait_for(retry_backoff.wait(), 2)
        else:
            while preparation_counts()["waiting"] == 0:
                await asyncio.sleep(0)
        assert not waiter.done()
        assert engine.pool.checkedout() == 2
        advance(6)
        # Let elapsed acquisition timers run before releasing the replacements.
        for _ in range(10):
            await asyncio.sleep(0)
        release.set()
        resume_retry.set()
        successors = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        prepared = await asyncio.wait_for(waiter, 3)
        assert prepared.ma_session_id == successors[0].ma_session_id
        assert any(
            e["type"] == "user.message" and "queued message" in json.dumps(e["content"])
            for e in transport.state.events[prepared.ma_session_id]
        )
        assert not any(
            e["type"] == "user.message" and "queued message" in json.dumps(e["content"])
            for e in transport.state.events[first.ma_session_id]
        )
    finally:
        release.set()
        allow_rollback.set()
        resume_retry.set()
        for task in [*tasks, *([waiter] if waiter is not None else [])]:
            task.cancel()
        await asyncio.gather(
            *tasks, *([waiter] if waiter is not None else []), return_exceptions=True
        )
        await deps.anthropic.close()
        await admission_deps.anthropic.close()
        await engine.dispose()


async def test_chat_sessions_in_one_thread_each_mount_only_their_callers_personal_store(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    bob = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    prepared = {}
    for account in (alice, bob):
        turn = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
        assert isinstance(turn, PreparedTurn)
        prepared[account.id] = turn

    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_agent().id)
    async with db_session_factory() as session:
        shared = await get_memory_store_id(session, tenant_id=tenant.id, agent_id=agent_uuid)
        personal = {
            account.id: await get_personal_memory_store_id(
                session, tenant_id=tenant.id, agent_id=agent_uuid, account_id=account.id
            )
            for account in (alice, bob)
        }
    for account in (alice, bob):
        session_id = prepared[account.id].ma_session_id
        mounted = {
            r.memory_store_id
            for r in transport.state.sessions[session_id].resources
            if r.type == "memory_store"
        }
        assert mounted == {shared, personal[account.id]}, "own store and the shared one, only"
        row = await _live_row(db_session_factory, tenant=tenant, account=account)
        assert row is not None and row.effective_config is not None
        assert row.effective_config.memory_store_id == shared
        assert row.effective_config.personal_memory_store_id == personal[account.id]


async def test_a_session_from_before_personal_memory_is_reused_not_replaced(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deploying personal memory must not end live conversations. A session
    recorded without a personal store keeps running, with no MA call, even once
    the caller has one; only a new session mounts it."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = _admission(account=account)
    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn) and first.mapping_id is not None
    async with db_session_factory() as session, session.begin():
        # What a row recorded before this change holds: no personal store.
        await session.execute(
            text(
                "UPDATE thread_sessions SET effective_config = "
                "effective_config - 'personal_memory_store_id' WHERE id = :id"
            ),
            {"id": first.mapping_id},
        )
    before = len(transport.calls)

    second = await _prepare(deps, admission, tenant=tenant, account=account)

    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id == first.ma_session_id and second.reused
    assert second.continuity == ContinuityOutcome()
    assert transport.calls[before:] == []


@pytest.mark.parametrize("active", [False, True], ids=["idle", "active"])
async def test_a_writable_personal_only_mount_is_not_reused_for_a_read_only_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    active: bool,
) -> None:
    """The shared store failed to provision, so the session holds only the caller's
    writable personal store. Once the caller's turn must be read-only (a seal, or
    the DM policy), an idle session is replaced and an active one refuses the
    turn, exactly as for a writable shared mount."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    transport.fail_shared_memory_create = True
    deps = _deps(db_session_factory, transport)
    admission = _admission(account=account)
    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    mounted = transport.state.sessions[first.ma_session_id].resources
    assert [r.access for r in mounted if r.type == "memory_store"] == ["read_write"]
    if active:
        row = await _live_row(db_session_factory, tenant=tenant, account=account)
        assert row is not None
        async with db_session_factory.begin() as session:
            await mark_turn_active(session, id=row.id, active_turn_message_id="in-flight", now=_NOW)

    second = await _prepare(
        deps, replace(admission, memory_read_only=True), tenant=tenant, account=account
    )

    if active:
        assert isinstance(second, PreparationBusy), "never run it on writable personal memory"
    else:
        assert isinstance(second, PreparedTurn) and second.ma_session_id != first.ma_session_id
        fresh = transport.state.sessions[second.ma_session_id].resources
        assert [r.access for r in fresh if r.type == "memory_store"] == ["read_only"]


async def test_a_member_hands_the_thread_to_the_channels_new_agent_in_place(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The channel now answers with research-bot. Before the switch every turn in
    the old thread is refused; after it the next turn runs as research-bot in the
    same thread, with the old work handed over, and the old session is closed."""
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, db_session_factory
    )
    account = await _account_of(db_session_factory, first.admission.account_id)

    stuck = await _admit_next(admission_deps, tenant)
    assert stuck.agent.id == "ag_research"
    with pytest.raises(SessionAgentMismatch):
        await _prepare(deps, stuck, tenant=tenant, account=account)

    outcome = await _click_hand_over(admission_deps, tenant)
    assert outcome.switched, outcome.text

    carried: list[str] = []

    async def _transfer(**kwargs: Any) -> PreparedReplacement:
        carried.append(kwargs["old_session_id"])
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="<previous_session>...</previous_session>",
        )

    handed = await _prepare(
        deps,
        await _admit_next(admission_deps, tenant),
        tenant=tenant,
        account=account,
        transfer=_transfer,
    )
    assert isinstance(handed, PreparedTurn)
    assert handed.admission.agent.id == "ag_research", "the thread now answers as research-bot"
    assert transport.state.sessions[handed.ma_session_id].agent.id == "ag_research"
    assert handed.continuity.state == "replaced"
    assert carried == [first.ma_session_id], "the old conversation was handed over"
    assert first.mapping_id is not None
    old = await get_thread_session_by_id(db_session, id=first.mapping_id)
    assert old is not None and old.status == "superseded", "the old session is closed"
    assert await _count_live_rows(db_session_factory, tenant=tenant, account=account) == 1

    again = await _prepare(
        deps, await _admit_next(admission_deps, tenant), tenant=tenant, account=account
    )
    assert isinstance(again, PreparedTurn)
    assert again.ma_session_id == handed.ma_session_id, "later messages stay with research-bot"


async def test_a_sealed_thread_handed_over_keeps_its_seal_and_read_only_memory(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, db_session_factory, sealed=True
    )
    account = await _account_of(db_session_factory, first.admission.account_id)
    assert _sealed_stamp(transport, first.ma_session_id) == "channel-1"

    assert (await _click_hand_over(admission_deps, tenant)).switched

    handed = await _prepare(
        deps, await _admit_next(admission_deps, tenant), tenant=tenant, account=account
    )
    assert isinstance(handed, PreparedTurn)
    assert handed.admission.agent.id == "ag_research"
    assert _sealed_stamp(transport, handed.ma_session_id) == "channel-1", (
        "the new session carries the thread's seal ids"
    )
    observed = await deps.anthropic.beta.sessions.retrieve(handed.ma_session_id)
    memory = next(r for r in observed.resources if r.type == "memory_store")
    assert memory.access == "read_only", "research-bot's memory is read-only in a sealed thread"


async def test_old_session_cannot_be_written_after_successor_exists(db_session, db_session_factory):
    tenant, transport, deps, admission_deps, first, handed, transfers = await _review_replacement(
        db_session, db_session_factory
    )
    async with db_session_factory() as db:
        old = await get_thread_session_by_id(db, id=first.mapping_id)
    observed = await deps.anthropic.beta.sessions.retrieve(first.ma_session_id)
    router = MARouter()
    router.add_agent_list(first.admission.agent, handed.admission.agent)
    client = build_fake_anthropic(
        combine_handlers(make_fake_sessions_handler(transport.state), router.dispatch)
    )
    runtime = SimpleNamespace(
        client=client, session_factory=db_session_factory, settings=SimpleNamespace(mcp=deps.mcp)
    )
    auth = AuthIdentity(
        account_id=first.admission.account_id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="user-1",
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=first.admission.agent.id),
    )
    recheck = _admission_recheck(
        auth, sessionmaker=db_session_factory, tool_name="continue_turn", agent_names=None
    )
    before = len(transport.state.events.get(first.ma_session_id, []))
    from daimon.core.session_mutation import SessionRetired
    from fastmcp.exceptions import ToolError

    with pytest.raises((SessionRetired, ToolError)):
        await _continue_turn_impl(
            runtime, auth, first.ma_session_id, "write after handoff", recheck=recheck
        )
    after = len(transport.state.events.get(first.ma_session_id, []))
    assert before == after, "superseded old session still accepts a real continue_turn"
    assert observed.archived_at is not None
    assert old.status == "superseded"
    history = [
        event async for event in deps.anthropic.beta.sessions.events.list(first.ma_session_id)
    ]
    assert len(history) == before


async def test_inherited_sealed_transcript_keeps_read_only_memory_after_unseal(
    db_session, db_session_factory
):
    tenant, transport, deps, admission_deps, first, handed, transfers = await _review_replacement(
        db_session, db_session_factory, sealed=True, lift=True
    )
    new = await deps.anthropic.beta.sessions.retrieve(handed.ma_session_id)
    old = await deps.anthropic.beta.sessions.retrieve(first.ma_session_id)
    # Shared and personal mounts alike: a sealed origin keeps both read-only.
    assert [r.access for r in old.resources if r.type == "memory_store"] == [
        "read_only",
        "read_only",
    ]
    assert _sealed_stamp(transport, handed.ma_session_id) == "channel-1"
    assert handed.admission.memory_read_only
    new_memory = [r.access for r in new.resources if r.type == "memory_store"]
    assert new_memory == ["read_only", "read_only"], (
        "sealed content is carried into the new agent with writable shared memory"
    )


async def test_unseal_during_preparation_retains_inherited_memory_restriction(
    db_session, db_committing_engine, monkeypatch
):
    from daimon.core import session_preparation

    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, factory, sealed=True
    )
    account = await _account_of(factory, first.admission.account_id)
    await _handoff_policy(factory, tenant.id, TenantAccessPolicy())
    assert (await _click_hand_over(admission_deps, tenant)).switched
    queued = await _admit_next(admission_deps, tenant)
    assert not queued.memory_read_only
    await _handoff_policy(factory, tenant.id, TenantAccessPolicy(sealed_channel_ids=("channel-1",)))
    entered, release = asyncio.Event(), asyncio.Event()
    original = session_preparation.reauthorize

    async def pause(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(session_preparation, "reauthorize", pause)

    async def carry(**_kwargs):
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="sealed history",
        )

    task = asyncio.create_task(
        _prepare(deps, queued, tenant=tenant, account=account, transfer=carry)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await _handoff_policy(factory, tenant.id, TenantAccessPolicy())
    finally:
        release.set()
    successor = await asyncio.wait_for(task, 5)
    assert isinstance(successor, PreparedTurn)
    assert _sealed_stamp(transport, successor.ma_session_id) == "channel-1"
    assert successor.admission.memory_read_only
    assert [
        r.access
        for r in transport.state.sessions[successor.ma_session_id].resources
        if r.type == "memory_store"
    ] == ["read_only", "read_only"]


async def test_prepared_turn_cannot_resume_retired_session(
    db_session, db_committing_engine, monkeypatch
):
    from daimon.core.turn.run import run_prepared_turn
    from daimon.testing.turn_fakes import RecordingLifecycle

    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, _, first, successor, _ = await _review_replacement(db_session, factory)
    # Prove the DB fence itself rejects the stale prepared capability even if
    # an MA transport ignores archive state.
    transport.state.sessions[first.ma_session_id] = transport.state.sessions[
        first.ma_session_id
    ].model_copy(update={"archived_at": None})
    before = len(transport.state.events.get(first.ma_session_id, []))
    gates = _observe_send_gates(monkeypatch, threshold=1)

    async def reseed():
        return "stale prepared turn"

    from daimon.core.session_mutation import SessionRetired

    with pytest.raises(SessionRetired):
        await run_prepared_turn(
            deps,
            first,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            external_user_id="user-1",
            user_message="stale prepared turn",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            render_interval_s=0.001,
        )
    assert len(transport.state.events.get(first.ma_session_id, [])) == before
    # Which path refused it: the mux binding check answers before the turn
    # takes its lease or reaches the fence; the legacy turn is refused at the
    # session-mutation fence (`require_writable_session`) and never leases.
    on_mux = (deps.turn_path or load_turn_settings().path) == "mux"
    assert (len(gates.lease_tasks), len(gates.arrived)) == ((0, 0) if on_mux else (0, 1))
    async with factory() as db:
        live = await get_live_thread_session(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            account_id=first.session_account_id,
        )
    assert live.id == successor.mapping_id


async def test_retired_mapping_is_reported_only_inside_the_callers_scope(
    db_session, db_committing_engine
):
    """A retired session says so to its own caller; anyone else gets a scope refusal.

    `prepared_persistence` is where the mux path checks the mapping a turn was
    prepared against. The retired answer must not leak another thread, tenant or
    account's mapping state, so each out-of-scope caller is refused as a scope
    violation instead.
    """
    from daimon.core.session_mutation import SessionRetired
    from daimon.core.turn.binding import prepared_persistence
    from mux.errors import ScopeViolation

    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, _, deps, _, first, successor, _ = await _review_replacement(db_session, factory)
    assert first.mapping_id is not None and successor.mapping_id is not None

    async def check(*, admission=first.admission, tenant_id=tenant.id, thread_id="thread-1"):
        return await prepared_persistence(
            deps,
            admission,
            tenant_id=tenant_id,
            platform="discord",
            thread_id=thread_id,
            session_id=first.ma_session_id,
            mapping_id=first.mapping_id,
        )

    with pytest.raises(SessionRetired):
        await check()
    for out_of_scope in (
        {"thread_id": "another-thread"},
        {"tenant_id": uuid.uuid4()},
        {"admission": replace(first.admission, account_id=uuid.uuid4())},
    ):
        with pytest.raises(ScopeViolation):
            await check(**out_of_scope)


async def test_failed_handoff_create_keeps_predecessor_writable(db_session, db_committing_engine):
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, factory
    )
    account = await _account_of(factory, first.admission.account_id)
    assert (await _click_hand_over(admission_deps, tenant)).switched
    transport.fail_session_create = True
    result = await _prepare(
        deps, await _admit_next(admission_deps, tenant), tenant=tenant, account=account
    )
    assert isinstance(result, PreparationFailure)
    assert transport.state.sessions[first.ma_session_id].archived_at is None
    async with factory() as db:
        old = await get_thread_session_by_id(db, id=first.mapping_id)
    assert old.status == "live"


async def test_create_fence_rechecks_inherited_seals(db_session, db_committing_engine, monkeypatch):
    from daimon.core.turn import prepare as prepare_module

    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, factory, sealed=True
    )
    account = await _account_of(factory, first.admission.account_id)
    await _handoff_policy(factory, tenant.id, TenantAccessPolicy())
    assert (await _click_hand_over(admission_deps, tenant)).switched
    original = prepare_module._env_bytes_sha256
    stamped = False

    async def stamp_during_build(*args, **kwargs):
        nonlocal stamped
        value = await original(*args, **kwargs)
        if not stamped:
            stamped = True
            await prepare_module.stamp_session_seal(
                deps,
                first.ma_session_id,
                replace(first.admission, origin_seal_ids=frozenset({"channel-1", "thread-1"})),
                tenant_id=tenant.id,
                now=lambda: _NOW,
            )
        return value

    monkeypatch.setattr(prepare_module, "_env_bytes_sha256", stamp_during_build)
    creates = transport.creates
    with pytest.raises(SessionBusyError):
        await _prepare(
            deps, await _admit_next(admission_deps, tenant), tenant=tenant, account=account
        )
    assert transport.creates == creates
    assert transport.state.sessions[first.ma_session_id].archived_at is None
    successor = await _prepare(
        deps,
        await _admit_next(admission_deps, tenant),
        tenant=tenant,
        account=account,
        now=_NOW + timedelta(minutes=2),
    )
    assert isinstance(successor, PreparedTurn)
    from daimon.core.session_seal import seal_ids

    assert seal_ids(transport.state.sessions[successor.ma_session_id].metadata) == frozenset(
        {"channel-1", "thread-1"}
    )
    assert successor.admission.memory_read_only
    assert [
        r.access
        for r in transport.state.sessions[successor.ma_session_id].resources
        if r.type == "memory_store"
    ] == ["read_only", "read_only"]


async def test_handoff_waits_for_an_inflight_handle_send(
    db_session, db_committing_engine, monkeypatch
):
    from daimon.core import session_preparation
    from daimon.core.session_fence_retry import FenceUnavailable

    contended = asyncio.Event()
    original_lock = session_preparation.lock_session_mutation

    async def observed_lock(*args, **kwargs):
        try:
            return await original_lock(*args, **kwargs)
        except FenceUnavailable:
            contended.set()
            raise

    monkeypatch.setattr(session_preparation, "lock_session_mutation", observed_lock)
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db_session, factory
    )
    account = await _account_of(factory, first.admission.account_id)
    assert (await _click_hand_over(admission_deps, tenant)).switched
    router = MARouter()
    router.add_agent_list(first.admission.agent)
    client = build_fake_anthropic(
        combine_handlers(make_fake_sessions_handler(transport.state), router.dispatch)
    )
    runtime = SimpleNamespace(
        client=client, session_factory=factory, settings=SimpleNamespace(mcp=deps.mcp)
    )
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="user-1",
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=first.admission.agent.id),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    real_send = client.beta.sessions.events.send

    async def paused_send(*args, **kwargs):
        entered.set()
        await release.wait()
        result = await real_send(*args, **kwargs)
        transport.state.sessions[first.ma_session_id] = transport.state.sessions[
            first.ma_session_id
        ].model_copy(update={"status": "running"})
        return result

    monkeypatch.setattr(client.beta.sessions.events, "send", paused_send)
    sending = asyncio.create_task(
        _continue_turn_impl(runtime, auth, first.ma_session_id, "in flight")
    )
    replacing = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        replacing = asyncio.create_task(
            _prepare(
                deps, await _admit_next(admission_deps, tenant), tenant=tenant, account=account
            )
        )
        # Observe an actual failed PostgreSQL try-lock, not an assumed delay.
        await asyncio.wait_for(contended.wait(), 3)
        assert transport.state.sessions[first.ma_session_id].archived_at is None
        release.set()
        await asyncio.wait_for(sending, 5)
        with pytest.raises(SessionBusyError):
            await asyncio.wait_for(replacing, 5)
        assert transport.state.sessions[first.ma_session_id].archived_at is None
        assert await _count_live_rows(factory, tenant=tenant, account=account) == 1
    finally:
        release.set()
        await asyncio.gather(
            sending, *([replacing] if replacing is not None else []), return_exceptions=True
        )


async def _click_hand_over(admission_deps: TurnDeps, tenant: TenantRow) -> Any:
    from daimon.core.native_admins import AdminCaller
    from daimon.core.thread_handoff import switch_thread_on_request

    return await switch_thread_on_request(
        admission_deps.anthropic,
        admission_deps.sessionmaker,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id="thread-1",
        ma_agent_id="ag_research",
        caller=AdminCaller(platform_user_id="user-1"),
        default=admission_deps.deployment_default,
        channel="#channel-1",
        now=_NOW,
    )


async def _review_replacement(db, factory, *, sealed=False, lift=False):
    tenant, transport, deps, admission_deps, first = await _changed_channel_agent(
        db, factory, sealed=sealed
    )
    account = await _account_of(factory, first.admission.account_id)
    if lift:
        await _handoff_policy(factory, tenant.id, TenantAccessPolicy())
    assert (await _click_hand_over(admission_deps, tenant)).switched
    transfers = []

    async def carry(**kw):
        transfers.append(kw["old_session_id"])
        return PreparedReplacement(
            extra_resources=(),
            transfer_file_id=None,
            transfer_kind="transcript",
            user_prefix="SEALED_WORK",
        )

    handed = await _prepare(
        deps,
        await _admit_next(admission_deps, tenant),
        tenant=tenant,
        account=account,
        transfer=carry,
    )
    assert isinstance(handed, PreparedTurn)
    return tenant, transport, deps, admission_deps, first, handed, transfers


# --- Held turns mount their channel's memory alone ---------------------------

_HELD = TurnHold(platform="slack", channel_id="C-SYN-1", client=False)


def _mounted_stores(transport: _Transport, prepared: PreparedTurn) -> list[Any]:
    resources = transport.state.sessions[prepared.ma_session_id].resources
    return [r for r in resources if r.type == "memory_store"]


async def _bound_store_ids(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant: TenantRow
) -> dict[str, list[str]]:
    from daimon.core._models import (
        AgentChannelMemoryStore,
        AgentMemoryStore,
        AgentPersonalMemoryStore,
    )
    from sqlalchemy import select

    async with sessionmaker() as session:
        return {
            name: list(
                (await session.execute(select(column).where(tenant_column == tenant.id))).scalars()
            )
            for name, column, tenant_column in (
                ("shared", AgentMemoryStore.memory_store_id, AgentMemoryStore.tenant_id),
                (
                    "personal",
                    AgentPersonalMemoryStore.memory_store_id,
                    AgentPersonalMemoryStore.tenant_id,
                ),
                (
                    "channel",
                    AgentChannelMemoryStore.memory_store_id,
                    AgentChannelMemoryStore.tenant_id,
                ),
            )
        }


async def test_a_held_turn_mounts_its_channel_store_and_no_personal_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    prepared = await _prepare(
        deps, replace(_admission(account=account), hold=_HELD), tenant=tenant, account=account
    )

    assert isinstance(prepared, PreparedTurn)
    bound = await _bound_store_ids(db_session_factory, tenant=tenant)
    assert bound["shared"] == [] and bound["personal"] == [], (
        "a held session provisions neither the agent-wide nor a personal store"
    )
    (mounted,) = _mounted_stores(transport, prepared)
    assert [mounted.memory_store_id] == bound["channel"]
    assert mounted.access == "read_write"
    assert "including people from another organisation" in (mounted.instructions or "")


async def test_an_unheld_turn_mounts_the_agent_wide_and_personal_stores_never_a_channels(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)

    prepared = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)

    assert isinstance(prepared, PreparedTurn)
    bound = await _bound_store_ids(db_session_factory, tenant=tenant)
    assert bound["channel"] == []
    assert sorted(m.memory_store_id for m in _mounted_stores(transport, prepared)) == sorted(
        [*bound["shared"], *bound["personal"]]
    )
    assert len(bound["shared"]) == 1 and len(bound["personal"]) == 1


async def test_two_held_channels_get_different_stores(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    other = TurnHold(platform="slack", channel_id="C-SYN-2", client=False)

    ids = []
    for thread, hold in (("thread-1", _HELD), ("thread-2", other)):
        prepared = await _prepare(
            deps,
            replace(_admission(account=account), hold=hold),
            tenant=tenant,
            account=account,
            thread_id=thread,
        )
        assert isinstance(prepared, PreparedTurn)
        ids.append(_mounted_stores(transport, prepared)[0].memory_store_id)

    assert ids[0] != ids[1]
    assert sorted(ids) == sorted(
        (await _bound_store_ids(db_session_factory, tenant=tenant))["channel"]
    )


async def test_a_channel_store_session_is_reused_on_the_next_held_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = replace(_admission(account=account), hold=_HELD)

    first = await _prepare(deps, admission, tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    creates = transport.creates
    second = await _prepare(deps, admission, tenant=tenant, account=account)

    assert isinstance(second, PreparedTurn)
    assert second.ma_session_id == first.ma_session_id and second.reused
    assert second.continuity == ContinuityOutcome(), "a channel-store session is not churned"
    assert transport.creates == creates


@pytest.mark.parametrize("active", [False, True], ids=["idle", "active"])
async def test_a_channel_turning_shared_replaces_its_agent_wide_session_without_a_transfer(
    active: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The session that read agent-wide and personal memory never runs in the
    shared channel: idle, it is replaced with nothing carried over; active, the
    turn is refused until it ends (`_leaves_memory_store`, PR #1282)."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(deps, _admission(account=account), tenant=tenant, account=account)
    assert isinstance(first, PreparedTurn)
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None
    if active:
        async with db_session_factory.begin() as session:
            await mark_turn_active(session, id=row.id, active_turn_message_id="in-flight", now=_NOW)

    async def forbidden_transfer(**kwargs: Any) -> Any:
        pytest.fail("agent-wide memory work must not be carried into a channel's store")

    second = await _prepare(
        deps,
        replace(_admission(account=account), hold=_HELD),
        tenant=tenant,
        account=account,
        transfer=forbidden_transfer,
    )

    if active:
        assert isinstance(second, PreparationBusy)
        assert "memory_store" in second.pending_reasons
        assert transport.creates == 1
    else:
        assert isinstance(second, PreparedTurn)
        assert second.ma_session_id != first.ma_session_id
        assert {"memory_store", "personal_memory_store"} <= set(second.continuity.applied)
        bound = await _bound_store_ids(db_session_factory, tenant=tenant)
        assert [m.memory_store_id for m in _mounted_stores(transport, second)] == bound["channel"]


async def test_a_shared_thread_in_a_held_place_mounts_the_channel_store(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A shared thread's session has no caller (and so no credential to hold), but
    its memory is still the place's."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    admission = replace(_admission(account=account), hold=_HELD, shared_owner=account.id)

    prepared = await _prepare(deps, admission, tenant=tenant, account=account)

    assert isinstance(prepared, PreparedTurn)
    bound = await _bound_store_ids(db_session_factory, tenant=tenant)
    assert bound["shared"] == [] and bound["personal"] == []
    assert [m.memory_store_id for m in _mounted_stores(transport, prepared)] == bound["channel"]


_OTHER_HELD = TurnHold(platform="slack", channel_id="C-SYN-2", client=False)


@pytest.mark.parametrize(
    ("before", "after", "carried"),
    [
        (None, _HELD, False),
        (_HELD, None, False),
        (_HELD, _OTHER_HELD, False),
        (_HELD, _HELD, True),
        (None, None, True),
    ],
    ids=["entering", "leaving", "other-held-channel", "same-held-channel", "unheld"],
)
async def test_a_handoff_carries_nothing_across_the_isolation_boundary(
    before: TurnHold | None,
    after: TurnHold | None,
    carried: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A permitted handoff still carries the old work to a new agent on the same
    side of memory isolation, and nothing into, out of or between held places."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = _deps(db_session_factory, transport)
    first = await _prepare(
        deps, replace(_admission(account=account), hold=before), tenant=tenant, account=account
    )
    assert isinstance(first, PreparedTurn)
    successor_agent = _agent(agent_id="ag_successor")
    _register(transport.state, successor_agent)
    async with db_session_factory() as session, session.begin():
        binding = await create_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="vault",
            thread_id="thread-1",
            responder_ma_agent_id="ag_successor",
            responder_name="research-bot",
            kind="handoff",
        )
    calls: list[str] = []

    async def _transfer(**kwargs: Any) -> PreparedReplacement:
        calls.append(kwargs["old_session_id"])
        return PreparedReplacement(
            extra_resources=(), transfer_file_id=None, transfer_kind="transcript", user_prefix="x"
        )

    handed = await _prepare(
        deps,
        replace(
            _admission(account=account, agent=successor_agent, thread_binding_id=binding.id),
            hold=after,
        ),
        tenant=tenant,
        account=account,
        transfer=_transfer,
    )

    assert isinstance(handed, PreparedTurn)
    assert handed.continuity.state == "replaced"
    assert calls == ([first.ma_session_id] if carried else [])


async def test_a_held_place_whose_store_never_provisioned_still_has_its_scope(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With the channel store down nothing mounts, so no store id tells two held
    places apart. The recorded scope does: the same place reuses its session,
    another place replaces it with nothing carried over."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    transport.fail_shared_memory_create = True
    deps = _deps(db_session_factory, transport)

    async def prepare(hold: TurnHold | None) -> Any:
        async def forbidden_transfer(**kwargs: Any) -> Any:
            pytest.fail("work from another place's session must not be carried")

        return await _prepare(
            deps,
            replace(_admission(account=account), hold=hold),
            tenant=tenant,
            account=account,
            transfer=forbidden_transfer,
        )

    first = await prepare(_HELD)
    assert isinstance(first, PreparedTurn)
    assert _mounted_stores(transport, first) == []
    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None and row.effective_config is not None
    assert row.effective_config.memory_scope == "channel:slack:C-SYN-1"
    assert "memory_scope" in row.effective_config.model_dump(mode="json")

    same = await prepare(_HELD)
    assert isinstance(same, PreparedTurn) and same.ma_session_id == first.ma_session_id

    other = await prepare(_OTHER_HELD)
    assert isinstance(other, PreparedTurn)
    assert other.ma_session_id != first.ma_session_id
    assert "memory_scope" in other.continuity.applied

    row = await _live_row(db_session_factory, tenant=tenant, account=account)
    assert row is not None
    async with db_session_factory.begin() as session:
        await mark_turn_active(session, id=row.id, active_turn_message_id="in-flight", now=_NOW)
    busy = await prepare(None)
    assert isinstance(busy, PreparationBusy)
    assert "memory_scope" in busy.pending_reasons


def test_an_unscoped_snapshot_serialises_without_a_scope() -> None:
    """The previous build reads what an unheld session stores: no new key."""
    from daimon.core.session_snapshot import SessionSnapshot

    snapshot = SessionSnapshot(
        ma_agent_id="agent_baseline",
        model_id="claude-sonnet-5",
        system_sha256="s",
        skills_sha256="k",
        environment_id="env",
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        tools_sha256="t",
        mcp_servers_sha256="m",
        env_sha256=None,
        agent_version=1,
        agent_name="research-bot",
    )
    assert "memory_scope" not in snapshot.model_dump(mode="json")
    scoped = snapshot.model_copy(update={"memory_scope": "channel:slack:C-SYN-1"})
    assert scoped.model_dump(mode="json")["memory_scope"] == "channel:slack:C-SYN-1"
