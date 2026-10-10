"""Unit tests for daimon.core.turn.prepare.bind_session -- the D-01 stage-two
chokepoint: thread-session find-or-create, the single shared `create_session`
call, the `thread_sessions` mapping write, and recorder binding.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from cryptography.fernet import Fernet
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.config import McpSettings
from daimon.core.errors import TurnError
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.session_compat import ChangeReason
from daimon.core.session_snapshot import (
    SessionSnapshot,
    fingerprint_identity,
    fingerprint_mutable,
    hash_mcp_servers,
    hash_skills,
    hash_tools,
)
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.domain import AccountRow, TenantRow, ThreadSessionRow, TransferKind
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
    mark_turn_active,
)
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import SessionBusyError
from daimon.core.turn.prepare import ContinuityOutcome, PreparedTurn, bind_session
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)
from daimon.testing.ma_models import (
    ma_agent,
    ma_environment,
    ma_model_usage,
    ma_session,
    ma_session_agent,
)
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daimon.testing.factories import (  # isort: skip
    make_account,
    make_tenant,
    make_thread_session,
)

_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _admission(
    *, account_id: uuid.UUID, agent: BetaManagedAgentsAgent, env: BetaEnvironment
) -> Admission:
    return Admission(
        account_id=account_id,
        agent=agent,
        environment=env,
        config=ResolvedConfig(agent_name="daimon", environment_name="default"),
    )


def _snapshot(*, ma_agent_id: str, model_id: str, environment_id: str = "env_1") -> SessionSnapshot:
    """The configuration a session created from `_agent`/`_env` would freeze."""
    return SessionSnapshot(
        ma_agent_id=ma_agent_id,
        model_id=model_id,
        system_sha256=None,
        skills_sha256=hash_skills([]),
        environment_id=environment_id,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        tools_sha256=hash_tools([]),
        mcp_servers_sha256=hash_mcp_servers([]),
        env_sha256=None,
        agent_version=1,
        agent_name="daimon",
    )


async def _make_snapshotted_thread_session(
    session: AsyncSession,
    *,
    tenant: TenantRow,
    account: AccountRow,
    thread_id: str,
    ma_session_id: str,
    ma_agent_id: str,
    model_id: str = "claude-sonnet-4-6",
    environment_id: str = "env_1",
    watermark_message_id: str | None = None,
    active_turn: bool = False,
) -> ThreadSessionRow:
    """A live mapping row that already records what its session runs.

    `make_thread_session` writes the pre-continuity shape (no
    `effective_config`), which now costs a backfilling `sessions.retrieve` on
    reuse. Tests about anything else use this instead, so their routers stay
    about the thing they test, and `environment_id` must match the admission's
    environment or the bind reads the row as configuration drift.

    `active_turn` marks the row as mid-turn, which defers every configuration
    change to the caller's next message — the state a test needs when it wants
    the drifted session reused rather than replaced.
    """
    snapshot = _snapshot(ma_agent_id=ma_agent_id, model_id=model_id, environment_id=environment_id)
    row = await create_thread_session(
        session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id=thread_id,
        account_id=account.id,
        ma_session_id=ma_session_id,
        ma_agent_id=ma_agent_id,
        watermark_message_id=watermark_message_id,
        effective_config=snapshot,
        identity_fingerprint=fingerprint_identity(snapshot),
        mutable_fingerprint=fingerprint_mutable(snapshot),
    )
    if active_turn:
        await mark_turn_active(
            session, id=row.id, active_turn_message_id="msg-in-flight", now=datetime.now(UTC)
        )
    return row


def _router_with_session_create(*, session_bodies: list[dict[str, object]]) -> MARouter:
    """A router serving the memory-store cold-provision path + session-create,
    which is all `create_session` needs when no vault/env-secrets/repo-binding
    are configured. Captures each session-create request body."""
    router = MARouter()
    memory_handler = make_fake_memory_store_handler()

    def _memory(request: httpx.Request, _match: object) -> httpx.Response:
        return memory_handler(request)

    router.add("POST", r"/v1/memory_stores", _memory)

    def _session_create(request: httpx.Request, _match: object) -> httpx.Response:
        import json

        body = json.loads(request.content)
        session_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "id": f"sess_{len(session_bodies)}",
                "type": "session",
                "agent": {
                    "id": body["agent"],
                    "mcp_servers": [],
                    "model": {"id": "claude-sonnet-4-6"},
                    "name": "daimon",
                    "skills": [],
                    "tools": [],
                    "type": "agent",
                    "version": 1,
                },
                "created_at": "2026-07-28T00:00:00Z",
                "outcome_evaluations": [],
                "environment_id": body["environment_id"],
                "metadata": {},
                "resources": [],
                "stats": {},
                "status": "idle",
                "updated_at": "2026-07-28T00:00:00Z",
                "usage": {},
                "vault_ids": [],
            },
        )

    router.add("POST", r"/v1/sessions", _session_create)
    return router


def _deps(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> TurnDeps:
    return TurnDeps(
        anthropic=build_fake_anthropic(router.dispatch),
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


async def test_bind_session_reuses_live_row_when_reuse_existing_true_and_row_exists(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await _make_snapshotted_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        thread_id="thread-1",
        ma_session_id="sess_existing",
        ma_agent_id="ag_1",
        watermark_message_id="msg-99",
    )
    await db_session.commit()

    def _explode(_request: httpx.Request, _match: object) -> httpx.Response:
        raise AssertionError("create_session must not be called on a reuse hit")

    router = MARouter()
    router.add("POST", r"/v1/sessions", _explode)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-1",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert prepared.reused is True, "a live row hit must set reused=True"
    assert prepared.ma_session_id == "sess_existing", "must return the row's ma_session_id"
    assert prepared.mapping_id == row.id, "must return the row's id as mapping_id"
    assert prepared.watermark == "msg-99", "must return the row's watermark_message_id"


@pytest.mark.parametrize("seal_id", [None, "vault"], ids=["open", "sealed"])
async def test_bind_session_stamps_the_seal_on_a_session_reused_after_sealing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    seal_id: str | None,
) -> None:
    """A thread whose channel was sealed mid-conversation keeps its session: the
    seal must reach that session's stamp, or unsealing later would open a
    transcript written under the seal."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _make_snapshotted_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        thread_id="thread-1",
        ma_session_id="sess_existing",
        ma_agent_id="ag_1",
        watermark_message_id="msg-99",
    )
    await db_session.commit()
    updates: list[dict[str, object]] = []

    def _update(request: httpx.Request, _match: object) -> httpx.Response:
        updates.append(json.loads(request.content))
        return httpx.Response(200, json=ma_session(id="sess_existing").model_dump(mode="json"))

    router = MARouter()
    router.add("POST", r"/v1/sessions/sess_existing$", _update)
    router.add(
        "GET",
        r"/v1/sessions/sess_existing$",
        lambda _r, _m: httpx.Response(
            200, json=ma_session(id="sess_existing").model_dump(mode="json")
        ),
    )
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = dataclasses.replace(
        _admission(account_id=account.id, agent=agent, env=env),
        origin_channel_id="vault",
        origin_thread_id="thread-1",
        origin_seal_ids=frozenset() if seal_id is None else frozenset({seal_id}),
        memory_read_only=seal_id is not None,
    )

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-1",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert prepared.reused is True
    if seal_id is None:
        assert updates == [], "an open turn must not touch the session's metadata"
    else:
        assert [u.get("metadata") for u in updates] == [
            {"daimon_channel": "vault", "daimon_thread": "thread-1", "daimon_sealed": "vault"}
        ]


