"""Tests for daimon.core.smoke.run_smoke_check.

Fake MA built at the transport level with MARouter + build_fake_anthropic
(daimon.testing.ma) — real SDK models only, no method-level mocking and no
unvalidated construction shortcuts. A small mutable `_Workspace` stands in
for the MA org: agent/environment CREATE/UPDATE/ARCHIVE handlers store real
SDK-model JSON dumps keyed by id, and the LIST/RETRIEVE handlers read back
from it — this is what lets the self-heal test delete an agent between two
calls and observe a real CREATE request on the retry.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
    BetaManagedAgentsSessionAgent,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_retry_status_terminal import (
    BetaManagedAgentsRetryStatusTerminal,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_unknown_error import (
    BetaManagedAgentsUnknownError,
)
from daimon.core.errors import TurnError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.smoke import SMOKE_TOPUP_USD, SmokeCheckError, run_smoke_check
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores import tenants as tenants_store
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    EMPTY_SESSION_STATS,
    EMPTY_SESSION_USAGE,
    MARouter,
    build_fake_anthropic,
    list_response,
    session_response,
    sse_response,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TENANT_ID = derive_tenant_uuid(platform="cli", workspace_id="smoke")
_CREATED_AT = datetime(2026, 4, 21, tzinfo=UTC)
_MODEL_ID = "claude-sonnet-4-6"
_AGENT_MODEL = BetaManagedAgentsModelConfig(id=_MODEL_ID)


def _write_seed_tree(root: Path) -> None:
    """Minimal defaults tree: one agent tagged 'daimon', one env tagged 'default'."""
    (root / "agents").mkdir(parents=True)
    (root / "environments").mkdir(parents=True)
    (root / "agents" / "daimon.yaml").write_text(f"name: daimon\nmodel: {_MODEL_ID}\n")
    (root / "environments" / "default.yaml").write_text("name: default\n")


def _agent_json(
    agent_id: str, *, metadata: dict[str, Any], archived_at: datetime | None = None
) -> dict[str, Any]:
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=metadata.get("daimon_name", "daimon"),
        model=_AGENT_MODEL,
        metadata=metadata,
        description=None,
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
        archived_at=archived_at,
    ).model_dump(mode="json")


def _env_json(
    env_id: str, *, metadata: dict[str, Any], archived_at: str | None = None
) -> dict[str, Any]:
    return BetaEnvironment(
        id=env_id,
        type="environment",
        name=metadata.get("daimon_name", "default"),
        config=EMPTY_CLOUD_CONFIG,
        metadata=metadata,
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        archived_at=archived_at,
    ).model_dump(mode="json")


class _Workspace:
    """Mutable stand-in for the MA org's agents/environments state."""

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.environments: dict[str, dict[str, Any]] = {}
        self.agent_create_calls: list[dict[str, Any]] = []
        self._agent_n = 0
        self._env_n = 0

    def next_agent_id(self) -> str:
        self._agent_n += 1
        return f"ag_{self._agent_n}"

    def next_env_id(self) -> str:
        self._env_n += 1
        return f"env_{self._env_n}"


