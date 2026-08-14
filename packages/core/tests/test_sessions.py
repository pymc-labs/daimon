from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
    FileMetadata,
)
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.config import McpSettings
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
from daimon.core.sessions import create_session
from daimon.core.stores import agent_github_binding as github_binding_store
from daimon.core.stores import agent_repo_binding as repo_binding_store
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.domain import RepoAccessProof
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    FakeMemoryStoreState,
    NotHandled,
    combine_handlers,
    json_body,
    make_fake_memory_store_handler,
)
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _session_body(
    *,
    session_id: str,
    agent_id: str,
    environment_id: str,
    agent_name: str = "daimon",
    model_id: str = "claude-opus-4-7",
) -> dict[str, Any]:
    """Build a BetaManagedAgentsSession JSON body via validated SDK models."""
    return BetaManagedAgentsSession.model_validate(
        {
            "id": session_id,
            "agent": {
                "id": agent_id,
                "mcp_servers": [],
                "model": {"id": model_id},
                "name": agent_name,
                "skills": [],
                "tools": [],
                "type": "agent",
                "version": 1,
            },
            "created_at": "2026-04-21T00:00:00Z",
            "outcome_evaluations": [],
            "environment_id": environment_id,
            "metadata": {},
            "resources": [],
            "stats": {},
            "status": "idle",
            "type": "session",
            "updated_at": "2026-04-21T00:00:00Z",
            "usage": {},
            "vault_ids": [],
        }
    ).model_dump(mode="json")


def _session_create_handler(
    session_id: str = "sess_new",
) -> Callable[[httpx.Request], httpx.Response]:
    """Mint a handler that responds to POST /v1/sessions with a valid body."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/v1/sessions")
        body = json.loads(request.content)
        assert "agent" in body
        assert "environment_id" in body
        return httpx.Response(
            200,
            json=_session_body(
                session_id=session_id,
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    return _handler


def _make_agent(
    *,
    anthropic_id: str = "ag_1",
    name: str = "a",
) -> BetaManagedAgentsAgent:
    """Inline BetaManagedAgentsAgent construction — no DB needed."""
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": anthropic_id,
            "type": "agent",
            "name": name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": {},
            "description": None,
            "archived_at": None,
            "created_at": "2026-04-21T00:00:00Z",
            "updated_at": "2026-04-21T00:00:00Z",
            "version": 1,
            "mcp_servers": [],
            "skills": [],
            "tools": [],
            "system": None,
        }
    )


def _make_env(
    *,
    anthropic_id: str = "env_1",
    name: str = "e",
) -> BetaEnvironment:
    """Inline BetaEnvironment construction — no DB needed."""
    return BetaEnvironment(
        id=anthropic_id,
        type="environment",
        name=name,
        config=EMPTY_CLOUD_CONFIG,
        metadata={},
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    )


async def test_create_session_calls_ma_api_and_returns_sdk_type() -> None:
    agent = _make_agent(anthropic_id="ag_create")
    env = _make_env(anthropic_id="env_create")
    client = build_fake_anthropic_http(_session_create_handler("sess_1"))

    result = await create_session(
        client,
        agent=agent,
        environment=env,
    )
    assert isinstance(result, BetaManagedAgentsSession), (
        "create_session must return BetaManagedAgentsSession"
    )
    assert result.id == "sess_1", "session id must match MA-minted id"


async def test_create_session_propagates_ma_error() -> None:
    import anthropic

    agent = _make_agent(anthropic_id="ag_x")
    env = _make_env(anthropic_id="env_x")

    def _explode(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"type": "api_error", "message": "boom"}})

    client = build_fake_anthropic_http(_explode)

    with pytest.raises(anthropic.APIError):
        await create_session(
            client,
            agent=agent,
            environment=env,
        )


async def test_create_session_skips_vault_when_public_url_is_none() -> None:
    agent = _make_agent(anthropic_id="ag_skip")
    env = _make_env(anthropic_id="env_skip")

    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/v1/sessions"), (
            f"no vault calls expected when public_url is None; got {request.url.path}"
        )
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_skip",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    client = build_fake_anthropic_http(_handler)

    await create_session(
        client,
        agent=agent,
        environment=env,
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=None),
    )

    assert len(requests) == 1, "must POST only /v1/sessions when public_url is None"
    body = json.loads(requests[0].content)
    assert "vault_ids" not in body, (
        "session-create body must omit vault_ids when no vault is ensured"
    )


async def test_create_session_calls_ensure_agent_mcp_vault_when_public_url_set(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import uuid

    agent = _make_agent(anthropic_id="ag_vault")
    env = _make_env(anthropic_id="env_vault")
    account_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
    agent_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")

    display = f"daimon-mcp:{account_id}:{agent_uuid}"
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vlt_existing",
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
        if request.method == "GET" and request.url.path == "/v1/vaults/vlt_existing/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_existing",
                            "type": "credential",
                            "vault_id": "vlt_existing",
                            "auth": {
                                "type": "static_bearer",
                                "mcp_server_url": "https://mcp.example.com/mcp",
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id="sess_with_vault",
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url}")

    client = build_fake_anthropic_http(_handler)

    result = await create_session(
        client,
        agent=agent,
        environment=env,
        account_id=account_id,
        agent_uuid=agent_uuid,
        mcp_settings=McpSettings(
            jwt_secret=SecretStr("x" * 32),
            public_url=HttpUrl("https://mcp.example.com/mcp"),
        ),
        session_factory=db_session_factory,
    )

    assert isinstance(result, BetaManagedAgentsSession), (
        "must return BetaManagedAgentsSession even with vault"
    )
    paths = [(r.method, r.url.path) for r in requests]
    assert ("GET", "/v1/vaults") in paths, "must GET /v1/vaults to look up existing vault"
    session_create = next(
        r for r in requests if r.method == "POST" and r.url.path.endswith("/v1/sessions")
    )
    body = json.loads(session_create.content)
    assert body.get("vault_ids") == ["vlt_existing"], (
        "session-create must carry the ensured vault id"
    )


def _vault_cold_handler(
    *,
    account_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    public_url: str,
    captured_credential_bodies: list[dict[str, Any]],
    session_id: str,
) -> Callable[[httpx.Request], httpx.Response]:
    """Cold-vault + session-create handler. Records credential POST body."""
    display = f"daimon-mcp:{account_id}:{agent_uuid}"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if request.method == "POST" and request.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "id": "vlt_new",
                    "type": "vault",
                    "display_name": display,
                    "metadata": None,
                    "archived_at": None,
                    "created_at": "2026-04-24T00:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/vaults/vlt_new/credentials":
            captured_credential_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_new",
                    "type": "vault_credential",
                    "vault_id": "vlt_new",
                    "metadata": {},
                    "created_at": "2026-04-24T00:00:00Z",
                    "updated_at": "2026-04-24T00:00:00Z",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": public_url,
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id=session_id,
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url}")

    return _handler


# --- .env resource mount threading ---


def _without_memory_resource(
    resources: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Strip the memory_store entry that session provisioning attaches
    unconditionally whenever tenant_id/agent_uuid/session_factory are provided,
    so pre-existing resource-shape assertions don't need to special-case it."""
    if resources is None:
        return []
    return [r for r in resources if r.get("type") != "memory_store"]