async def test_bind_session_blocks_a_sealed_turn_ma_will_not_stamp_mid_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Running the turn unstamped is the leak; wait for the session instead."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _make_snapshotted_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        thread_id="thread-1",
        ma_session_id="sess_existing",
        ma_agent_id="ag_1",
        watermark_message_id="msg-99",
    )
    await db_session.commit()
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/sess_existing$",
        lambda _r, _m: httpx.Response(
            200, json=ma_session(id="sess_existing").model_dump(mode="json")
        ),
    )
    router.add(
        "POST",
        r"/v1/sessions/sess_existing$",
        lambda _r, _m: httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "Cannot update session while session is running",
                },
            },
        ),
    )
    deps = _deps(sessionmaker=db_session_factory, router=router)
    admission = dataclasses.replace(
        _admission(
            account_id=account.id,
            agent=ma_agent(id="ag_1", tenant_id=tenant.id),
            env=ma_environment(id="env_1", tenant_id=tenant.id),
        ),
        origin_channel_id="vault",
        origin_thread_id="thread-1",
        origin_seal_ids=frozenset({"vault"}),
        memory_read_only=True,
    )

    with pytest.raises(SessionBusyError):
        await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
        )


async def test_bind_session_creates_session_and_writes_mapping_when_no_live_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router_with_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-2",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert len(session_bodies) == 1, "no live row must trigger exactly one create_session call"
    assert prepared.reused is False, "no live row must set reused=False"
    assert prepared.watermark is None, "a fresh session has no watermark yet"
    assert prepared.mapping_id is not None, "a new thread_sessions row must be written"

    async with db_session_factory() as s:
        from daimon.core.stores.thread_sessions import get_live_thread_session

        live = await get_live_thread_session(
            s,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-2",
            account_id=account.id,
        )
    assert live is not None, "the mapping write must be visible via get_live_thread_session"
    assert live.ma_session_id == prepared.ma_session_id, (
        "the persisted row's ma_session_id must match the returned PreparedTurn"
    )


