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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_custom_tool import BetaManagedAgentsCustomTool
from anthropic.types.beta.beta_managed_agents_custom_tool_input_schema import (
    BetaManagedAgentsCustomToolInputSchema,
)
from anthropic.types.beta.session_create_params import Resource
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core.config import McpSettings
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.session_preparation import (
    PreparationBusy,
    PreparationDeferred,
    PreparationFailure,
    PreparedReplacement,
    SessionOps,
    prepare_session_for_turn,
)
from daimon.core.session_snapshot import hash_env_bytes, hash_tools
from daimon.core.stores import usage_events
from daimon.core.stores.agent_files import list_agent_files, put_agent_file
from daimon.core.stores.domain import AccountRow, TenantRow, ThreadSessionRow
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.stores.thread_session_lineage import request_fresh_start
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
    mark_turn_active,
    set_pending_unsaved_work,
)
from daimon.core.tool_safety import ToolSafetyPolicy, has_confirmation_gate
from daimon.core.turn.admission import Admission
from daimon.core.turn.ceiling import TURN_CEILING_S
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import SessionAgentMismatch, SessionBusyError
from daimon.core.turn.prepare import (
    ContinuityOutcome,
    PreparedTurn,
    bind_recorder,
    create_fresh_session,
)
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from daimon.testing.ma_models import ma_agent, ma_environment, ma_model_usage
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import QueuePool

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
                request.method == "POST"
                and path.startswith("/v1/sessions/")
                and path.count("/") == 3
            ):
                self.updates.append(json.loads(request.content))
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
                        "error": {"type": "invalid_request_error", "message": "boom"},
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
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The successor is created and committed before the old row is closed, so
    a process that dies between them leaves two live rows and one usable
    session. The next bind must adopt that session, not pay for another."""
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
async def test_fresh_burst_waits_before_pool_checkout(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
    pool_size: int,
    gate_limit: int,
) -> None:
    """Measure the unchanged lock hold, then burst above the bounded pool size."""
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
                    _admission(account=account),
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
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
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
    await _stamp_reused_seal(deps, reused, now=lambda: _NOW)
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
