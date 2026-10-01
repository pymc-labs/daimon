"""What each in-place op actually sends to a live MA session.

Driven through the stateful sessions fake at the transport level, so every
request goes through the real SDK's parameter validation and response parsing
— a method-level mock would accept a `sessions.update` body that MA rejects.
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_custom_tool import BetaManagedAgentsCustomTool
from anthropic.types.beta.beta_managed_agents_custom_tool_input_schema import (
    BetaManagedAgentsCustomToolInputSchema,
)
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.sessions.beta_managed_agents_file_resource import (
    BetaManagedAgentsFileResource,
)
from daimon.core.config import McpSettings
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.credential_requests import mint_request_token
from daimon.core.session_compat import (
    RemirrorVaultCredentials,
    ReplaceEnvFile,
    ReplaceToolsAndMcpServers,
    RotateRepoToken,
)
from daimon.core.session_snapshot import (
    SessionSnapshot,
    hash_env_bytes,
    hash_mcp_servers,
    hash_tools,
    snapshot_from_created_session,
)
from daimon.core.session_update_ops import (
    AppliedOps,
    EnvMountLost,
    SessionBusy,
    apply_update_ops,
)
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.agent_files import list_agent_files, put_agent_file
from daimon.core.stores.domain import RepoAccessProof
from daimon.core.stores.pending_file_deletes import list_due_pending_file_deletes
from daimon.testing.factories import make_agent_repo_binding, make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
)
from daimon.testing.ma_models import ma_agent
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
_AGENT_ID = "ag_update_ops"
_ENV_ID = "env_update_ops"


def _agent(
    *,
    tools: list[BetaManagedAgentsCustomTool] | None = None,
    mcp_servers: list[BetaManagedAgentsMCPServerURLDefinition] | None = None,
) -> BetaManagedAgentsAgent:
    return ma_agent(
        id=_AGENT_ID,
        name="daimon",
        tools=tools or [],
        mcp_servers=mcp_servers or [],
        version=2,
        created_at=_NOW,
    )


def _register_agent(ma_state: FakeMAState) -> None:
    ma_state.agents[_AGENT_ID] = {
        "id": _AGENT_ID,
        "type": "agent",
        "name": "daimon",
        "version": 1,
        "model": {"id": "claude-sonnet-4-6"},
        "system": None,
        "description": None,
        "metadata": {},
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }


def _build_client(state: FakeSessionsState, *, before: list[Any] | None = None) -> AsyncAnthropic:
    handlers = list(before or [])
    handlers.extend([make_fake_sessions_handler(state), make_fake_ma_handler(state.ma)])
    return build_fake_anthropic(combine_handlers(*handlers))


async def _session_with_env(
    client: AsyncAnthropic, *, content: bytes
) -> tuple[str, SessionSnapshot]:
    """A live session with one mounted `.env`, plus the snapshot recording it."""
    uploaded = await client.beta.files.upload(file=(".env", io.BytesIO(content), "text/plain"))
    created = await client.beta.sessions.create(
        agent=_AGENT_ID,
        environment_id=_ENV_ID,
        resources=[{"type": "file", "file_id": uploaded.id, "mount_path": ".env"}],
    )
    snapshot = snapshot_from_created_session(
        created,
        env_sha256=hash_env_bytes(content),
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id=None,
    )
    return created.id, snapshot


async def _apply(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    ops: tuple[Any, ...],
    session_id: str,
    recorded: SessionSnapshot,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    agent: BetaManagedAgentsAgent | None = None,
    account_id: uuid.UUID | None = None,
) -> AppliedOps | SessionBusy:
    return await apply_update_ops(
        client,
        sessionmaker,
        ops=ops,
        session_id=session_id,
        recorded=recorded,
        agent=agent if agent is not None else _agent(),
        tenant_id=tenant_id,
        agent_uuid=agent_uuid,
        account_id=account_id if account_id is not None else uuid.uuid4(),
        mcp=McpSettings(),
        fernet=None,
        github_fallback_pat="ghp_fallback",
        github_app_id=None,
        github_app_private_key=None,
        now=_NOW,
    )


async def test_tools_update_sends_both_full_arrays_and_never_vault_ids(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)
    bodies: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.count("/") == 3:
            import json

            bodies.append(json.loads(request.content))
        raise NotHandled

    client = _build_client(state, before=[_capture])
    session_id, recorded = await _session_with_env(client, content=b"A=1\n")

    tool = BetaManagedAgentsCustomTool(
        description="search the corpus",
        input_schema=BetaManagedAgentsCustomToolInputSchema(type="object"),
        name="search",
        type="custom",
    )
    server = BetaManagedAgentsMCPServerURLDefinition(
        name="linear", type="url", url="https://mcp.example/linear"
    )
    agent = _agent(tools=[tool], mcp_servers=[server])

    result = await _apply(
        client,
        db_session_factory,
        ops=(ReplaceToolsAndMcpServers(),),
        session_id=session_id,
        recorded=recorded,
        tenant_id=tenant.id,
        agent_uuid=uuid.uuid4(),
        agent=agent,
    )

    assert isinstance(result, AppliedOps), "an idle session accepts the agent update"
    update_bodies = [b for b in bodies if "agent" in b]
    assert len(update_bodies) == 1, "the op must send exactly one sessions.update"
    sent = update_bodies[0]["agent"]
    assert [t["name"] for t in sent["tools"]] == ["search"], (
        "the update must carry the agent's whole tool array, not a patch"
    )
    assert [s["name"] for s in sent["mcp_servers"]] == ["linear"], (
        "the update must carry the whole mcp_servers array alongside tools"
    )
    assert "vault_ids" not in update_bodies[0], "MA rejects vault_ids on update; never send it"
    assert result.snapshot.tools_sha256 == hash_tools([tool]), (
        "the returned snapshot must hash the tools the session now runs"
    )
    assert result.snapshot.mcp_servers_sha256 == hash_mcp_servers([server]), (
        "the returned snapshot must hash the mcp_servers the session now runs"
    )
    assert result.applied == ("tools", "mcp_servers"), "the op reports both axes it replaced"


async def test_env_replacement_deletes_the_old_resource_before_adding_the_new_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        key="TOGGL_TOKEN",
        content="tok",
        set_by_account_id=None,
    )
    await db_session.commit()

    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)
    calls: list[tuple[str, str]] = []

    def _record(request: httpx.Request) -> httpx.Response:
        if "/resources" in request.url.path:
            calls.append((request.method, request.url.path))
        raise NotHandled

    client = _build_client(state, before=[_record])
    session_id, recorded = await _session_with_env(client, content=b"OLD=1\n")
    old_resource_id = recorded.env_resource_id
    assert old_resource_id is not None, "the created session must carry a .env resource"

    result = await _apply(
        client,
        db_session_factory,
        ops=(ReplaceEnvFile(old_resource_id=old_resource_id, old_file_id=recorded.env_file_id),),
        session_id=session_id,
        recorded=recorded,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
    )

    assert isinstance(result, AppliedOps), "the swap must succeed on an idle session"
    assert [method for method, _ in calls] == ["DELETE", "POST"], (
        "MA rejects a second resource at an occupied mount path: delete, then add"
    )
    assert calls[0][1].endswith(old_resource_id), "the delete must name the recorded resource"

    async with db_session_factory() as session:
        rows = await list_agent_files(session, tenant_id=tenant.id, agent_id=agent_uuid)
    assert result.snapshot.env_sha256 == hash_env_bytes(assemble_env_bytes(rows)), (
        "the snapshot must record the hash of the bytes now mounted"
    )
    assert result.snapshot.env_resource_id is not None, (
        "the snapshot must record the new resource so the next swap can delete it"
    )
    assert result.snapshot.env_resource_id != old_resource_id, (
        "the new mount is a different resource"
    )

    mounted = [
        resource
        for resource in state.resources[session_id]
        if isinstance(resource, BetaManagedAgentsFileResource)
    ]
    assert len(mounted) == 1, "the session must end with exactly one .env mounted"
    assert state.sandbox[session_id]["/mnt/session/uploads/.env"] == b"TOGGL_TOKEN=tok\n", (
        "the session must be able to read the new secrets at the same path"
    )


async def test_env_replacement_enqueues_the_new_file_for_ttl_deletion(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        key="KEY",
        content="v",
        set_by_account_id=None,
    )
    await db_session.commit()

    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)
    client = _build_client(state)
    session_id, recorded = await _session_with_env(client, content=b"OLD=1\n")

    result = await _apply(
        client,
        db_session_factory,
        ops=(ReplaceEnvFile(old_resource_id=recorded.env_resource_id, old_file_id=None),),
        session_id=session_id,
        recorded=recorded,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
    )
    assert isinstance(result, AppliedOps)

    async with db_session_factory() as session:
        due = await list_due_pending_file_deletes(session, now=datetime.now(UTC).replace(year=2030))
    assert [row.file_id for row in due] == [result.snapshot.env_file_id], (
        "the uploaded .env is disposable and must be queued for deletion like every other"
    )


async def test_env_replacement_finds_the_mounted_env_when_no_resource_id_was_recorded(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A backfilled snapshot may not name the resource; the session does."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        key="KEY",
        content="v",
        set_by_account_id=None,
    )
    await db_session.commit()

    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)
    client = _build_client(state)
    session_id, recorded = await _session_with_env(client, content=b"OLD=1\n")
    old_resource_id = recorded.env_resource_id

    result = await _apply(
        client,
        db_session_factory,
        ops=(ReplaceEnvFile(old_resource_id=None, old_file_id=None),),
        session_id=session_id,
        recorded=recorded.model_copy(update={"env_resource_id": None}),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
    )

    assert isinstance(result, AppliedOps)
    remaining = [
        resource.id
        for resource in state.resources[session_id]
        if isinstance(resource, BetaManagedAgentsFileResource)
    ]
    assert old_resource_id not in remaining, "the listed .env mount must have been deleted"
    assert len(remaining) == 1, "and replaced by exactly one new mount"


async def test_env_replacement_clears_the_recorded_env_when_the_add_fails_after_the_delete(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The session now has no `.env`; the snapshot must say so, or the next
    bind would try to delete a resource that is already gone instead of
    mounting the secrets the caller saved."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        key="KEY",
        content="v",
        set_by_account_id=None,
    )
    await db_session.commit()

    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)

    def _refuse_add(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/resources"):
            return httpx.Response(
                500,
                json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
            )
        raise NotHandled

    client = _build_client(state, before=[_refuse_add])
    session_id, recorded = await _session_with_env(client, content=b"OLD=1\n")

    with pytest.raises(EnvMountLost) as raised:
        await _apply(
            client,
            db_session_factory,
            ops=(ReplaceEnvFile(old_resource_id=recorded.env_resource_id, old_file_id=None),),
            session_id=session_id,
            recorded=recorded,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
        )

    assert raised.value.snapshot.env_sha256 is None, (
        "a session with no .env mounted must record no env hash, so the next bind adds one"
    )
    assert raised.value.snapshot.env_resource_id is None, (
        "the deleted resource must not stay on the snapshot"
    )
    assert raised.value.snapshot.env_file_id is None, "nor the file it mounted"


async def test_env_replacement_leaves_no_env_when_the_agents_last_secret_was_removed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)
    client = _build_client(state)
    session_id, recorded = await _session_with_env(client, content=b"OLD=1\n")

    result = await _apply(
        client,
        db_session_factory,
        ops=(ReplaceEnvFile(old_resource_id=recorded.env_resource_id, old_file_id=None),),
        session_id=session_id,
        recorded=recorded,
        tenant_id=tenant.id,
        agent_uuid=uuid.uuid4(),
    )

    assert isinstance(result, AppliedOps)
    assert result.snapshot.env_sha256 is None, "an agent with no secrets mounts no .env"
    assert state.resources[session_id] == [], "the old .env must be gone from the session"


async def test_repo_token_rotation_targets_the_recorded_resource(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await make_agent_repo_binding(
        db_session,
        tenant=tenant,
        agent_id=agent_uuid,
        repo_url="acme/data",
        # Verified-public, so the operator fallback PAT is the authorized
        # credential and the rotation needs no GitHub App round trip.
        proof=RepoAccessProof(kind="public", at=_NOW, account_id=None),
    )
    await db_session.commit()

    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)
    created = await build_fake_anthropic(
        combine_handlers(make_fake_sessions_handler(state), make_fake_ma_handler(state.ma))
    ).beta.sessions.create(
        agent=_AGENT_ID,
        environment_id=_ENV_ID,
        resources=[
            {
                "type": "github_repository",
                "url": "https://github.com/acme/data",
                "authorization_token": "ghp_old",
                "checkout": {"type": "branch", "name": "main"},
            }
        ],
    )
    recorded = snapshot_from_created_session(
        created, env_sha256=None, env_file_id=None, repo_token_issued_at=0, vault_id=None
    )
    assert recorded.repo_resource_id is not None, "the session must carry a repo resource"

    rotations: list[tuple[str, dict[str, Any]]] = []

    def _record(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST" and "/resources/" in request.url.path:
            rotations.append((request.url.path, json.loads(request.content)))
        raise NotHandled

    client = _build_client(state, before=[_record])

    result = await _apply(
        client,
        db_session_factory,
        ops=(RotateRepoToken(resource_id=recorded.repo_resource_id),),
        session_id=created.id,
        recorded=recorded,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
    )

    assert isinstance(result, AppliedOps)
    assert len(rotations) == 1, "rotation must be one resources.update call"
    path, body = rotations[0]
    assert path.endswith(recorded.repo_resource_id), (
        "the rotation must target the repo resource the snapshot recorded"
    )
    assert body["authorization_token"] == "ghp_fallback", (
        "the rotated token must be the one resolve_clone_token minted now"
    )
    assert result.snapshot.repo_token_issued_at == int(_NOW.timestamp()), (
        "the snapshot must record when the new token was issued, so age is measurable"
    )
    assert result.applied == ("repo_token_age",)


async def test_repo_token_rotation_is_skipped_when_the_agent_has_no_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)
    client = _build_client(state)
    session_id, recorded = await _session_with_env(client, content=b"A=1\n")

    result = await _apply(
        client,
        db_session_factory,
        ops=(RotateRepoToken(resource_id="res_gone"),),
        session_id=session_id,
        recorded=recorded,
        tenant_id=tenant.id,
        agent_uuid=uuid.uuid4(),
    )

    assert isinstance(result, AppliedOps)
    assert result.applied == (), "an unbound agent has no token to rotate"


async def test_update_defers_as_session_busy_when_ma_refuses_a_running_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MA's own serialization (capability matrix P2.d): the agent cannot be
    updated mid-turn. That is a wait, not a fault — and whatever applied before
    it must still be reported, or the caller would redo it."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        key="KEY",
        content="v",
        set_by_account_id=None,
    )
    await db_session.commit()

    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)

    def _running(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST" and re.fullmatch(r"/v1/sessions/[^/]+", request.url.path):
            assert "agent" in json.loads(request.content)
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": (
                            "Cannot update agent while session is running. "
                            "Send a `user.interrupt` event first."
                        ),
                    },
                },
            )
        raise NotHandled

    client = _build_client(state, before=[_running])
    session_id, recorded = await _session_with_env(client, content=b"OLD=1\n")

    result = await _apply(
        client,
        db_session_factory,
        ops=(
            ReplaceEnvFile(old_resource_id=recorded.env_resource_id, old_file_id=None),
            ReplaceToolsAndMcpServers(),
            RemirrorVaultCredentials(),
        ),
        session_id=session_id,
        recorded=recorded,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
    )

    assert isinstance(result, SessionBusy), (
        "a mid-turn refusal must come back as a deferral, not an exception"
    )
    assert result.applied == ("env_file",), (
        "the .env swap ran before the refusal and must be reported as applied"
    )
    assert result.snapshot.env_sha256 is not None, (
        "the partial snapshot must carry the .env that did land"
    )
    assert result.snapshot.tools_sha256 == recorded.tools_sha256, (
        "and must not claim the tools update that MA refused"
    )


async def test_tools_update_leaves_off_a_server_only_another_member_signed_in_to(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A configuration change must not push a personally-connected server back
    onto a session that was created without it — the first `.env` edit of the
    thread's life would otherwise undo the filter."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    connected_account_id = uuid.uuid4()
    bystander_account_id = uuid.uuid4()
    request = await requests_store.create_credential_request(
        db_session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        account_id=connected_account_id,
        target="docs",
        mcp_server_url="https://mcp.example.com/docs",
        requester_platform_user_id="requester-connected",
        channel_id="chan-1",
        expires_at=_NOW + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id=_AGENT_ID,
        target_name="daimon",
        requested_work=None,
    )
    flow = await flows_store.create_flow(
        db_session,
        state="st_" + uuid.uuid4().hex,
        request_token=request.token,
        tenant_id=tenant.id,
        account_id=connected_account_id,
        agent_id=agent_uuid,
        server_name="docs",
        mcp_server_url="https://mcp.example.com/docs",
        redirect_uri="https://d.example/oauth/mcp/callback",
        code_verifier="verifier",
        expires_at=_NOW + timedelta(minutes=10),
    )
    await flows_store.consume_flow(db_session, state=flow.state, now=_NOW)
    await flows_store.mark_flow_completed(db_session, state=flow.state, now=_NOW)
    await db_session.commit()

    state = FakeSessionsState(ma=FakeMAState())
    _register_agent(state.ma)
    bodies: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.count("/") == 3:
            import json

            bodies.append(json.loads(request.content))
        raise NotHandled

    client = _build_client(state, before=[_capture])
    session_id, recorded = await _session_with_env(client, content=b"A=1\n")

    personal = BetaManagedAgentsMCPServerURLDefinition(
        name="docs", type="url", url="https://mcp.example.com/docs"
    )
    daimon = BetaManagedAgentsMCPServerURLDefinition(
        name="daimon-mcp", type="url", url="https://mcp.example/daimon"
    )
    agent = _agent(mcp_servers=[personal, daimon])

    result = await _apply(
        client,
        db_session_factory,
        ops=(ReplaceToolsAndMcpServers(),),
        session_id=session_id,
        recorded=recorded,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        agent=agent,
        account_id=bystander_account_id,
    )

    assert isinstance(result, AppliedOps), "an idle session accepts the agent update"
    sent = [b for b in bodies if "agent" in b][0]["agent"]
    assert [s["name"] for s in sent["mcp_servers"]] == ["daimon-mcp"], (
        "the update carries the caller's server list, not the agent's"
    )
    assert result.snapshot.mcp_servers_sha256 == hash_mcp_servers([daimon]), (
        "and the snapshot hashes what the session now runs, or the next turn re-applies"
    )