async def test_bind_session_stamps_the_budget_channel_apart_from_the_origin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A DM turn runs in the DM but bills the channel it was opened from."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    session_bodies: list[dict[str, object]] = []
    deps = _deps(
        sessionmaker=db_session_factory,
        router=_router_with_session_create(session_bodies=session_bodies),
    )
    admission = dataclasses.replace(
        _admission(
            account_id=account.id,
            agent=ma_agent(id="ag_1", tenant_id=tenant.id),
            env=ma_environment(id="env_1", tenant_id=tenant.id),
        ),
        origin_channel_id="dm-chan",
        budget_channel_id="chan-2",
    )

    await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="dm:1",
        session_account_id=account.id,
        reuse_existing=True,
    )

    metadata = session_bodies[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata.get("daimon_channel") == "dm-chan"
    assert metadata.get("daimon_budget_channel") == "chan-2"


async def test_bind_session_always_creates_fresh_session_when_reuse_existing_false(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    # A live row DOES exist for this thread -- but reuse_existing=False must
    # ignore it (Discord's channel-mention path).
    await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-3",
        ma_session_id="sess_should_not_be_reused",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router_with_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-3",
        session_account_id=account.id,
        reuse_existing=False,
    )

    assert len(session_bodies) == 1, "reuse_existing=False must always create a fresh session"
    assert prepared.reused is False, "reuse_existing=False must report reused=False"
    assert prepared.ma_session_id != "sess_should_not_be_reused", (
        "the existing live row must be ignored, not reused"
    )


async def test_bind_session_recorder_writes_usage_event_matching_ma_session_id(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router_with_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-4",
        session_account_id=account.id,
        reuse_existing=True,
    )

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_1",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=ma_model_usage(input_tokens=10, output_tokens=20),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    await prepared._record(event=event)  # pyright: ignore[reportPrivateUsage]

    async with db_session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
    assert len(rows) == 1, "the bound recorder must write exactly one usage_events row"
    assert rows[0].managed_session_id == prepared.ma_session_id, (
        "the recorded managed_session_id must equal bind_session's returned ma_session_id"
    )


def test_prepared_turn_public_fields_exclude_recorder() -> None:
    field_names = {f.name for f in dataclasses.fields(PreparedTurn)}
    public_field_names = {name for name in field_names if not name.startswith("_")}
    assert "record" not in public_field_names, (
        "PreparedTurn's public field set must not expose the recorder"
    )
    assert public_field_names == {
        "admission",
        "ma_session_id",
        "mapping_id",
        "watermark",
        "reused",
        "session_account_id",
        "continuity",
    }, "PreparedTurn must expose exactly the documented public fields"


async def test_bind_session_syncs_agent_mcp_credential_into_a_reused_session_vault(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The regression that made an agent-level MCP server one person's private
    tool: a reused session skips create_session, so a credential added to the
    agent after that session existed never reached this caller and every one of
    their turns died at MCP init. The admin adds it once; this caller does
    nothing and their live session picks it up on the next turn."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _make_snapshotted_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        thread_id="thread-reuse-mcp",
        ma_session_id="sess_live",
        ma_agent_id="ag_reuse",
        environment_id="env_reuse",
        watermark_message_id="msg-1",
    )
    await db_session.commit()

    public_url = "https://mcp.example.com/mcp"
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    agent = ma_agent(id="ag_reuse", tenant_id=tenant.id)
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_reuse")

    # Someone else (the admin) attached the server and stored the token.
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        mcp_server_url="https://internal.example.com/mcp",
        plaintext_token="tok_internal",
    )

    display = f"daimon-mcp:{account.id}:{agent_uuid}"
    created: list[dict[str, object]] = []
    deleted: list[str] = []

    router = MARouter()

    def _explode(_request: httpx.Request, _match: object) -> httpx.Response:
        raise AssertionError("create_session must not be called on a reuse hit")

    def _vaults(_request: httpx.Request, _match: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vlt_caller",
                        "type": "vault",
                        "display_name": display,
                        "metadata": None,
                        "archived_at": None,
                        "created_at": "2026-04-01T00:00:00Z",
                    }
                ],
                "has_more": False,
            },
        )

    def _list_creds(_request: httpx.Request, _match: object) -> httpx.Response:
        # Only this caller's own daimon-mcp credential — nothing for the
        # external server, which is the state that used to fail the turn.
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vcrd_daimon_mcp",
                        "metadata": {"daimon_chat_identity": str(agent_uuid)},
                        "type": "credential",
                        "vault_id": "vlt_caller",
                        "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                    }
                ],
                "has_more": False,
            },
        )

    def _create_cred(request: httpx.Request, _match: object) -> httpx.Response:
        import json

        body = json.loads(request.content)
        created.append(body)
        return httpx.Response(
            200,
            json={
                "id": "vcrd_new",
                "type": "credential",
                "vault_id": "vlt_caller",
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": body["auth"]["mcp_server_url"],
                },
            },
        )

    def _delete_cred(request: httpx.Request, _match: object) -> httpx.Response:
        deleted.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"id": "vcrd_gone", "type": "credential"})

    router.add("POST", r"/v1/sessions", _explode)
    router.add("GET", r"/v1/vaults$", _vaults)
    router.add("GET", r"/v1/vaults/vlt_caller/credentials", _list_creds)
    router.add("POST", r"/v1/vaults/vlt_caller/credentials", _create_cred)
    router.add("DELETE", r"/v1/vaults/vlt_caller/credentials/.*", _delete_cred)

    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        anthropic=build_fake_anthropic(router.dispatch),
        fernet=fernet,
        mcp=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
    )
    admission = _admission(
        account_id=account.id, agent=agent, env=ma_environment(id="env_reuse", tenant_id=tenant.id)
    )

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-not-the-admin",
        thread_id="thread-reuse-mcp",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert prepared.reused is True, "this must still be the reuse path"
    urls = [c["auth"]["mcp_server_url"] for c in created]  # pyright: ignore[reportIndexIssue]
    assert urls == ["https://internal.example.com/mcp"], (
        "the agent's external MCP credential must be created in this caller's vault"
    )
    assert created[0]["auth"]["token"] == "tok_internal"  # pyright: ignore[reportIndexIssue]
    assert deleted == [], (
        "the sync must never delete — the vault is shared across this caller's "
        "threads and a concurrent turn may be using a credential"
    )