def _files_and_session_handler(
    *,
    file_id: str,
    session_id: str,
    session_create_bodies: list[dict[str, Any]],
    memory_state: FakeMemoryStoreState | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve POST /v1/files (Files upload) and POST /v1/sessions.

    The session-create request body is captured so tests can assert on
    `resources` / `vault_ids`. Also serves the memory-store endpoints (a
    memory store is attached on the same tenant/agent/session_factory gate as
    the .env mount) via ``make_fake_memory_store_handler``.
    """
    now = "2026-05-29T12:00:00Z"
    memory_handler = make_fake_memory_store_handler(memory_state)

    def _handler(request: httpx.Request) -> httpx.Response:
        try:
            return memory_handler(request)
        except NotHandled:
            pass
        if request.method == "POST" and request.url.path == "/v1/files":
            return httpx.Response(
                200,
                json=FileMetadata(
                    id=file_id,
                    created_at="2026-05-29T12:00:00Z",  # type: ignore[arg-type]
                    filename=".env",
                    mime_type="text/plain",
                    size_bytes=len(request.content),
                    type="file",
                ).model_dump(mode="json"),
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json.loads(request.content)
            session_create_bodies.append(body)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id=session_id,
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url.path} ({now})")

    return _handler


async def test_create_session_mounts_env_resource_when_agent_has_secrets(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await put_agent_file(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, key="API_KEY", content="secret"
    )
    await db_session.commit()

    agent = _make_agent(anthropic_id="ag_secrets")
    env = _make_env(anthropic_id="env_secrets")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_env123", session_id="sess_secrets", session_create_bodies=bodies
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
    )

    assert len(bodies) == 1, "exactly one session-create call"
    resources = _without_memory_resource(bodies[0].get("resources"))
    assert resources == [{"type": "file", "file_id": "file_env123", "mount_path": ".env"}], (
        "session-create body must carry the .env file resource when the agent has secrets"
    )


async def test_create_session_omits_resources_when_agent_has_no_secrets(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await db_session.commit()  # no agent_files seeded — agent has no secrets

    agent = _make_agent(anthropic_id="ag_nosecrets")
    env = _make_env(anthropic_id="env_nosecrets")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_nosecrets", session_create_bodies=bodies
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
    )

    assert len(bodies) == 1, "exactly one session-create call"
    assert _without_memory_resource(bodies[0].get("resources")) == [], (
        "session-create body must carry no non-memory resources when the agent has no secrets"
    )


async def test_create_session_omits_resources_when_phase51_params_absent() -> None:
    """Backward compat: without tenant/agent/factory, behaves exactly as today."""
    agent = _make_agent(anthropic_id="ag_noparams")
    env = _make_env(anthropic_id="env_noparams")
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/v1/sessions"), (
            f"no Files upload expected when resource-mount params are absent; got {request.url.path}"
        )
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_noparams",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    client = build_fake_anthropic_http(_handler)

    await create_session(client, agent=agent, environment=env)

    assert len(requests) == 1, "must POST only /v1/sessions; no Files upload attempted"
    body = json.loads(requests[0].content)
    assert "resources" not in body, "no resources when resource-mount params are absent"


# --- GDPR session metadata tagging ---


async def test_create_session_tags_metadata_with_account_and_tenant_when_both_provided() -> None:
    account_id = uuid.UUID("00000000-0000-0000-0000-000000000011")
    tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000022")
    agent = _make_agent(anthropic_id="ag_tag")
    env = _make_env(anthropic_id="env_tag")
    captured_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/sessions")
        body = json.loads(request.content)
        captured_bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_tagged",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    client = build_fake_anthropic_http(_handler)

    await create_session(
        client,
        agent=agent,
        environment=env,
        account_id=account_id,
        tenant_id=tenant_id,
    )

    assert len(captured_bodies) == 1, "exactly one session-create call"
    metadata = captured_bodies[0].get("metadata")
    assert metadata is not None, "session-create body must carry metadata when account_id is given"
    assert metadata["daimon_account"] == str(account_id), (
        "metadata must tag daimon_account with the account UUID string"
    )
    assert metadata["daimon_tenant"] == str(tenant_id), (
        "metadata must tag daimon_tenant with the tenant UUID string"
    )


async def test_create_session_omits_metadata_when_account_and_tenant_both_none() -> None:
    agent = _make_agent(anthropic_id="ag_notag")
    env = _make_env(anthropic_id="env_notag")
    captured_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/sessions")
        body = json.loads(request.content)
        captured_bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_notag",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    client = build_fake_anthropic_http(_handler)

    await create_session(
        client,
        agent=agent,
        environment=env,
        account_id=None,
        tenant_id=None,
        mcp_settings=None,
    )

    assert len(captured_bodies) == 1, "exactly one session-create call"
    assert "metadata" not in captured_bodies[0], (
        "session-create body must omit metadata entirely when both account_id and tenant_id are None"
    )


async def test_create_session_composes_resources_alongside_vault_ids(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """resources must compose with the vault_ids branch, not replace it."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    account_id = uuid.UUID("00000000-0000-0000-0000-000000000055")
    await put_agent_file(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, key="API_KEY", content="secret"
    )
    await db_session.commit()

    agent = _make_agent(anthropic_id="ag_both")
    env = _make_env(anthropic_id="env_both")
    public_url = "https://mcp.example.com/mcp"
    display = f"daimon-mcp:{account_id}:{agent_uuid}"
    bodies: list[dict[str, Any]] = []
    memory_handler = make_fake_memory_store_handler()

    def _handler(request: httpx.Request) -> httpx.Response:
        try:
            return memory_handler(request)
        except NotHandled:
            pass
        if request.method == "GET" and request.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vlt_existing",
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
        if request.method == "GET" and request.url.path == "/v1/vaults/vlt_existing/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_existing",
                            "type": "credential",
                            "vault_id": "vlt_existing",
                            "auth": {
                                "type": "static_bearer",
                                "mcp_server_url": public_url,
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.method == "POST" and request.url.path == "/v1/files":
            return httpx.Response(
                200,
                json=FileMetadata(
                    id="file_both",
                    created_at="2026-05-29T12:00:00Z",  # type: ignore[arg-type]
                    filename=".env",
                    mime_type="text/plain",
                    size_bytes=len(request.content),
                    type="file",
                ).model_dump(mode="json"),
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id="sess_both",
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    client = build_fake_anthropic_http(_handler)

    await create_session(
        client,
        agent=agent,
        environment=env,
        account_id=account_id,
        mcp_settings=McpSettings(
            jwt_secret=SecretStr("x" * 32),
            public_url=HttpUrl(public_url),
        ),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
    )

    assert len(bodies) == 1, "exactly one session-create call"
    assert bodies[0].get("vault_ids") == ["vlt_existing"], (
        "vault_ids behavior must be preserved alongside resources"
    )
    assert _without_memory_resource(bodies[0].get("resources")) == [
        {"type": "file", "file_id": "file_both", "mount_path": ".env"}
    ], "resources must compose alongside vault_ids, not replace it"


# --- per-agent vault isolation ---


async def test_create_session_raises_when_mcp_active_and_agent_uuid_none() -> None:
    """SC-2b: create_session must raise ValueError when mcp_settings is mcp-active
    (public_url + jwt_secret both set) and agent_uuid is None. No MA calls are made."""

    def _no_calls_expected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"no MA calls expected — guard fires before any network call; got {request.url}"
        )

    client = build_fake_anthropic_http(_no_calls_expected)
    agent = _make_agent(anthropic_id="ag_raise")
    env = _make_env(anthropic_id="env_raise")
    account_id = uuid.UUID("00000000-0000-0000-0000-000000000088")

    with pytest.raises(
        ValueError,
        match=r"^agent_uuid is required when mcp_settings has public_url and jwt_secret$",
    ):
        await create_session(
            client,
            agent=agent,
            environment=env,
            account_id=account_id,
            agent_uuid=None,
            mcp_settings=McpSettings(
                jwt_secret=SecretStr("x" * 32),
                public_url=HttpUrl("https://mcp.example.local/mcp"),
            ),
        )


# --- Dev-agent port: github_repository clone resource injection (Task 1) -----


async def _seed_repo_binding_with_pat(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    fernet: MultiFernet,
    db_session_factory: async_sessionmaker[AsyncSession],
    repo_url: str = "https://github.com/example-org/example-repo",
    default_branch: str = "main",
    plaintext_token: str = "ghp_dev_agent_token",
) -> None:
    """Seed an agent_repo_binding + a resolvable per-agent PAT.

    After this, get_binding(tenant,agent) returns the repo binding and
    get_pat(agent_id=agent_uuid) decrypts to ``plaintext_token``.
    """
    await repo_binding_store.set_binding(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        repo_url=repo_url,
        default_branch=default_branch,
        ma_secret_ref="anon:",
        proof=RepoAccessProof(kind="pat", at=datetime(2026, 5, 1, tzinfo=UTC), account_id=None),
    )
    # Per-agent credential overlay: agent_uuid → principal_id.
    await github_binding_store.set_agent_github_binding(
        db_session, agent_id=agent_uuid, principal_id=agent_uuid
    )
    await db_session.commit()
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=agent_uuid,
        github_login="dev-agent",
        plaintext_token=plaintext_token,
        scopes=("repo",),
    )


async def test_create_session_mounts_repo_resource_when_bound_and_pat_present(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_repo_binding_with_pat(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        fernet=fernet,
        db_session_factory=db_session_factory,
    )

    agent = _make_agent(anthropic_id="ag_repo")
    env = _make_env(anthropic_id="env_repo")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_repo", session_create_bodies=bodies
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
    )

    assert len(bodies) == 1, "exactly one session-create call"
    resources = _without_memory_resource(bodies[0].get("resources"))
    assert resources == [
        {
            "type": "github_repository",
            "url": "https://github.com/example-org/example-repo",
            "authorization_token": "ghp_dev_agent_token",
            "checkout": {"type": "branch", "name": "main"},
        }
    ], "session-create body must carry the github_repository clone resource"


def _generate_rsa_keypair() -> str:
    """Return a PEM-encoded RSA private key string for GitHub App-JWT tests."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


async def test_create_session_uses_app_installation_token_when_app_installed_and_no_pat(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No per-agent PAT, App installed on the (private) repo owner, and the
    binding recorded a pat-kind proof at bind time -> the minted installation
    token lands in authorization_token (step 2)."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await _seed_inline_pat_binding(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        proof=RepoAccessProof(kind="pat", at=datetime.now(UTC), account_id=None),
    )

    agent = _make_agent(anthropic_id="ag_app")
    env = _make_env(anthropic_id="env_app")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_app", session_create_bodies=bodies
        )
    )

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/example-org/private-repo/installation":
            return httpx.Response(status_code=200, json={"id": 999})
        if request.url.path == "/app/installations/999/access_tokens":
            return httpx.Response(status_code=201, json={"token": "ghs_installation_token"})
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    github_client = httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        github_app_id="12345",
        github_app_private_key=_generate_rsa_keypair(),
        http_client=github_client,
    )
    await github_client.aclose()

    assert len(bodies) == 1, "exactly one session-create call"
    resources = _without_memory_resource(bodies[0].get("resources"))
    assert resources == [
        {
            "type": "github_repository",
            "url": "https://github.com/example-org/private-repo",
            "authorization_token": "ghs_installation_token",
            "checkout": {"type": "branch", "name": "main"},
        }
    ], "session-create body must carry the App-mode clone resource"


async def test_create_session_pat_wins_with_zero_github_app_calls(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When both a per-agent PAT and App creds are present, the PAT wins
    and resolve_clone_token issues ZERO GitHub App HTTP calls."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_repo_binding_with_pat(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        fernet=fernet,
        db_session_factory=db_session_factory,
    )

    agent = _make_agent(anthropic_id="ag_pat_wins")
    env = _make_env(anthropic_id="env_pat_wins")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_pat_wins", session_create_bodies=bodies
        )
    )

    def github_handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"GitHub App transport must not be called when a PAT wins; got {request.url}")

    github_client = httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
        github_app_id="12345",
        github_app_private_key=_generate_rsa_keypair(),
        http_client=github_client,
    )
    await github_client.aclose()

    assert len(bodies) == 1, "exactly one session-create call"
    resources = _without_memory_resource(bodies[0].get("resources"))
    assert resources == [
        {
            "type": "github_repository",
            "url": "https://github.com/example-org/example-repo",
            "authorization_token": "ghp_dev_agent_token",
            "checkout": {"type": "branch", "name": "main"},
        }
    ], "PAT must win over App coverage"


async def test_create_session_raises_when_private_binding_app_not_installed_no_fallback(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Private binding, no per-agent PAT, App not installed (404), no fallback
    PAT -> resolve_clone_token raises (step 4); no session-create call
    happens with an empty authorization_token."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await _seed_inline_pat_binding(db_session, tenant_id=tenant.id, agent_uuid=agent_uuid)

    agent = _make_agent(anthropic_id="ag_app_404")
    env = _make_env(anthropic_id="env_app_404")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_app_404", session_create_bodies=bodies
        )
    )

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/example-org/private-repo/installation":
            return httpx.Response(status_code=404)
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    github_client = httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    with pytest.raises(DaimonError):
        await create_session(
            client,
            agent=agent,
            environment=env,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            session_factory=db_session_factory,
            github_app_id="12345",
            github_app_private_key=_generate_rsa_keypair(),
            http_client=github_client,
        )
    await github_client.aclose()

    assert len(bodies) == 0, "no session-create call when the clone credential fails to resolve"


async def test_create_session_raises_when_fernet_absent_and_no_other_credential(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without the fernet kwarg, create_session cannot decrypt the per-agent PAT.

    Behavior change (step 4 / C-03): the binding is ``anon:`` (public) with
    no per-agent PAT, no App creds, and no fallback PAT resolvable — this is the
    fail-loud ``none`` branch, not a silent omit. resolve_clone_token raises."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_repo_binding_with_pat(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        fernet=fernet,
        db_session_factory=db_session_factory,
    )

    agent = _make_agent(anthropic_id="ag_norepo")
    env = _make_env(anthropic_id="env_norepo")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_norepo", session_create_bodies=bodies
        )
    )

    with pytest.raises(DaimonError):
        await create_session(
            client,
            agent=agent,
            environment=env,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            session_factory=db_session_factory,
            # fernet omitted
        )

    assert len(bodies) == 0, "no session-create call when the clone credential fails to resolve"


async def test_create_session_omits_repo_resource_when_unbound(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fernet present but agent has no repo binding → no clone resource."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await db_session.commit()  # no binding, no credential seeded
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    agent = _make_agent(anthropic_id="ag_unbound")
    env = _make_env(anthropic_id="env_unbound")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_unbound", session_create_bodies=bodies
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
    )

    assert len(bodies) == 1, "exactly one session-create call"
    assert _without_memory_resource(bodies[0].get("resources")) == [], (
        "no non-memory resource when the agent is unbound"
    )


# --- Dev-agent port: Copilot MCP credential provisioning (Task 2) -----------


def _warm_vault_copilot_handler(
    *,
    account_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    public_url: str,
    credential_post_bodies: list[dict[str, Any]],
    session_create_bodies: list[dict[str, Any]],
    session_id: str,
) -> Callable[[httpx.Request], httpx.Response]:
    """Warm-vault handler that captures vault credential POSTs + session create.

    The existing vault already carries the daimon-mcp credential at ``public_url``
    (so ``ensure_agent_mcp_vault`` does NOT rebind) and NO Copilot credential
    yet — so the only credential POST is the Copilot one.
    Also serves the memory-store endpoints (the unconditional memory attach).
    """
    display = f"daimon-mcp:{account_id}:{agent_uuid}"
    memory_handler = make_fake_memory_store_handler()

    def _handler(request: httpx.Request) -> httpx.Response:
        try:
            return memory_handler(request)
        except NotHandled:
            pass
        if request.method == "GET" and request.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vlt_existing",
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
        if request.method == "GET" and request.url.path == "/v1/vaults/vlt_existing/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_daimon_mcp",
                            "type": "credential",
                            "vault_id": "vlt_existing",
                            "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.method == "POST" and request.url.path == "/v1/vaults/vlt_existing/credentials":
            credential_post_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_copilot",
                    "type": "credential",
                    "vault_id": "vlt_existing",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": "https://api.githubcopilot.com/mcp",
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json.loads(request.content)
            session_create_bodies.append(body)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id=session_id,
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    return _handler


async def test_create_session_provisions_copilot_credential_from_pat(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the vault is ensured and the agent has a resolvable PAT, a Copilot
    static_bearer credential is provisioned on the same vault from that PAT."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    account_id = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
    public_url = "https://mcp.example.com/mcp"
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_repo_binding_with_pat(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        fernet=fernet,
        db_session_factory=db_session_factory,
        plaintext_token="ghp_copilot_pat",
    )

    agent = _make_agent(anthropic_id="ag_copilot")
    env = _make_env(anthropic_id="env_copilot")
    cred_bodies: list[dict[str, Any]] = []
    session_bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _warm_vault_copilot_handler(
            account_id=account_id,
            agent_uuid=agent_uuid,
            public_url=public_url,
            credential_post_bodies=cred_bodies,
            session_create_bodies=session_bodies,
            session_id="sess_copilot",
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        account_id=account_id,
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
    )

    assert len(cred_bodies) == 1, "exactly one credential POST (the Copilot cred)"
    auth = cred_bodies[0]["auth"]
    assert auth["type"] == "static_bearer"
    assert auth["mcp_server_url"] == "https://api.githubcopilot.com/mcp"
    assert auth["token"] == "ghp_copilot_pat", "Copilot cred must carry the resolved per-agent PAT"
    assert len(session_bodies) == 1
    assert session_bodies[0].get("vault_ids") == ["vlt_existing"], (
        "the Copilot cred rides the same vault attached to the session"
    )


async def test_create_session_skips_copilot_when_no_pat(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Vault ensured but the agent has no resolvable PAT → no Copilot cred POST."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    account_id = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
    public_url = "https://mcp.example.com/mcp"
    await db_session.commit()  # no binding / no credential seeded
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    agent = _make_agent(anthropic_id="ag_nocopilot")
    env = _make_env(anthropic_id="env_nocopilot")
    cred_bodies: list[dict[str, Any]] = []
    session_bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _warm_vault_copilot_handler(
            account_id=account_id,
            agent_uuid=agent_uuid,
            public_url=public_url,
            credential_post_bodies=cred_bodies,
            session_create_bodies=session_bodies,
            session_id="sess_nocopilot",
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        account_id=account_id,
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
    )

    assert len(cred_bodies) == 0, "no Copilot credential POST when there is no PAT"
    assert len(session_bodies) == 1


async def test_create_session_never_mirrors_fallback_pat_into_copilot_vault(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An agent with no per-agent PAT but a configured operator fallback still
    gets NO Copilot vault credential -- the fallback must never be mirrored
    into a per-agent MA vault. This is a regression guard: the signature
    change that added allow_service_default/fallback_pat to get_pat makes it
    newly *possible* for this call site to opt in; sessions.py's single
    get_pat call (feeding both the Copilot mirror and the clone resolver)
    must stay at the default so this never happens."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    account_id = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
    public_url = "https://mcp.example.com/mcp"
    await _seed_anon_binding(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        proof=RepoAccessProof(kind="public", at=datetime.now(UTC), account_id=None),
    )
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    agent = _make_agent(anthropic_id="ag_fallback_novault")
    env = _make_env(anthropic_id="env_fallback_novault")
    cred_bodies: list[dict[str, Any]] = []
    session_bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _warm_vault_copilot_handler(
            account_id=account_id,
            agent_uuid=agent_uuid,
            public_url=public_url,
            credential_post_bodies=cred_bodies,
            session_create_bodies=session_bodies,
            session_id="sess_fallback_novault",
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        account_id=account_id,
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
        github_fallback_pat="ghp_operator_fallback",
    )

    assert len(cred_bodies) == 0, (
        "the operator fallback PAT must never be mirrored into a per-agent Copilot vault "
        "credential, even when a repo binding resolves it for the clone resource"
    )
    assert len(session_bodies) == 1, "the session is still created"
    resources = _without_memory_resource(session_bodies[0].get("resources"))
    assert resources == [
        {
            "type": "github_repository",
            "url": "https://github.com/example-org/example-repo",
            "authorization_token": "ghp_operator_fallback",
            "checkout": {"type": "branch", "name": "main"},
        }
    ], "the clone resource still resolves via the fallback -- only the Copilot mirror is guarded"


async def test_create_session_app_installation_token_wins_over_fallback_for_anon_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An anon: (verified-public) binding with a configured GitHub App
    installation AND a configured operator fallback must clone via the
    installation token, not the fallback -- the App keeps winning. Regression
    guard for the resolve_clone_token ordering (per_agent_pat -> App ->
    fallback) now that get_pat can itself resolve a fallback: sessions.py
    must keep passing per_agent_pat=None to resolve_clone_token so the App
    branch is reached first, exactly as before this phase's changes. The
    binding recorded a verified-public proof, which is what the fallback
    tier requires (and the App tier accepts any recorded proof kind)."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await _seed_anon_binding(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        proof=RepoAccessProof(kind="public", at=datetime.now(UTC), account_id=None),
    )

    agent = _make_agent(anthropic_id="ag_app_over_fallback")
    env = _make_env(anthropic_id="env_app_over_fallback")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused",
            session_id="sess_app_over_fallback",
            session_create_bodies=bodies,
        )
    )

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/example-org/example-repo/installation":
            return httpx.Response(status_code=200, json={"id": 4242})
        if request.url.path == "/app/installations/4242/access_tokens":
            return httpx.Response(status_code=201, json={"token": "ghs_installation_wins"})
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    github_client = httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        github_fallback_pat="ghp_operator_fallback",
        github_app_id="12345",
        github_app_private_key=_generate_rsa_keypair(),
        http_client=github_client,
    )
    await github_client.aclose()

    assert len(bodies) == 1, "exactly one session-create call"
    resources = _without_memory_resource(bodies[0].get("resources"))
    assert resources == [
        {
            "type": "github_repository",
            "url": "https://github.com/example-org/example-repo",
            "authorization_token": "ghs_installation_wins",
            "checkout": {"type": "branch", "name": "main"},
        }
    ], "the App installation token must win over a configured operator fallback"


def _warm_vault_copilot_500_handler(
    *,
    account_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    public_url: str,
    session_create_bodies: list[dict[str, Any]],
    session_id: str,
) -> Callable[[httpx.Request], httpx.Response]:
    """Like ``_warm_vault_copilot_handler``, but the Copilot credential POST
    500s — models a transient MA APIError on that optional mount."""
    display = f"daimon-mcp:{account_id}:{agent_uuid}"
    memory_handler = make_fake_memory_store_handler()

    def _handler(request: httpx.Request) -> httpx.Response:
        try:
            return memory_handler(request)
        except NotHandled:
            pass
        if request.method == "GET" and request.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vlt_existing",
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
        if request.method == "GET" and request.url.path == "/v1/vaults/vlt_existing/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_daimon_mcp",
                            "type": "credential",
                            "vault_id": "vlt_existing",
                            "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.method == "POST" and request.url.path == "/v1/vaults/vlt_existing/credentials":
            return httpx.Response(
                500,
                json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json.loads(request.content)
            session_create_bodies.append(body)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id=session_id,
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    return _handler


async def test_create_session_degrades_when_copilot_credential_mount_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SYNC-04: a transient anthropic.APIError on the Copilot credential POST
    must not raise out of create_session — the session is still created, and
    a stable-keyed structured warning fires (the alerting pipeline keys off these)."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    account_id = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
    public_url = "https://mcp.example.com/mcp"
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_repo_binding_with_pat(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        fernet=fernet,
        db_session_factory=db_session_factory,
        plaintext_token="ghp_copilot_pat",
    )

    agent = _make_agent(anthropic_id="ag_copilot_fail")
    env = _make_env(anthropic_id="env_copilot_fail")
    session_bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _warm_vault_copilot_500_handler(
            account_id=account_id,
            agent_uuid=agent_uuid,
            public_url=public_url,
            session_create_bodies=session_bodies,
            session_id="sess_copilot_fail",
        )
    )

    import structlog.testing

    with structlog.testing.capture_logs() as logs:
        session = await create_session(
            client,
            agent=agent,
            environment=env,
            account_id=account_id,
            mcp_settings=McpSettings(
                jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)
            ),
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            session_factory=db_session_factory,
            fernet=fernet,
        )

    assert session is not None, "a transient Copilot-credential APIError must degrade, not raise"
    assert len(session_bodies) == 1, "session-create must still happen after the degrade"
    assert any(e.get("event") == "copilot_credential.mount_failed" for e in logs), (
        "must emit the stable copilot_credential.mount_failed warning key"
    )


# --- Operator fallback PAT for public-repo clones (quick task 260616-45k) -----


async def _seed_anon_binding(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    repo_url: str = "https://github.com/example-org/example-repo",
    default_branch: str = "main",
    proof: RepoAccessProof | None = None,
) -> None:
    """Seed an ``anon:`` (public, no-PAT) repo binding with NO per-agent PAT.

    ``proof`` defaults to None (a proof-NULL row) — callers whose scenario
    relies on the App or fallback tier firing must pass the proof that tier
    now requires.
    """
    await repo_binding_store.set_binding(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        repo_url=repo_url,
        default_branch=default_branch,
        ma_secret_ref="anon:",
        proof=proof,
    )
    await db_session.commit()


async def _seed_inline_pat_binding(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    repo_url: str = "https://github.com/example-org/private-repo",
    default_branch: str = "main",
    proof: RepoAccessProof | None = None,
) -> None:
    """Seed an ``inline-pat:`` (private) binding with NO resolvable per-agent PAT.

    Models the guardrail case: a private binding whose per-agent credential is
    absent. The fallback PAT must NEVER apply here. ``proof`` defaults to
    None — callers whose scenario relies on the App tier firing must pass a
    proof.
    """
    await repo_binding_store.set_binding(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        repo_url=repo_url,
        default_branch=default_branch,
        ma_secret_ref=f"inline-pat:{agent_uuid}",
        proof=proof,
    )
    await db_session.commit()


async def test_create_session_uses_fallback_pat_for_anon_binding_without_per_agent_pat(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_anon_binding(
        db_session,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        proof=RepoAccessProof(kind="public", at=datetime.now(UTC), account_id=None),
    )

    agent = _make_agent(anthropic_id="ag_fallback")
    env = _make_env(anthropic_id="env_fallback")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_fallback", session_create_bodies=bodies
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
        github_fallback_pat="ghp_operator_fallback",
    )

    assert len(bodies) == 1, "exactly one session-create call"
    assert _without_memory_resource(bodies[0].get("resources")) == [
        {
            "type": "github_repository",
            "url": "https://github.com/example-org/example-repo",
            "authorization_token": "ghp_operator_fallback",
            "checkout": {"type": "branch", "name": "main"},
        }
    ], "anon binding with no per-agent PAT must clone via the operator fallback PAT"


async def test_create_session_fallback_not_used_for_inline_pat_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The guardrail (fallback PAT never clones a private binding) now surfaces
    as a raise, not a silent omit (step 4 / C-03): a private (inline-pat:)
    binding with no per-agent PAT, no App coverage, and no App creds configured
    hits the fail-loud ``none`` branch even though an operator fallback PAT was
    passed — the fallback only ever applies to ``anon:`` (public) bindings."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_inline_pat_binding(db_session, tenant_id=tenant.id, agent_uuid=agent_uuid)

    agent = _make_agent(anthropic_id="ag_private")
    env = _make_env(anthropic_id="env_private")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_private", session_create_bodies=bodies
        )
    )

    with pytest.raises(DaimonError):
        await create_session(
            client,
            agent=agent,
            environment=env,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            session_factory=db_session_factory,
            fernet=fernet,
            github_fallback_pat="ghp_operator_fallback",
        )

    assert len(bodies) == 0, (
        "fallback PAT must never clone a private (inline-pat) binding — guardrail holds "
        "via a raise, no session-create call happens"
    )


async def test_create_session_raises_when_binding_present_but_no_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Behavior change (step 4 / C-03): a binding exists but nothing
    resolves a clone token (no per-agent PAT, no App creds, no fallback) —
    resolve_clone_token raises instead of the old ``repo_clone.no_token``
    warn-and-omit. No session-create call happens with an empty token."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_anon_binding(db_session, tenant_id=tenant.id, agent_uuid=agent_uuid)

    agent = _make_agent(anthropic_id="ag_notoken")
    env = _make_env(anthropic_id="env_notoken")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_notoken", session_create_bodies=bodies
        )
    )

    with pytest.raises(DaimonError):
        await create_session(
            client,
            agent=agent,
            environment=env,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            session_factory=db_session_factory,
            fernet=fernet,
            github_fallback_pat=None,  # no fallback configured
        )

    assert len(bodies) == 0, "no session-create call when the clone credential fails to resolve"


async def test_create_session_raises_on_empty_fallback_pat_per_d02_behavior_change(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A blank DAIMON_GITHUB__FALLBACK_PAT ("" not None) must behave like "no
    token" — without the guard it builds authorization_token="" and MA 400s.

    Deliberate C-03 behavior change from earlier semantics: this
    test used to assert the clone resource was silently omitted. The
    step-4 "fail loudly" resolution order now makes resolve_clone_token raise
    on this exact case (anon: binding, no per-agent PAT, no App coverage, and
    an empty/falsy fallback PAT) instead of omitting the resource — a bound
    repo with no resolvable credential is a hard failure, never a silent
    no-clone."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_anon_binding(db_session, tenant_id=tenant.id, agent_uuid=agent_uuid)

    agent = _make_agent(anthropic_id="ag_emptytok")
    env = _make_env(anthropic_id="env_emptytok")
    bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _files_and_session_handler(
            file_id="file_unused", session_id="sess_emptytok", session_create_bodies=bodies
        )
    )

    with pytest.raises(DaimonError):
        await create_session(
            client,
            agent=agent,
            environment=env,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            session_factory=db_session_factory,
            fernet=fernet,
            github_fallback_pat="",  # blank fallback PAT must resolve to "no token" -> raise
        )

    assert len(bodies) == 0, "no session-create call when the clone credential fails to resolve"


# --- Agent memory: per-agent memory store attach -----------------------------


async def test_create_session_attaches_memory_store(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """resources[] gains a memory_store entry alongside the existing mounts."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_uuid = uuid.uuid4()
    mem_state = FakeMemoryStoreState()
    captured: dict[str, Any] = {}

    agent = _make_agent(anthropic_id="ag_memory", name="daimon-memtest")
    env = _make_env(anthropic_id="env_memory")

    def capture_session_create(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json_body(request)
            captured.update(body)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id="sess_memory",
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise NotHandled

    client = build_fake_anthropic_http(
        combine_handlers(capture_session_create, make_fake_memory_store_handler(mem_state))
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
    )

    memory_resources = [r for r in captured.get("resources", []) if r.get("type") == "memory_store"]
    assert len(memory_resources) == 1, (
        "session-create body must carry exactly one memory_store resource"
    )
    assert memory_resources[0]["access"] == "read_write"
    assert memory_resources[0]["memory_store_id"] in mem_state.stores


async def test_create_session_degrades_when_memory_provisioning_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A memory-API outage must not block the session (degrade-not-block)."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    captured: dict[str, Any] = {}

    agent = _make_agent(anthropic_id="ag_memory_fail")
    env = _make_env(anthropic_id="env_memory_fail")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/memory_stores":
            return httpx.Response(
                500,
                json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json_body(request)
            captured.update(body)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id="sess_memory_fail",
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    client = build_fake_anthropic_http(handler)

    session = await create_session(
        client,
        agent=agent,
        environment=env,
        tenant_id=tenant.id,
        agent_uuid=uuid.uuid4(),
        session_factory=db_session_factory,
    )
    assert session is not None
    assert all(r.get("type") != "memory_store" for r in captured.get("resources", [])), (
        "failed memory provisioning must degrade to a memory-less session, not raise"
    )


# --- Agent-scoped external MCP credentials mirror into the caller's vault ----


def _warm_vault_mirror_handler(
    *,
    account_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    public_url: str,
    credential_post_bodies: list[dict[str, Any]],
    credential_deletes: list[str],
    session_create_bodies: list[dict[str, Any]],
    session_id: str,
) -> Callable[[httpx.Request], httpx.Response]:
    """Warm vault holding only the caller's own daimon-mcp credential.

    No credential for any external server yet — which is exactly the state a
    caller who did not attach the server arrives in.
    """
    display = f"daimon-mcp:{account_id}:{agent_uuid}"
    memory_handler = make_fake_memory_store_handler()

    def _handler(request: httpx.Request) -> httpx.Response:
        try:
            return memory_handler(request)
        except NotHandled:
            pass
        if request.method == "GET" and request.url.path == "/v1/vaults":
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
        if request.method == "GET" and request.url.path == "/v1/vaults/vlt_caller/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vcrd_daimon_mcp",
                            "type": "credential",
                            "vault_id": "vlt_caller",
                            "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.method == "DELETE" and "/credentials/" in request.url.path:
            credential_deletes.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"id": "vcrd_gone", "type": "credential"})
        if request.method == "POST" and request.url.path == "/v1/vaults/vlt_caller/credentials":
            body = json.loads(request.content)
            credential_post_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_mirrored",
                    "type": "credential",
                    "vault_id": "vlt_caller",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": body["auth"]["mcp_server_url"],
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json.loads(request.content)
            session_create_bodies.append(body)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id=session_id,
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    return _handler


async def test_create_session_mirrors_agent_mcp_credential_for_a_caller_who_did_not_attach_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The regression: an external MCP server is attached to the agent, so a
    second caller's session must carry its credential too. Before the fix the
    token lived only in the attacher's vault and this caller's turn died with
    mcp_authentication_failed_error."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    attacher_account_id = uuid.UUID("00000000-0000-0000-0000-00000000aaaa")
    other_account_id = uuid.UUID("00000000-0000-0000-0000-00000000bbbb")
    public_url = "https://mcp.example.com/mcp"
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    # The attacher stored the credential agent-scoped, as the setup panel does.
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        mcp_server_url="https://internal.example.com/mcp",
        plaintext_token="tok_internal",
    )
    assert attacher_account_id != other_account_id

    agent = _make_agent(anthropic_id="ag_mirror")
    env = _make_env(anthropic_id="env_mirror")
    cred_bodies: list[dict[str, Any]] = []
    cred_deletes: list[str] = []
    session_bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _warm_vault_mirror_handler(
            account_id=other_account_id,
            agent_uuid=agent_uuid,
            public_url=public_url,
            credential_post_bodies=cred_bodies,
            credential_deletes=cred_deletes,
            session_create_bodies=session_bodies,
            session_id="sess_mirror",
        )
    )

    await create_session(
        client,
        agent=agent,
        environment=env,
        account_id=other_account_id,
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
    )

    mirrored = [
        b for b in cred_bodies if b["auth"]["mcp_server_url"].startswith("https://internal")
    ]
    assert len(mirrored) == 1, (
        "the agent's external MCP credential must be mirrored into this caller's vault"
    )
    assert mirrored[0]["auth"]["token"] == "tok_internal", "mirrored cred carries the stored token"
    assert "vcrd_daimon_mcp" not in cred_deletes, (
        "mirroring must not disturb the caller's own daimon-mcp credential"
    )
    assert session_bodies[0].get("vault_ids") == ["vlt_caller"], (
        "the mirrored cred rides the caller's own vault"
    )


async def test_create_session_mirrors_nothing_when_agent_has_no_stored_mcp_credentials(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No stored credentials means no credential POSTs — the mirror must not
    invent work for the common case of an agent with only daimon-mcp."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    account_id = uuid.UUID("00000000-0000-0000-0000-00000000cccc")
    public_url = "https://mcp.example.com/mcp"
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    cred_bodies: list[dict[str, Any]] = []
    cred_deletes: list[str] = []
    session_bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _warm_vault_mirror_handler(
            account_id=account_id,
            agent_uuid=agent_uuid,
            public_url=public_url,
            credential_post_bodies=cred_bodies,
            credential_deletes=cred_deletes,
            session_create_bodies=session_bodies,
            session_id="sess_nomirror",
        )
    )

    await create_session(
        client,
        agent=_make_agent(anthropic_id="ag_nomirror"),
        environment=_make_env(anthropic_id="env_nomirror"),
        account_id=account_id,
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=fernet,
    )

    assert cred_bodies == [], "no stored credentials means no credential POSTs"
    assert cred_deletes == [], "and nothing deleted"
    assert len(session_bodies) == 1, "the session is still created"