def _register_defaults_routes(router: MARouter, workspace: _Workspace) -> None:
    """List/retrieve/create/update/archive routes for skills/environments/agents."""
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response(
            [e for e in workspace.environments.values() if e.get("archived_at") is None]
        ),
    )
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [a for a in workspace.agents.values() if a.get("archived_at") is None]
        ),
    )

    def get_agent(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        agent = workspace.agents.get(m.group(1))
        if agent is None:
            return httpx.Response(
                404,
                json={"type": "error", "error": {"type": "not_found_error", "message": "missing"}},
            )
        return httpx.Response(200, json=agent)

    def get_env(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        env = workspace.environments.get(m.group(1))
        if env is None:
            return httpx.Response(
                404,
                json={"type": "error", "error": {"type": "not_found_error", "message": "missing"}},
            )
        return httpx.Response(200, json=env)

    def create_env(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body: dict[str, Any] = json.loads(req.content)
        env_id = workspace.next_env_id()
        payload = _env_json(env_id, metadata=body.get("metadata", {}))
        workspace.environments[env_id] = payload
        return httpx.Response(200, json=payload)

    def update_env(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        body: dict[str, Any] = json.loads(req.content)
        env_id = m.group(1)
        existing_metadata = workspace.environments[env_id]["metadata"]
        payload = _env_json(env_id, metadata=body.get("metadata", existing_metadata))
        workspace.environments[env_id] = payload
        return httpx.Response(200, json=payload)

    def create_agent(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body: dict[str, Any] = json.loads(req.content)
        agent_id = workspace.next_agent_id()
        payload = _agent_json(agent_id, metadata=body.get("metadata", {}))
        workspace.agents[agent_id] = payload
        workspace.agent_create_calls.append(body)
        return httpx.Response(200, json=payload)

    def update_agent(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        body: dict[str, Any] = json.loads(req.content)
        agent_id = m.group(1)
        existing_metadata = workspace.agents[agent_id]["metadata"]
        payload = _agent_json(agent_id, metadata=body.get("metadata", existing_metadata))
        workspace.agents[agent_id] = payload
        return httpx.Response(200, json=payload)

    def archive_agent(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        agent_id = m.group(1)
        metadata = workspace.agents[agent_id]["metadata"]
        payload = _agent_json(agent_id, metadata=metadata, archived_at=_CREATED_AT)
        workspace.agents[agent_id] = payload
        return httpx.Response(200, json=payload)

    router.add("GET", r"/v1/agents/([^/]+)", get_agent)
    router.add("GET", r"/v1/environments/([^/]+)", get_env)
    router.add("POST", r"/v1/environments", create_env)
    router.add("POST", r"/v1/environments/([^/]+)", update_env)
    router.add("POST", r"/v1/agents", create_agent)
    router.add("POST", r"/v1/agents/([^/]+)/archive", archive_agent)
    router.add("POST", r"/v1/agents/([^/]+)", update_agent)


def _register_turn_routes(
    router: MARouter,
    event_dicts: list[dict[str, Any]],
    *,
    session_id: str = "ses_smoke",
    model_id: str = _MODEL_ID,
    hang: bool = False,
    terminal_less_script: bool = False,
) -> None:
    """POST /v1/sessions (create) + GET stream (SSE) + POST events (send).

    `terminal_less_script=True` additionally registers `GET /v1/sessions/{id}`
    (status "idle", no reopen) and `GET /v1/sessions/{id}/events` (replay,
    the same `event_dicts`) -- required whenever `event_dicts` does not end
    in a real terminal event (`session.status_idle`/`session.status_terminated`).
    A `session.error` event alone is not stream-terminal to the driver's
    consume loop, so the stream ending right after one is a clean close that
    triggers a status check.
    """
    session_json = BetaManagedAgentsSession(
        outcome_evaluations=[],
        id=session_id,
        type="session",
        status="idle",
        environment_id="env_x",
        metadata={},
        resources=[],
        vault_ids=[],
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
        stats=EMPTY_SESSION_STATS,
        usage=EMPTY_SESSION_USAGE,
        agent=BetaManagedAgentsSessionAgent(
            id="agent_x",
            type="agent",
            name="daimon",
            version=1,
            model=BetaManagedAgentsModelConfig(id=model_id),  # type: ignore[arg-type]
            mcp_servers=[],
            tools=[],
            skills=[],
        ),
    ).model_dump(mode="json")

    def handle_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=session_json)

    def handle_stream(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return sse_response(event_dicts)

    async def handle_stream_hang(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        # Never resolves within any test's timeout_s — the point is to give
        # asyncio.timeout something real to cancel.
        await asyncio.sleep(3600)
        return sse_response(event_dicts)  # pragma: no cover — unreachable

    def handle_send(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    router.add("POST", r"/v1/sessions", handle_create)
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events/stream",
        # httpx.MockTransport awaits an async handler's return value even
        # though MARouter's Handler alias is declared sync-only (it works
        # at runtime — see httpx._transports.mock.MockTransport).
        handle_stream_hang if hang else handle_stream,  # pyright: ignore[reportArgumentType]
    )
    router.add("POST", r"/v1/sessions/[^/]+/events", handle_send)
    if terminal_less_script:
        router.add(
            "GET",
            r"/v1/sessions/[^/]+$",
            lambda req, _m: session_response(session_id=session_id, status="idle"),
        )
        router.add(
            "GET",
            r"/v1/sessions/[^/]+/events",
            lambda req, _m: list_response(event_dicts),
        )


def _happy_events(*, id_prefix: str = "evt") -> list[BetaManagedAgentsSessionEvent]:
    """span.model_request_end -> agent.message -> terminal status_idle.

    `id_prefix` must be varied across calls within the same test — MA's
    usage_events write is idempotent on (managed_session_id, event_id), so
    two calls sharing both a session id and event ids would silently dedup
    to a single row and falsely look like a real orchestrator bug.
    """
    return [
        BetaManagedAgentsSpanModelRequestEndEvent(
            id=f"{id_prefix}_span_1",
            type="span.model_request_end",
            processed_at=_CREATED_AT,
            model_request_start_id=f"{id_prefix}_span_start_1",
            model_usage=BetaManagedAgentsSpanModelUsage(
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                input_tokens=10,
                output_tokens=5,
            ),
        ),
        BetaManagedAgentsAgentMessageEvent(
            id=f"{id_prefix}_msg_1",
            type="agent.message",
            processed_at=_CREATED_AT,
            content=[BetaManagedAgentsTextBlock(type="text", text="done")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id=f"{id_prefix}_idle_1",
            type="session.status_idle",
            processed_at=_CREATED_AT,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]


def _build_happy_client(workspace: _Workspace, *, session_id: str = "ses_smoke") -> AsyncAnthropic:
    router = MARouter()
    _register_defaults_routes(router, workspace)
    _register_turn_routes(
        router,
        [e.model_dump(mode="json") for e in _happy_events(id_prefix=session_id)],
        session_id=session_id,
    )
    return build_fake_anthropic(router.dispatch)


async def test_run_smoke_check_writes_usage_event_and_returns_result_when_turn_completes(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _write_seed_tree(tmp_path)
    workspace = _Workspace()
    client = _build_happy_client(workspace)

    result = await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=client,
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="happy-path",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )

    assert result.tail == "done", "tail should reflect the agent.message text"
    assert result.usage_event_count == 1, "exactly one usage_events row should be counted"
    assert result.topped_up is True, "a fresh tenant starts at zero balance and must be topped up"

    async with db_session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=_TENANT_ID)
    assert len(rows) == 1, "exactly one usage_events row must exist for the smoke tenant"


async def test_run_smoke_check_is_idempotent_when_called_twice(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _write_seed_tree(tmp_path)
    workspace = _Workspace()

    first = await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace, session_id="ses_idempotent_1"),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="idempotent-1",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )
    second = await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace, session_id="ses_idempotent_2"),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="idempotent-2",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )

    assert second.tenant_id == first.tenant_id, (
        "two runs must resolve to the same deterministic (cli, smoke) tenant identity"
    )
    assert second.account_id == first.account_id, (
        "two runs must resolve to the same deterministic account identity"
    )
    async with db_session_factory() as s:
        tenant_row = await tenants_store.get_tenant(s, _TENANT_ID)
        account_is_live = await accounts_store.account_exists(s, account_id=first.account_id)
    assert tenant_row is not None, "the (cli, smoke) tenant row must exist after both runs"
    assert account_is_live, "the smoke account row must exist after both runs"


async def test_run_smoke_check_recreates_agent_when_ma_agent_missing(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _write_seed_tree(tmp_path)
    workspace = _Workspace()

    first = await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace, session_id="ses_self_heal_1"),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="self-heal",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )

    # Simulate the smoke tenant's MA agent being wiped from the workspace
    # (e.g. contract.yml archiving it) between runs.
    del workspace.agents[first.agent_id]
    workspace.agent_create_calls.clear()

    second = await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace, session_id="ses_self_heal_2"),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),  # fresh cache: no stale TTL hit masking the miss
        topup_key="self-heal",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )

    assert second.tail == "done", "second run must still complete successfully"
    assert len(workspace.agent_create_calls) >= 1, (
        "self-heal must issue at least one agent create request via the apply_callable branch"
    )


async def test_run_smoke_check_raises_when_no_usage_event_written(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _write_seed_tree(tmp_path)
    workspace = _Workspace()
    router = MARouter()
    _register_defaults_routes(router, workspace)
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_CREATED_AT,
            content=[BetaManagedAgentsTextBlock(type="text", text="done")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_CREATED_AT,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    _register_turn_routes(router, [e.model_dump(mode="json") for e in events])
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(SmokeCheckError, match="no usage_events row"):
        await run_smoke_check(
            session_factory=db_session_factory,
            anthropic=client,
            defaults_root=tmp_path,
            agent_tag="daimon",
            environment_tag="default",
            resolver_cache=new_resolver_cache(),
            topup_key="no-usage-event",
            markup=Decimal("1.0"),
            on_stage=lambda msg: None,
        )


async def test_run_smoke_check_propagates_session_error_when_stream_errors(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `session.error` event is not itself stream-terminal, so the stream
    ending right after it is a clean close: the driver checks MA's session
    status, finalizes via replay-and-refold, and the replayed `session.error`
    folds into `TurnState.error` -- a `TurnError(kind="upstream")`, raised
    as-is by `headless_runner.run_turn` and left unhandled by
    `run_smoke_check` (which only catches its own `TimeoutError`)."""
    _write_seed_tree(tmp_path)
    workspace = _Workspace()
    router = MARouter()
    _register_defaults_routes(router, workspace)
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSessionErrorEvent(
            id="evt_err_1",
            type="session.error",
            processed_at=_CREATED_AT,
            error=BetaManagedAgentsUnknownError(
                type="unknown_error",
                message="boom",
                retry_status=BetaManagedAgentsRetryStatusTerminal(type="terminal"),
            ),
        ),
    ]
    _register_turn_routes(
        router, [e.model_dump(mode="json") for e in events], terminal_less_script=True
    )
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(TurnError) as exc_info:
        await run_smoke_check(
            session_factory=db_session_factory,
            anthropic=client,
            defaults_root=tmp_path,
            agent_tag="daimon",
            environment_tag="default",
            resolver_cache=new_resolver_cache(),
            topup_key="session-error",
            markup=Decimal("1.0"),
            on_stage=lambda msg: None,
        )

    assert exc_info.value.kind == "upstream"
    assert "boom" in exc_info.value.message


async def test_run_smoke_check_inserts_topup_when_balance_zero(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _write_seed_tree(tmp_path)
    workspace = _Workspace()

    result = await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="topup-zero-balance",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )

    assert result.topped_up is True, "zero balance must trigger a top-up"
    async with db_session_factory() as s:
        rows = await tenant_ledger.list_for_tenant(s, tenant_id=_TENANT_ID)
    topup_rows = [r for r in rows if r.reason == "smoke-topup"]
    assert len(topup_rows) == 1, "exactly one smoke-topup ledger row expected"
    assert topup_rows[0].delta_usd == SMOKE_TOPUP_USD, "top-up amount must equal SMOKE_TOPUP_USD"


async def test_run_smoke_check_skips_topup_when_balance_positive(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.defaults.provisioning import provision_tenant

    _write_seed_tree(tmp_path)
    workspace = _Workspace()
    await provision_tenant(db_session_factory, platform="cli", workspace_id="smoke")
    async with db_session_factory() as s, s.begin():
        await tenant_ledger.insert_entry(
            s,
            tenant_id=_TENANT_ID,
            delta_usd=Decimal("10.00"),
            reason="test-preseed",
            idempotency_key="preseed-positive-balance",
        )

    result = await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="skip-topup",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )

    assert result.topped_up is False, "a positive balance must not trigger a top-up"
    async with db_session_factory() as s:
        rows = await tenant_ledger.list_for_tenant(s, tenant_id=_TENANT_ID)
    assert not any(r.reason == "smoke-topup" for r in rows), (
        "no smoke-topup row should be inserted when the balance is already positive"
    )


async def test_run_smoke_check_does_not_double_credit_when_topup_key_repeats(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _write_seed_tree(tmp_path)
    workspace = _Workspace()

    await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace, session_id="ses_repeat_1"),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="repeat-key",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )

    # Drain the balance back to <= 0 so the second call's top-up floor check
    # fires again and actually re-attempts the insert under the same key.
    async with db_session_factory() as s, s.begin():
        balance = await tenant_ledger.get_balance(s, tenant_id=_TENANT_ID)
        if balance > Decimal("0"):
            await tenant_ledger.insert_entry(
                s,
                tenant_id=_TENANT_ID,
                delta_usd=-balance,
                reason="test-drain",
                idempotency_key="drain-for-repeat-test",
            )

    second = await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace, session_id="ses_repeat_2"),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="repeat-key",
        markup=Decimal("1.0"),
        on_stage=lambda msg: None,
    )

    assert second.topped_up is False, (
        "re-using the same topup_key must be reported as a no-op insert (idempotency_key conflict)"
    )
    async with db_session_factory() as s:
        rows = await tenant_ledger.list_for_tenant(s, tenant_id=_TENANT_ID)
    topup_rows = [r for r in rows if r.reason == "smoke-topup"]
    assert len(topup_rows) == 1, "repeating topup_key must not insert a second smoke-topup row"


async def test_run_smoke_check_raises_timeout_error_when_turn_exceeds_timeout(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _write_seed_tree(tmp_path)
    workspace = _Workspace()
    router = MARouter()
    _register_defaults_routes(router, workspace)
    _register_turn_routes(router, [], hang=True)
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(SmokeCheckError, match="timed out"):
        await run_smoke_check(
            session_factory=db_session_factory,
            anthropic=client,
            defaults_root=tmp_path,
            agent_tag="daimon",
            environment_tag="default",
            resolver_cache=new_resolver_cache(),
            topup_key="timeout-case",
            markup=Decimal("1.0"),
            timeout_s=0.05,
            on_stage=lambda msg: None,
        )


async def test_run_smoke_check_reports_each_stage_via_on_stage(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _write_seed_tree(tmp_path)
    workspace = _Workspace()
    stages: list[str] = []

    await run_smoke_check(
        session_factory=db_session_factory,
        anthropic=_build_happy_client(workspace),
        defaults_root=tmp_path,
        agent_tag="daimon",
        environment_tag="default",
        resolver_cache=new_resolver_cache(),
        topup_key="stage-reporting",
        markup=Decimal("1.0"),
        on_stage=stages.append,
    )

    assert len(stages) >= 6, f"expected at least 6 stage reports, got {stages!r}"
    joined = " ".join(stages).lower()
    for keyword in ("tenant", "balance", "agent", "environment", "turn", "usage_events"):
        assert keyword in joined, f"stage reports must mention {keyword!r}: {stages!r}"