async def test_bind_session_reuse_skips_mcp_sync_when_agent_has_no_stored_credentials(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The common case pays one indexed query and touches MA not at all."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _make_snapshotted_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        thread_id="thread-reuse-plain",
        ma_session_id="sess_live",
        ma_agent_id="ag_plain",
        environment_id="env_plain",
        watermark_message_id="msg-1",
    )
    await db_session.commit()

    router = MARouter()

    def _explode(request: httpx.Request, _match: object) -> httpx.Response:
        raise AssertionError(f"no MA call expected, got {request.method} {request.url.path}")

    router.add("POST", r"/v1/sessions", _explode)
    router.add("GET", r"/v1/vaults$", _explode)

    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        anthropic=build_fake_anthropic(router.dispatch),
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
        mcp=McpSettings(
            jwt_secret=SecretStr("x" * 32), public_url=HttpUrl("https://mcp.example.com/mcp")
        ),
    )
    admission = _admission(
        account_id=account.id,
        agent=ma_agent(id="ag_plain", tenant_id=tenant.id),
        env=ma_environment(id="env_plain", tenant_id=tenant.id),
    )

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-reuse-plain",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert prepared.reused is True, "reuse path unchanged for agents with no external MCP"


async def test_bind_session_fresh_bind_raises_ceiling_error_when_deadline_already_past(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router_with_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    past_deadline = datetime.now(UTC) - timedelta(seconds=5)

    with pytest.raises(TurnError) as exc_info:
        await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-ceiling-fresh",
            session_account_id=account.id,
            reuse_existing=True,
            deadline=past_deadline,
        )

    assert exc_info.value.kind == "ceiling"


async def test_bind_session_ceiling_breach_leaves_no_orphan_mapping_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router_with_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    past_deadline = datetime.now(UTC) - timedelta(seconds=5)

    with pytest.raises(TurnError) as exc_info:
        await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-ceiling-no-orphan",
            session_account_id=account.id,
            reuse_existing=True,
            deadline=past_deadline,
        )

    assert exc_info.value.kind == "ceiling"

    async with db_session_factory() as s:
        live = await get_live_thread_session(
            s,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-ceiling-no-orphan",
            account_id=account.id,
        )
    assert live is None, "a ceiling breach during bind must not leave a partial mapping row"


async def test_bind_session_reuse_path_raises_ceiling_error_when_deadline_already_past(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _make_snapshotted_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        thread_id="thread-ceiling-reuse",
        ma_session_id="sess_existing",
        ma_agent_id="ag_1",
        watermark_message_id="msg-1",
    )
    await db_session.commit()

    router = MARouter()

    def _explode(_request: httpx.Request, _match: object) -> httpx.Response:
        raise AssertionError("create_session must not be called on a reuse hit")

    router.add("POST", r"/v1/sessions", _explode)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    past_deadline = datetime.now(UTC) - timedelta(seconds=5)

    with pytest.raises(TurnError) as exc_info:
        await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-ceiling-reuse",
            session_account_id=account.id,
            reuse_existing=True,
            deadline=past_deadline,
        )

    assert exc_info.value.kind == "ceiling"


async def test_bind_session_default_deadline_none_still_succeeds_on_the_happy_path(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`deadline=None` is fail-safe, not off -- it computes a full
    TURN_CEILING_S-away deadline, so a fast route must still complete
    normally rather than being mistaken for "no ceiling"."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router_with_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-ceiling-default-none",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert len(session_bodies) == 1, "deadline=None must not skip create_session"
    assert prepared.reused is False


async def test_bind_session_with_generous_explicit_deadline_matches_no_deadline_outcome(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression pin: passing a generous, far-future deadline changes nothing
    about the returned PreparedTurn compared to the pre-ceiling behavior."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router_with_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    future_deadline = datetime.now(UTC) + timedelta(hours=1)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-ceiling-generous",
        session_account_id=account.id,
        reuse_existing=True,
        deadline=future_deadline,
    )

    assert len(session_bodies) == 1
    assert prepared.reused is False
    assert prepared.watermark is None
    assert prepared.mapping_id is not None
    assert prepared.ma_session_id == "sess_1"


async def test_bind_recorder_bills_the_session_snapshot_model_when_the_agent_model_changed_after_create(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The live defect this wave closes: an MA session executes the agent it
    froze at creation, so a session created while the agent was on sonnet keeps
    running sonnet after the agent is moved to opus. Billing the agent's
    CURRENT model would price that turn at opus rates for work done at sonnet
    rates.

    A model change now replaces the session — but not while a turn is in
    flight, which is the state here: the replacement waits for the caller's
    next message and this turn still runs, and must still be billed, on the
    session that froze sonnet."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _make_snapshotted_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        thread_id="thread-model-changed",
        ma_session_id="sess_frozen_on_sonnet",
        ma_agent_id="ag_1",
        model_id="claude-sonnet-4-6",
        active_turn=True,
    )
    await db_session.commit()

    router = MARouter()

    def _explode(request: httpx.Request, _match: object) -> httpx.Response:
        raise AssertionError(f"no MA call expected, got {request.method} {request.url.path}")

    router.add("POST", r"/v1/sessions", _explode)
    router.add("GET", r"/v1/sessions/.*", _explode)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    # The responder agent has since been moved to opus.
    agent = ma_agent(id="ag_1", tenant_id=tenant.id, model="claude-opus-5")
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-model-changed",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert prepared.reused is True, "this must be the reuse path"

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_1",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    await prepared._record(event=event)  # pyright: ignore[reportPrivateUsage]

    async with db_session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
        entries = await tenant_ledger.list_for_tenant(s, tenant_id=tenant.id)
    assert [row.model for row in rows] == ["claude-sonnet-4-6"], (
        "usage must record the model the reused session actually runs, not the agent's new one"
    )
    debits = [entry.delta_usd for entry in entries if entry.reason == "turn_debit"]
    assert debits == [Decimal("-3.000000")], (
        "1M input tokens must be debited at the sonnet rate the session ran, not opus's"
    )


async def test_fresh_session_records_a_snapshot_with_fingerprints(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The snapshot comes from what `sessions.create` returned — the authority
    on what the session will execute — not from the agent we asked for."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router_with_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    # The router's created session reports sonnet whatever we ask for, so an
    # opus agent here proves the recorded model is read off the response.
    agent = ma_agent(id="ag_1", tenant_id=tenant.id, model="claude-opus-5")
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-fresh-snapshot",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert prepared.reused is False, "this must be the fresh-session path"
    async with db_session_factory() as s:
        live = await get_live_thread_session(
            s,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-fresh-snapshot",
            account_id=account.id,
        )
    assert live is not None, "the fresh session must leave a live mapping row"
    snapshot = live.effective_config
    assert snapshot is not None, "a fresh session must record the configuration it froze"
    assert snapshot.model_id == "claude-sonnet-4-6", (
        "the recorded model must be the created session's, not the agent's current one"
    )
    assert snapshot.ma_agent_id == "ag_1", "the snapshot must name the agent the session froze"
    assert snapshot.environment_id == "env_1", "the snapshot must record the session's environment"
    assert live.identity_fingerprint == fingerprint_identity(snapshot), (
        "the stored identity fingerprint must be the one this snapshot hashes to"
    )
    assert live.mutable_fingerprint == fingerprint_mutable(snapshot), (
        "the stored mutable fingerprint must be the one this snapshot hashes to"
    )


@pytest.mark.parametrize("legacy_ma_agent_id", [None, "ag_1"])
async def test_legacy_row_without_snapshot_is_backfilled_with_one_retrieve(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    legacy_ma_agent_id: str | None,
) -> None:
    """A row written before continuity has no snapshot, so the session is read
    once and the snapshot persisted. Both legacy shapes cost ONE read: with no
    `ma_agent_id` the identity check already fetched the session and hands it
    over; with one, this is the only fetch."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-legacy",
        ma_session_id="sess_legacy",
        ma_agent_id=legacy_ma_agent_id,
    )
    # The session this row points at froze a different model and environment,
    # so without a turn in flight the bind would replace it rather than read
    # it. The marker defers that to the caller's next message and leaves this
    # test about the backfill.
    await mark_turn_active(
        db_session, id=row.id, active_turn_message_id="msg-in-flight", now=datetime.now(UTC)
    )
    await db_session.commit()
    assert row.effective_config is None, "the legacy shape is a row with no recorded configuration"

    now = datetime.now(UTC)
    observed = ma_session(
        id="sess_legacy",
        agent=ma_session_agent(id="ag_1", name="daimon", version=3, model="claude-haiku-4-5"),
        environment_id="env_legacy",
        vault_ids=["vlt_legacy"],
        created_at=now,
    )
    retrieves: list[str] = []

    def _retrieve(request: httpx.Request, _match: object) -> httpx.Response:
        retrieves.append(request.url.path)
        return httpx.Response(200, json=observed.model_dump(mode="json"))

    def _explode(request: httpx.Request, _match: object) -> httpx.Response:
        raise AssertionError(f"unexpected MA call: {request.method} {request.url.path}")

    router = MARouter()
    router.add("GET", r"/v1/sessions/sess_legacy", _retrieve)
    router.add("POST", r"/v1/sessions", _explode)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id, model="claude-opus-5")
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-legacy",
        session_account_id=account.id,
        reuse_existing=True,
    )

    assert prepared.reused is True, "a legacy row is still reused"
    assert len(retrieves) == 1, "a legacy row must cost exactly one sessions.retrieve"

    async with db_session_factory() as s:
        backfilled = await get_thread_session_by_id(s, id=row.id)
    assert backfilled is not None, "the backfill must not replace the row"
    snapshot = backfilled.effective_config
    assert snapshot is not None, "the retrieved configuration must be persisted on the row"
    assert snapshot.model_id == "claude-haiku-4-5", (
        "the backfilled model must be the session's own, not the agent's current one"
    )
    assert snapshot.environment_id == "env_legacy", "the backfill records the session's environment"
    assert snapshot.vault_id == "vlt_legacy", "the backfill records the session's vault"
    assert backfilled.identity_fingerprint == fingerprint_identity(snapshot), (
        "the backfill must store the identity fingerprint alongside the snapshot"
    )
    assert backfilled.mutable_fingerprint == fingerprint_mutable(snapshot), (
        "the backfill must store the mutable fingerprint alongside the snapshot"
    )

    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_legacy",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    await prepared._record(event=event)  # pyright: ignore[reportPrivateUsage]
    async with db_session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
    assert [usage.model for usage in rows] == ["claude-haiku-4-5"], (
        "a backfilled row must bill the model the session was found to be running"
    )


async def test_bind_session_raises_session_busy_when_a_handoff_lands_mid_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The destination agent's turn must not run inside the source agent's
    session. `bind_session` turns that into an error rather than a
    `PreparedTurn`, so an adapter cannot accidentally run the turn anyway."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await _make_snapshotted_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        thread_id="thread-handoff-busy",
        ma_session_id="sess_source",
        ma_agent_id="ag_source",
        active_turn=True,
    )
    binding = await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id="thread-handoff-busy",
        responder_ma_agent_id="ag_destination",
        responder_name="research-bot",
        kind="handoff",
    )
    await db_session.commit()

    def _explode(request: httpx.Request, _match: object) -> httpx.Response:
        raise AssertionError(f"unexpected MA call: {request.method} {request.url.path}")

    router = MARouter()
    router.add("POST", r"/v1/sessions", _explode)
    router.add("GET", r"/v1/sessions/sess_source", _explode)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    destination = Admission(
        account_id=account.id,
        agent=ma_agent(id="ag_destination", tenant_id=tenant.id, name="research-bot"),
        environment=ma_environment(id="env_1", tenant_id=tenant.id),
        config=ResolvedConfig(
            agent_name="research-bot",
            environment_name="default",
            thread_binding_id=binding.id,
        ),
    )

    before = datetime.now(UTC)
    with pytest.raises(SessionBusyError) as caught:
        await bind_session(
            deps,
            destination,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-handoff-busy",
            session_account_id=account.id,
            reuse_existing=True,
        )

    assert caught.value.pending_reasons == ("agent_identity",), (
        "the adapter is told which change is waiting, so it can say what happens next"
    )
    assert caught.value.retry_after > before, "and when it will be made"

    async with db_session_factory() as s:
        untouched = await get_thread_session_by_id(s, id=row.id)
    assert untouched is not None and untouched.status == "live", (
        "the source agent's session keeps running its own turn"
    )


@pytest.mark.parametrize(
    ("applied", "transfer_kind", "announced"),
    [
        # The prod cases: MA echoing a resolved skill version, and the person
        # connecting GitHub (rid 01M4J0N1QH5HBFK4V36MZ2XQZ5), both carried in full.
        (("skills",), "full", False),
        (("repo_set",), "full", False),
        (("github_mode", "repo_set", "repo_url", "repo_branch"), "full", False),
        (("system_prompt", "tools", "mcp_servers", "env_file", "repo_token_age"), "full", False),
        (("memory_store", "memory_access", "vault", "environment"), "full", False),
        ((), "full", False),
        # A different agent or model answering is noticed, so it is said.
        (("model",), "full", True),
        (("agent_identity",), "full", True),
        (("skills", "model"), "full", True),
        # Anything lost is always said, whatever the reason.
        (("skills",), "partial", True),
        (("skills",), "transcript", True),
        (("repo_set",), "history", True),
    ],
)
def test_replacement_notice_is_only_for_changes_a_person_notices(
    applied: tuple[ChangeReason, ...], transfer_kind: TransferKind, announced: bool
) -> None:
    outcome = ContinuityOutcome(state="replaced", applied=applied, transfer_kind=transfer_kind)

    assert outcome.announces_replacement() is announced


def test_no_replacement_notice_without_a_replacement_or_a_transfer() -> None:
    assert not ContinuityOutcome().announces_replacement()
    assert not ContinuityOutcome(state="replaced", applied=("model",)).announces_replacement()
