from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
    FileMetadata,
)
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core._models import AgentGitHubMode, GitHubAppSessionVault
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.config import GithubAppSettings, McpSettings
from daimon.core.credential_requests import mint_request_token
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_CHANNEL,
)
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.github_app_auth import BotIdentity
from daimon.core.github_app_session import AppSessionAccess, AppToken
from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
from daimon.core.memory_resource import (
    MEMORY_INSTRUCTIONS,
    PERSONAL_MEMORY_INSTRUCTIONS,
    READ_ONLY_MEMORY_INSTRUCTIONS,
    READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS,
    SHARED_ONLY_MEMORY_INSTRUCTIONS,
)
from daimon.core.sessions import create_isolated_session, create_session
from daimon.core.skills.unresolved import SkillUnavailable
from daimon.core.stores import agent_github_binding as github_binding_store
from daimon.core.stores import agent_repo_binding as repo_binding_store
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.domain import RepoAccessProof
from daimon.core.turn.posture import ExemptReason
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    NotHandled,
    combine_handlers,
    json_body,
    make_fake_memory_store_handler,
)
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session, ma_session_agent
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
    return ma_session(
        id=session_id,
        agent=ma_session_agent(id=agent_id, name=agent_name, model=model_id),
        environment_id=environment_id,
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
    """An agent with this file's defaults — no DB needed."""
    return ma_agent(id=anthropic_id, name=name, model="claude-opus-4-7")


def _make_env(
    *,
    anthropic_id: str = "env_1",
    name: str = "e",
) -> BetaEnvironment:
    """An environment with this file's defaults — no DB needed."""
    return ma_environment(id=anthropic_id, name=name)


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


def _unresolved_skill_400(skill_id: str, version: str) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": (
                        "Could not resolve one or more skills: "
                        f'skill "{skill_id}" version "{version}" not found'
                    ),
                },
            },
        )

    return _handler


async def test_create_session_names_a_deleted_agent_skill() -> None:
    """Finding #11: a deleted skill the agent pins fails with copy naming it, not a bare 400."""
    agent = ma_agent(
        id="ag_strand",
        name="qa-fork",
        skills=[{"type": "custom", "skill_id": "skill_01PBRj", "version": "latest"}],
    )
    client = build_fake_anthropic_http(_unresolved_skill_400("skill_01PBRj", "latest"))

    with pytest.raises(SkillUnavailable) as refused:
        await create_session(client, agent=agent, environment=_make_env())

    message = str(refused.value)
    assert isinstance(refused.value, UserFacingError), "every adapter shows this copy as written"
    assert '"skill_01PBRj"' in message and "qa-fork uses" in message
    assert "remove_skill" in message
    assert "couldn't accept the request" not in message


async def test_an_isolated_session_for_a_deleted_skill_says_to_re_publish_the_report() -> None:
    """A report reader stranded on a deleted skill gets the repair, not a bare 400."""
    client = build_fake_anthropic_http(_unresolved_skill_400("skill_gone", "1"))

    with pytest.raises(SkillUnavailable) as refused:
        await create_isolated_session(
            client,
            agent=_make_agent(),
            environment=_make_env(),
            account_id=None,
            tenant_id=None,
            resources=[{"type": "file", "file_id": "file_1", "mount_path": "/bundle.tar.gz"}],
        )

    message = str(refused.value)
    assert '"skill_gone"' in message
    assert "Ask the report's publisher to re-publish" in message


async def test_create_session_names_a_deleted_channel_skill_from_its_rule(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A channel's deleted skill is named from the rule that added it."""
    from anthropic.types.beta import BetaManagedAgentsCustomSkill
    from daimon.core.stores.channel_skills import add_channel_skill

    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session)
        await add_channel_skill(
            session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="555",
            skill_id="skill_chan",
            version="17916",
            name="qa-haiku-canary",
            owner_agent_name=None,
            actor_account_id=None,
        )
    client = build_fake_anthropic_http(_unresolved_skill_400("skill_chan", "17916"))

    with pytest.raises(SkillUnavailable) as refused:
        await create_session(
            client,
            agent=_make_agent(),
            environment=_make_env(),
            tenant_id=tenant.id,
            session_factory=db_session_factory,
            channel_skills=(
                BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_chan", version="17916"),
            ),
        )

    assert "this channel adds" in str(refused.value)
    assert '"qa-haiku-canary" (skill_chan)' in str(refused.value)
    assert "remove_channel_skill" in str(refused.value)


async def test_create_session_keeps_other_400s_unchanged() -> None:
    import anthropic

    def _bad(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}
        )

    with pytest.raises(anthropic.BadRequestError):
        await create_session(
            build_fake_anthropic_http(_bad), agent=_make_agent(), environment=_make_env()
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
                            "metadata": {"daimon_chat_identity": str(agent_uuid)},
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
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        key="API_KEY",
        content="secret",
        set_by_account_id=None,
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


@pytest.mark.parametrize(
    ("billing_exempt", "expected"),
    [(None, None), ("mcp-internal-caller", "mcp-internal-caller")],
    ids=["billed-caller-unstamped", "exempt-caller-stamped"],
)
async def test_create_session_stamps_billing_exempt_reason_only_when_given(
    billing_exempt: ExemptReason | None, expected: str | None
) -> None:
    """``billing_exempt`` becomes ``daimon_billing_exempt=<reason>``, the marker
    the usage sweep skips; a billed caller's session has no such key."""
    captured_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_exempt",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    await create_session(
        build_fake_anthropic_http(_handler),
        agent=_make_agent(anthropic_id="ag_ex"),
        environment=_make_env(anthropic_id="env_ex"),
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000022"),
        billing_exempt=billing_exempt,
    )

    metadata = captured_bodies[0]["metadata"]
    assert metadata.get(MA_METADATA_KEY_BILLING_EXEMPT) == expected, (
        f"billing_exempt={billing_exempt!r} must stamp {expected!r}; got {metadata!r}"
    )


@pytest.mark.parametrize(
    ("thread_id", "sealed", "expected"),
    [
        (None, None, {"daimon_channel": "chan-1"}),
        ("thr-1", None, {"daimon_channel": "chan-1", "daimon_thread": "thr-1"}),
        (
            "thr-1",
            "thr-1",
            {"daimon_channel": "chan-1", "daimon_thread": "thr-1", "daimon_sealed": "thr-1"},
        ),
    ],
    ids=["channel", "thread", "sealed-thread"],
)
async def test_create_session_stamps_the_channel_turn_it_is_opened_for(
    thread_id: str | None, sealed: str | None, expected: dict[str, str]
) -> None:
    """The transcript tools apply the channel seal from these stamps."""
    captured_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_origin",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    await create_session(
        build_fake_anthropic_http(_handler),
        agent=_make_agent(anthropic_id="ag_origin"),
        environment=_make_env(anthropic_id="env_origin"),
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000022"),
        origin_channel_id="chan-1",
        origin_thread_id=thread_id,
        origin_seal_ids=() if sealed is None else (sealed,),
    )

    metadata = captured_bodies[0]["metadata"]
    stamped = {
        k: v
        for k, v in metadata.items()
        if k in {"daimon_channel", "daimon_thread", "daimon_sealed"}
    }
    assert stamped == expected


@pytest.mark.parametrize(
    "budget_channel_id", [None, "chan-1"], ids=["unbudgeted", "budget-stamped"]
)
async def test_create_session_stamps_the_budget_channel_apart_from_the_seal_origin(
    budget_channel_id: str | None,
) -> None:
    """``budget_channel_id`` becomes ``daimon_budget_channel`` and never ``daimon_channel``.

    A DM is budgeted to the channel it was moved from without running there, so
    the stamp the seal reads must stay absent.
    """
    captured_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_channel",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    await create_session(
        build_fake_anthropic_http(_handler),
        agent=_make_agent(anthropic_id="ag_ch"),
        environment=_make_env(anthropic_id="env_ch"),
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000022"),
        budget_channel_id=budget_channel_id,
    )

    metadata = captured_bodies[0]["metadata"]
    assert metadata.get(MA_METADATA_KEY_BUDGET_CHANNEL) == budget_channel_id, metadata
    assert MA_METADATA_KEY_CHANNEL not in metadata, (
        f"a budget stamp must not place the session in a channel's seal; got {metadata!r}"
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
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_uuid,
        key="API_KEY",
        content="secret",
        set_by_account_id=None,
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
                            "metadata": {"daimon_chat_identity": str(agent_uuid)},
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
                            "metadata": {"daimon_chat_identity": str(agent_uuid)},
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured new App never enters the legacy session's mint path."""
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
    app_mint = AsyncMock()
    monkeypatch.setattr("daimon.core.sessions.prepare_app_access", app_mint)

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
        agent_github_app=GithubAppSettings(app_id="new", private_key=SecretStr("unused")),
    )

    app_mint.assert_not_awaited()
    assert len(cred_bodies) == 0, "no Copilot credential POST when there is no PAT"
    assert len(session_bodies) == 1


@pytest.mark.parametrize("app_session_unmapped", [False, True])
async def test_create_session_app_mode_with_zero_grants_has_no_github_access(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    app_session_unmapped: bool,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant) if app_session_unmapped else None
    agent_uuid = uuid.uuid4()
    db_session.add(AgentGitHubMode(tenant_id=tenant.id, agent_id=agent_uuid, mode="app"))
    await db_session.commit()
    vault = AsyncMock(return_value="vlt_session")
    credentials = AsyncMock()
    monkeypatch.setattr("daimon.core.sessions.create_session_vault", vault)
    monkeypatch.setattr("daimon.core.sessions.add_app_credentials", credentials)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/sessions"
        body = json_body(request)
        bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_zero_grants",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    await create_session(
        build_fake_anthropic_http(handler),
        agent=_make_agent(anthropic_id="ag_zero_grants"),
        environment=_make_env(anthropic_id="env_zero_grants"),
        tenant_id=tenant.id,
        account_id=account.id if account is not None else None,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        app_session_unmapped=app_session_unmapped,
    )
    async with db_session_factory() as session:
        registered = await session.get(GitHubAppSessionVault, "sess_zero_grants")
    assert registered is not None
    assert registered.is_unmapped is app_session_unmapped
    assert registered.is_mcp is app_session_unmapped
    assert registered.agent_id == (agent_uuid if app_session_unmapped else None)
    assert bodies[0]["vault_ids"] == ["vlt_session"]
    assert not any(r["type"] == "github_repository" for r in bodies[0].get("resources", []))
    assert credentials.await_args.kwargs["access"].tokens == ()
    assert credentials.await_args.kwargs["access"].working_token is None


@pytest.mark.parametrize("identity_resolves", [True, False])
async def test_create_session_app_mode_mounts_the_bot_commit_identity_when_a_token_exists(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    identity_resolves: bool,
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    db_session.add(AgentGitHubMode(tenant_id=tenant.id, agent_id=agent_uuid, mode="app"))
    await db_session.commit()
    identity = BotIdentity(name="daimon[bot]", email="7+daimon[bot]@users.noreply.github.com")
    token = AppToken(uuid.uuid4(), "ghs_x", 1, (1,), "write", "GH_TOKEN_ACME_WRITE")
    monkeypatch.setattr("daimon.core.sessions.create_session_vault", AsyncMock(return_value="v"))
    monkeypatch.setattr(
        "daimon.core.sessions.prepare_app_access",
        AsyncMock(return_value=AppSessionAccess((), (token,), "ghs_x")),
    )
    monkeypatch.setattr("daimon.core.sessions.add_app_credentials", AsyncMock())
    monkeypatch.setattr("daimon.core.sessions.finish_app_delivery", AsyncMock())
    resolve = AsyncMock(return_value=identity if identity_resolves else None)
    monkeypatch.setattr("daimon.core.sessions.resolve_bot_identity", resolve)
    mount = AsyncMock(
        return_value={"type": "file", "file_id": "file_id", "mount_path": "git-identity.sh"}
    )
    monkeypatch.setattr("daimon.core.sessions.upload_git_identity_and_mount", mount)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/sessions"
        body = json_body(request)
        bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_identity",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    await create_session(
        build_fake_anthropic_http(handler),
        agent=_make_agent(anthropic_id="ag_identity"),
        environment=_make_env(anthropic_id="env_identity"),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        agent_github_app=GithubAppSettings(app_slug="daimon"),
    )

    resolve.assert_awaited_once()
    assert resolve.await_args is not None and resolve.await_args.kwargs["slug"] == "daimon"
    mounted = [r for r in bodies[0].get("resources", []) if r["type"] == "file"]
    if identity_resolves:
        assert mounted == [
            {"type": "file", "file_id": "file_id", "mount_path": "git-identity.sh"}
        ], "a session holding an App token carries the bot's commit identity"
    else:
        assert mounted == [], "an unresolved identity leaves the session without one"
        mount.assert_not_awaited()


async def test_app_session_attaches_caller_and_session_vaults(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid, account_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(AgentGitHubMode(tenant_id=tenant.id, agent_id=agent_uuid, mode="app"))
    await db_session.commit()
    monkeypatch.setattr(
        "daimon.core.sessions.create_session_vault", AsyncMock(return_value="vlt_session")
    )
    monkeypatch.setattr(
        "daimon.core.sessions.ensure_agent_mcp_vault", AsyncMock(return_value="vlt_caller")
    )
    add_app = AsyncMock()
    mirror = AsyncMock()
    monkeypatch.setattr("daimon.core.sessions.add_app_credentials", add_app)
    monkeypatch.setattr("daimon.core.sessions.mirror_credentials_into_vault", mirror)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/sessions"
        body = json_body(request)
        bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_two_vaults",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    await create_session(
        build_fake_anthropic_http(handler),
        agent=_make_agent(anthropic_id="ag_two_vaults"),
        environment=_make_env(anthropic_id="env_two_vaults"),
        mcp_settings=McpSettings(
            jwt_secret=SecretStr("x" * 32), public_url=HttpUrl("https://mcp.example.com/mcp")
        ),
        account_id=account_id,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
    )
    assert bodies[0]["vault_ids"] == ["vlt_caller", "vlt_session"]
    assert mirror.await_args.kwargs["vault_id"] == "vlt_caller"
    assert add_app.await_args.kwargs["vault_id"] == "vlt_session"


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
                            "metadata": {"daimon_chat_identity": str(agent_uuid)},
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


def _capturing_memory_client(
    mem_state: FakeMemoryStoreState,
    bodies: list[dict[str, Any]],
    *,
    refuse_personal: bool = False,
) -> AsyncAnthropic:
    """Memory-store endpoints plus a session create that records its body."""
    memory_handler = make_fake_memory_store_handler(mem_state)

    def handler(request: httpx.Request) -> httpx.Response:
        if (
            refuse_personal
            and request.method == "POST"
            and request.url.path == "/v1/memory_stores"
            and str(json_body(request).get("name", "")).startswith("personal ")
        ):
            return httpx.Response(
                500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}
            )
        try:
            return memory_handler(request)
        except NotHandled:
            pass
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json_body(request)
            bodies.append(body)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id=f"sess_{len(bodies)}",
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    return build_fake_anthropic_http(handler)


def _memory_mounts(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in body.get("resources", []) if r.get("type") == "memory_store"]


@pytest.mark.parametrize("read_only", [False, True])
async def test_personal_memory_mounts_the_callers_own_store_beside_the_shared_one(
    read_only: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two people talking to one agent share its store and each get their own.
    Neither session carries the other's personal store: the isolation is the
    mount, not the prompt."""
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    bob = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    agent_uuid = uuid.uuid4()
    mem_state = FakeMemoryStoreState()
    bodies: list[dict[str, Any]] = []
    client = _capturing_memory_client(mem_state, bodies)

    for account in (alice, bob):
        await create_session(
            client,
            agent=_make_agent(anthropic_id="ag_personal", name="daimon-personal"),
            environment=_make_env(anthropic_id="env_personal"),
            account_id=account.id,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            session_factory=db_session_factory,
            memory_read_only=read_only,
            personal_memory=True,
        )

    (alice_shared, alice_personal), (bob_shared, bob_personal) = (
        _memory_mounts(body) for body in bodies
    )
    assert alice_shared["memory_store_id"] == bob_shared["memory_store_id"]
    assert alice_personal["memory_store_id"] != bob_personal["memory_store_id"]
    assert mem_state.stores[alice_personal["memory_store_id"]]["metadata"]["daimon_account"] == (
        str(alice.id)
    )
    access = "read_only" if read_only else "read_write"
    assert {m["access"] for m in (alice_shared, alice_personal)} == {access}, (
        "a read-only conversation keeps the personal store read-only too"
    )
    assert alice_shared["instructions"] == (
        READ_ONLY_MEMORY_INSTRUCTIONS if read_only else MEMORY_INSTRUCTIONS
    )
    assert alice_personal["instructions"] == (
        READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS if read_only else PERSONAL_MEMORY_INSTRUCTIONS
    )


async def test_without_personal_memory_only_the_shared_store_is_mounted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Routines, MCP and CLI sessions: the shared store alone, telling the agent
    not to record anything personal."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    mem_state = FakeMemoryStoreState()
    bodies: list[dict[str, Any]] = []

    await create_session(
        _capturing_memory_client(mem_state, bodies),
        agent=_make_agent(anthropic_id="ag_shared_only"),
        environment=_make_env(anthropic_id="env_shared_only"),
        account_id=account.id,
        tenant_id=tenant.id,
        agent_uuid=uuid.uuid4(),
        session_factory=db_session_factory,
    )

    (shared,) = _memory_mounts(bodies[0])
    assert shared["instructions"] == SHARED_ONLY_MEMORY_INSTRUCTIONS
    assert len(mem_state.stores) == 1, "no personal store is provisioned"


async def test_a_personal_store_outage_degrades_to_shared_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    bodies: list[dict[str, Any]] = []

    await create_session(
        _capturing_memory_client(FakeMemoryStoreState(), bodies, refuse_personal=True),
        agent=_make_agent(anthropic_id="ag_personal_down"),
        environment=_make_env(anthropic_id="env_personal_down"),
        account_id=account.id,
        tenant_id=tenant.id,
        agent_uuid=uuid.uuid4(),
        session_factory=db_session_factory,
        personal_memory=True,
    )

    (shared,) = _memory_mounts(bodies[0])
    assert shared["instructions"] == SHARED_ONLY_MEMORY_INSTRUCTIONS, (
        "with no personal store the shared one must not point personal facts anywhere"
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
                            "metadata": {"daimon_chat_identity": str(agent_uuid)},
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
            sent_agent = body["agent"]
            return httpx.Response(
                200,
                json=_session_body(
                    session_id=session_id,
                    agent_id=sent_agent if isinstance(sent_agent, str) else sent_agent["id"],
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


async def _record_personal_sign_in(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_uuid: uuid.UUID
) -> None:
    """One member's finished OAuth sign-in for the agent's `docs` server."""
    now = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    request = await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        account_id=account_id,
        target="docs",
        mcp_server_url="https://mcp.example.com/docs",
        requester_platform_user_id="requester-connected",
        channel_id="chan-1",
        expires_at=now + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_personal",
        target_name="daimon",
        requested_work=None,
    )
    flow = await flows_store.create_flow(
        session,
        state="st_" + uuid.uuid4().hex,
        request_token=request.token,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_uuid,
        server_name="docs",
        mcp_server_url="https://mcp.example.com/docs",
        redirect_uri="https://d.example/oauth/mcp/callback",
        code_verifier="verifier",
        expires_at=now + timedelta(minutes=10),
    )
    await flows_store.consume_flow(session, state=flow.state, now=now)
    await flows_store.mark_flow_completed(session, state=flow.state, now=now)
    await session.commit()


def _agent_with_personal_server() -> BetaManagedAgentsAgent:
    """An agent carrying a `docs` server one member connected by OAuth."""
    toolset: dict[str, Any] = {
        "type": "mcp_toolset",
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }
    return ma_agent(
        id="ag_personal",
        name="a",
        model="claude-opus-4-7",
        mcp_servers=[
            {"name": "docs", "type": "url", "url": "https://mcp.example.com/docs"},
            {"name": "daimon-mcp", "type": "url", "url": "https://mcp.example.com/mcp"},
        ],
        tools=[
            {**toolset, "mcp_server_name": "docs"},
            {**toolset, "mcp_server_name": "daimon-mcp"},
        ],
    )


async def test_create_session_leaves_off_a_server_only_another_member_signed_in_to(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An OAuth grant lives in one member's vault, but the server it unlocked
    is attached to the agent everyone shares. Mounting it on a bystander's
    session only earns them a failed MCP init and the degraded-turn notice
    under every reply."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    connected_account_id = uuid.UUID("00000000-0000-0000-0000-00000000dd01")
    bystander_account_id = uuid.UUID("00000000-0000-0000-0000-00000000dd02")
    public_url = "https://mcp.example.com/mcp"
    await _record_personal_sign_in(
        db_session,
        tenant_id=tenant.id,
        account_id=connected_account_id,
        agent_uuid=agent_uuid,
    )

    session_bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _warm_vault_mirror_handler(
            account_id=bystander_account_id,
            agent_uuid=agent_uuid,
            public_url=public_url,
            credential_post_bodies=[],
            credential_deletes=[],
            session_create_bodies=session_bodies,
            session_id="sess_bystander",
        )
    )

    await create_session(
        client,
        agent=_agent_with_personal_server(),
        environment=_make_env(anthropic_id="env_personal"),
        account_id=bystander_account_id,
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
    )

    sent_agent = session_bodies[0]["agent"]
    assert sent_agent["type"] == "agent_with_overrides", (
        "a bystander's session must override the agent's server list"
    )
    assert sent_agent["id"] == "ag_personal" and "version" not in sent_agent, (
        "the overrides name the agent and let MA pin the latest version, as a bare id does"
    )
    assert [server["name"] for server in sent_agent["mcp_servers"]] == ["daimon-mcp"], (
        "the personally-connected server is left off"
    )
    assert [tool["mcp_server_name"] for tool in sent_agent["tools"]] == ["daimon-mcp"], (
        "and its toolset with it — MA rejects a toolset whose server is absent"
    )


async def test_create_session_keeps_the_server_for_the_member_who_signed_in(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Hiding is per caller: the person who drove the sign-in still gets the
    server, and their session is created the plain way — by agent id."""
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    connected_account_id = uuid.UUID("00000000-0000-0000-0000-00000000dd03")
    public_url = "https://mcp.example.com/mcp"
    await _record_personal_sign_in(
        db_session,
        tenant_id=tenant.id,
        account_id=connected_account_id,
        agent_uuid=agent_uuid,
    )

    session_bodies: list[dict[str, Any]] = []
    client = build_fake_anthropic_http(
        _warm_vault_mirror_handler(
            account_id=connected_account_id,
            agent_uuid=agent_uuid,
            public_url=public_url,
            credential_post_bodies=[],
            credential_deletes=[],
            session_create_bodies=session_bodies,
            session_id="sess_connected",
        )
    )

    await create_session(
        client,
        agent=_agent_with_personal_server(),
        environment=_make_env(anthropic_id="env_personal"),
        account_id=connected_account_id,
        mcp_settings=McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(public_url)),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
    )

    assert session_bodies[0]["agent"] == "ag_personal", (
        "nothing hidden means the call shape does not change at all"
    )


@pytest.mark.parametrize(
    "warm,read_only", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_read_only_source_projection_keeps_implicit_memory_without_provisioning(
    db_preflight, db_nullpool_engine, warm, read_only, monkeypatch
):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.memory_resource import memory_instructions
    from daimon.core.mux_backend import resource_scope
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores import agent_memory_stores
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.ma_transport import ScriptedTransport

    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    agent = ma_agent(id="agent-source", tenant_id=str(tenant.id), name="source")
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    if warm:
        async with sessions.begin() as db:
            await agent_memory_stores.insert_memory_store(
                db, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id="mem-existing"
            )

    def no_materialization(*args, **kwargs):
        raise AssertionError("read-only projection attempted a materialization or decryption")

    monkeypatch.setattr("daimon.core.memory_resource.create_memory_store", no_materialization)
    monkeypatch.setattr("daimon.core.credential_env.upload_env_file", no_materialization)
    monkeypatch.setattr("daimon.core.github_app_session.prepare_app_access", no_materialization)
    transport = ScriptedTransport()
    async with transport.client() as client:
        source = await load_backend_agent(
            client, sessions, tenant_id=tenant.id, agent_name="source", actual_agent=agent
        )
        projected = await project_session_resources(
            source,
            agent=agent,
            environment=ma_environment(tenant_id=str(tenant.id)),
            session_factory=sessions,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            account_id=account.id,
            memory_read_only=read_only,
            resource_scope=resource_scope(tenant_id=str(tenant.id), account_id=str(account.id)),
        )
    assert projected.memory_required is True
    assert projected.memory_store_id == ("mem-existing" if warm else None)
    assert projected.memory_read_only is read_only
    assert projected.memory_instructions == memory_instructions(read_only=read_only)
    assert projected.environment.spec.network.mode == "unrestricted"
    assert projected.environment.native["config"]["networking"]["type"] == "unrestricted"
    assert projected.resource_scope.tenant_id == str(tenant.id)
    assert projected.resource_scope.account_id == str(account.id)
    async with sessions() as db:
        assert await agent_memory_stores.get_memory_store_id(
            db, tenant_id=tenant.id, agent_id=agent_uuid
        ) == ("mem-existing" if warm else None)
    assert not transport.requests and not transport.violations


@pytest.mark.parametrize(
    "changed", ["tenant", "identity", "version", "instructions", "archived_environment"]
)
async def test_source_projection_refuses_changed_source_before_database_or_native_io(changed):
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_backend import resource_scope
    from daimon.core.mux_compat import source_agent_record
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores.domain import BackendAgent
    from mux.errors import ScopeViolation

    class NoDatabase:
        def __call__(self):
            raise AssertionError("foreign/stale source read a database")

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    agent = ma_agent(id="agent-source", tenant_id=str(tenant_id), name="source")
    source = BackendAgent(
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id),
        name="source",
        configuration_digest="preview",
        source=source_agent_record(agent, resource_scope(tenant_id=str(tenant_id))),
    )
    environment = ma_environment(tenant_id=str(tenant_id))
    if changed == "tenant":
        tenant_id = uuid.uuid4()
    elif changed == "identity":
        agent = agent.model_copy(update={"id": "same-name-new-source"})
    elif changed == "version":
        agent = agent.model_copy(update={"version": agent.version + 1})
    elif changed == "instructions":
        agent = agent.model_copy(update={"system": "changed source intent"})
    else:
        environment = environment.model_copy(update={"archived_at": "2026-10-12T00:00:00Z"})
    with pytest.raises(ScopeViolation):
        await project_session_resources(
            source,
            agent=agent,
            environment=environment,
            session_factory=NoDatabase(),
            tenant_id=tenant_id,
            agent_uuid=source.agent_id,
            account_id=account_id,
            memory_read_only=False,
            resource_scope=resource_scope(tenant_id=str(tenant_id), account_id=str(account_id)),
        )


async def test_projection_reads_bound_resource_intent_without_resolving_secrets(
    db_preflight, db_nullpool_engine, monkeypatch
):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_backend import resource_scope
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores import agent_mcp_credentials as credentials
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.ma_transport import ScriptedTransport

    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        agent = ma_agent(id="agent-resources", tenant_id=str(tenant.id), name="source")
        identity = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
        await put_agent_file(
            db,
            tenant_id=tenant.id,
            agent_id=identity,
            key="SOURCE_KEY",
            content="private-env-value",
            set_by_account_id=None,
        )
        credential = await credentials.upsert_credential(
            db,
            tenant_id=tenant.id,
            agent_id=identity,
            mcp_server_url="https://source.example.test",
            encrypted_token=b"private-ciphertext",
        )
        await repo_binding_store.set_binding(
            db,
            tenant_id=tenant.id,
            agent_id=identity,
            repo_url="owner/source",
            default_branch="work",
            ma_secret_ref="owned-repo-credential",
            proof=None,
        )

    def no_secret_resolution(*args, **kwargs):
        raise AssertionError("projection tried to resolve or export a secret")

    monkeypatch.setattr("daimon.core.stores.agent_files.decode_value", no_secret_resolution)
    monkeypatch.setattr("daimon.core.sessions.get_pat", no_secret_resolution)
    monkeypatch.setattr("daimon.core.sessions.resolve_clone_token", no_secret_resolution)
    transport = ScriptedTransport()
    async with transport.client() as client:
        source = await load_backend_agent(
            client, sessions, tenant_id=tenant.id, agent_name="source", actual_agent=agent
        )
        projected = await project_session_resources(
            source,
            agent=agent,
            environment=ma_environment(tenant_id=str(tenant.id)),
            session_factory=sessions,
            tenant_id=tenant.id,
            agent_uuid=identity,
            account_id=account.id,
            resource_scope=resource_scope(tenant_id=str(tenant.id), account_id=str(account.id)),
            memory_read_only=True,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            mcp_settings=McpSettings(
                public_url=HttpUrl("https://mcp.example.test/mcp"), jwt_secret=SecretStr("test-jwt")
            ),
        )
    assert {resource.credential_ref for resource in projected.resource_requirements} == {
        f"agent-env:{tenant.id}:{identity}:SOURCE_KEY",
        f"agent-mcp:{credential.id}",
        f"caller-mcp:{tenant.id}:{account.id}:{identity}",
    }
    assert projected.repository_sources[0].repository_url == "https://github.com/owner/source"
    assert projected.repository_sources[0].ref == "work"
    assert len(projected.resource_generations) == 3
    for output in (repr(projected), projected.model_dump_json()):
        assert "private-env-value" not in output and "private-ciphertext" not in output
        assert "owned-repo-credential" not in output and "SOURCE_KEY" not in output
    assert not transport.requests and not transport.violations


@pytest.mark.parametrize("app_mode", [False, True])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("crypto_available", [False, True])
async def test_projection_uses_composer_vault_copilot_and_app_filters(
    db_preflight, db_nullpool_engine, app_mode, configured, crypto_available, monkeypatch
):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mcp_vault import GITHUB_COPILOT_MCP_URL
    from daimon.core.mux_backend import resource_scope
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores import agent_mcp_credentials as credentials
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.ma_transport import ScriptedTransport

    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        foreign = await make_tenant(db)
        foreign_account = await make_account(db, tenant=foreign)
        agent = ma_agent(id="agent-composer", tenant_id=str(tenant.id), name="source")
        identity = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
        db.add(
            AgentGitHubMode(
                tenant_id=tenant.id, agent_id=identity, mode="app" if app_mode else "legacy"
            )
        )
        await github_binding_store.set_agent_github_binding(
            db, agent_id=identity, principal_id=account.id
        )
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "SOURCE_KEY"):
            await put_agent_file(
                db,
                tenant_id=tenant.id,
                agent_id=identity,
                key=key,
                content="private-env-value",
                set_by_account_id=None,
            )
        copilot = await credentials.upsert_credential(
            db,
            tenant_id=tenant.id,
            agent_id=identity,
            mcp_server_url=GITHUB_COPILOT_MCP_URL,
            encrypted_token=b"private-copilot",
        )
        external = await credentials.upsert_credential(
            db,
            tenant_id=tenant.id,
            agent_id=identity,
            mcp_server_url="https://external.example.test/mcp",
            encrypted_token=b"private-external",
        )
        other = await credentials.upsert_credential(
            db,
            tenant_id=foreign.id,
            agent_id=identity,
            mcp_server_url="https://foreign.example.test/mcp",
            encrypted_token=b"private-foreign",
        )
        await repo_binding_store.set_binding(
            db,
            tenant_id=tenant.id,
            agent_id=identity,
            repo_url="owner/legacy",
            default_branch="work",
            ma_secret_ref="owned-repo-credential",
            proof=None,
        )
    await upsert_credential_encrypted(
        sessionmaker=sessions,
        fernet=fernet,
        principal_id=account.id,
        github_login="owned",
        plaintext_token="private-owned-pat",
        scopes=("repo",),
    )
    await upsert_credential_encrypted(
        sessionmaker=sessions,
        fernet=fernet,
        principal_id=foreign_account.id,
        github_login="foreign",
        plaintext_token="private-foreign-pat",
        scopes=("repo",),
    )

    def no_secret_or_materialization(*args, **kwargs):
        raise AssertionError("projection resolved a secret or materialized a resource")

    for path in (
        "daimon.core.github_credentials.decrypt_token",
        "daimon.core.stores.agent_files.decode_value",
        "daimon.core.stores.github_credentials.get_credential_by_principal",
        "daimon.core.stores.agent_mcp_credentials.list_credentials",
        "daimon.core.sessions.create_session_vault",
        "daimon.core.sessions.ensure_agent_mcp_vault",
        "daimon.core.sessions.add_github_copilot_credential",
        "daimon.core.sessions.prepare_app_access",
        "daimon.core.sessions.resolve_clone_token",
    ):
        monkeypatch.setattr(path, no_secret_or_materialization)
    mcp = McpSettings(
        public_url=HttpUrl("https://mcp.example.test/mcp"),
        jwt_secret=SecretStr("private-jwt") if configured else None,
    )
    transport = ScriptedTransport()
    async with transport.client() as client:
        source = await load_backend_agent(
            client, sessions, tenant_id=tenant.id, agent_name="source", actual_agent=agent
        )
        projected = await project_session_resources(
            source,
            agent=agent,
            environment=ma_environment(tenant_id=str(tenant.id)),
            session_factory=sessions,
            tenant_id=tenant.id,
            agent_uuid=identity,
            account_id=account.id,
            resource_scope=resource_scope(tenant_id=str(tenant.id), account_id=str(account.id)),
            memory_read_only=True,
            mcp_settings=mcp,
            fernet=fernet if crypto_available else None,
        )
    refs = {r.id for r in projected.resource_requirements}
    assert (f"caller-mcp:{tenant.id}:{account.id}:{identity}" in refs) is configured
    assert (f"app-session-vault:{tenant.id}:{identity}" in refs) is app_mode
    assert ("github-copilot-mirror" in refs) is (configured and crypto_available and not app_mode)
    assert (f"github-credential:{account.id}" in refs) is (crypto_available and not app_mode)
    assert (f"agent-mcp:{copilot.id}" in refs) is (configured and crypto_available and not app_mode)
    assert (f"agent-mcp:{external.id}" in refs) is (configured and crypto_available)
    assert f"agent-mcp:{other.id}" not in refs
    assert not any(str(foreign_account.id) in ref for ref in refs)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        assert (f"agent-env:{tenant.id}:{identity}:{key}" in refs) is (not app_mode)
    assert f"agent-env:{tenant.id}:{identity}:SOURCE_KEY" in refs
    assert bool(projected.repository_sources) is (not app_mode)
    if not app_mode:
        assert projected.repository_sources[0].repository_url == "https://github.com/owner/legacy"
    assert projected.memory_required is True
    assert projected.memory_store_id is None
    assert not transport.requests and not transport.violations
    for output in (repr(projected), projected.model_dump_json()):
        assert "private-" not in output and "owned-repo-credential" not in output


async def worker_source_context(
    db_nullpool_engine,
    *,
    original_tools=(),
    granted_tools=(),
    environment_config=None,
    warm=False,
    native_budget=None,
):
    from dataclasses import replace

    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_backend import managed_agents, resource_ref
    from daimon.core.neutral_resources import resolve_worker_spec
    from daimon.core.sessions import project_session_resources
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.ma_transport import ScriptedTransport
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.ids import ChannelRef, Scope, ThreadRef
    from mux.contracts.resources import AgentSpec, ResourceBinding

    from .test_fleet_permissions import POLICY, parent

    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    original = ma_agent(
        id="original-source",
        tenant_id=str(tenant.id),
        name="source",
        system="Protected root instruction",
        tools=original_tools,
    )
    environment = ma_environment(tenant_id=str(tenant.id))
    if environment_config is not None:
        environment = BetaEnvironment.model_validate(
            {**environment.model_dump(mode="json"), "config": environment_config}
        )
    root = parent()
    root = replace(
        root,
        spec=AgentSpec(
            name="worker",
            model=root.spec.model,
            system=root.base_system,
            metadata={"daimon_tenant": str(tenant.id)},
            tools=granted_tools or None,
        ),
        fleet=replace(
            root.fleet,
            scope=Scope(
                tenant_id=str(tenant.id),
                account_id=str(account.id),
                principal_id="caller",
                authorization_id="root",
            ),
            account_id=account.id,
            root=ThreadRef(
                channel=ChannelRef(
                    tenant_id=str(tenant.id), platform="discord", channel_id="channel"
                ),
                thread_id="thread",
            ),
        ),
    )
    if native_budget is not None:
        root = replace(
            root,
            limits=type(root.limits).model_validate(
                root.limits.model_dump()
                | {
                    "native_budget_usd": native_budget,
                    "max_turns": 1,
                    "deadline": datetime(2026, 10, 12, 18, tzinfo=UTC) + timedelta(seconds=120),
                }
            ),
        )
    worker = resolve_worker_spec(
        root, root.spec, policy=POLICY, now=datetime(2026, 10, 12, 18, tzinfo=UTC)
    )
    from daimon.core.memory_resource import ensure_memory_store_and_mount
    from daimon.testing.ma import MARouter

    router = MARouter()
    memory_handler = make_fake_memory_store_handler(FakeMemoryStoreState())
    router.add("POST", r"/v1/memory_stores", lambda request, match: memory_handler(request))
    trace = ScriptedTransport(router=router)
    async with trace.client() as client:
        captured = await load_backend_agent(
            client, sessions, tenant_id=tenant.id, agent_name="source", actual_agent=original
        )
        if warm:
            mount = await ensure_memory_store_and_mount(
                client,
                sessions,
                tenant_id=tenant.id,
                agent_id=captured.agent_id,
                agent_name="source",
                read_only=True,
            )
            backend = managed_agents(client, scope=root.fleet.scope)
            binding = ResourceBinding(
                id=mount["memory_store_id"],
                kind="native",
                resource=resource_ref(
                    backend, "memory_store", mount["memory_store_id"], scope=root.fleet.scope
                ),
                native=ExtensionConfig(
                    namespace="anthropic.session_resource_create",
                    version=1,
                    value={"resource": mount},
                ),
            )
            root = replace(root, resources=(binding,))
            worker = resolve_worker_spec(
                root,
                root.spec,
                resources=root.resources,
                policy=POLICY,
                now=datetime(2026, 10, 12, 18, tzinfo=UTC),
            )
        projected = await project_session_resources(
            captured,
            agent=original,
            environment=environment,
            session_factory=sessions,
            tenant_id=tenant.id,
            agent_uuid=captured.agent_id,
            account_id=account.id,
            memory_read_only=True,
            resource_scope=worker.permissions.fleet.scope,
        )
    assert captured.agent_id == derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=original.id)
    return sessions, tenant.id, account.id, original, environment, worker, projected


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "session",
        "agent",
        "environment",
        "tenant",
        "account",
        "memory",
        "access",
        "instructions",
        "missing_mount",
        "duplicate_mount",
        "environment_snapshot",
        "row",
        "memory_cleared_before_read",
        "memory_rebound_before_read",
        *(
            f"{stage}_{field}"
            for stage in ("ack", "get")
            for field in (
                "model",
                "options",
                "system",
                "version",
                "tools",
                "mcp",
                "skills",
                "missing",
            )
        ),
    ],
)
@pytest.mark.parametrize("native_budget", [None, Decimal("0.01")])
async def test_worker_executes_new_agent_with_original_source_memory_and_resource_identity(
    db_preflight, db_nullpool_engine, fault, native_budget
):
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_compat import create_agent
    from daimon.core.stores.agent_memory_stores import get_memory_store_id
    from daimon.testing.ma_transport import ScriptedTransport

    (
        sessions,
        tenant_id,
        account_id,
        original,
        environment,
        worker,
        source,
    ) = await worker_source_context(db_nullpool_engine, warm=True, native_budget=native_budget)
    executing = ma_agent(
        id="actual-worker",
        tenant_id=str(tenant_id),
        name="worker",
        model=worker.spec.model.id,
        system=worker.spec.system,
    )
    transport = ScriptedTransport(router=None)
    # Provisioning already ran through the real SDK/store; readback must preserve its mount.
    from daimon.testing.ma import MARouter

    router = MARouter()
    snapshots = {}

    def drift(raw, stage):
        if fault == f"{stage}_budget":
            raw["budget"]["max_list_cost"]["amount"] = "2"
        if fault == f"{stage}_budget_missing":
            raw.pop("budget")
        if fault == f"{stage}_model":
            raw["agent"]["model"]["id"] = "claude-opus-4-6"
        elif fault == f"{stage}_options":
            raw["agent"]["model"]["speed"] = "fast"
        elif fault == f"{stage}_system":
            raw["agent"]["system"] = "unapproved protected instructions"
        elif fault == f"{stage}_version":
            raw["agent"]["version"] = 99
        elif fault == f"{stage}_tools":
            raw["agent"]["tools"] = [{"type": "bash", "name": "bash"}]
        elif fault == f"{stage}_mcp":
            raw["agent"]["mcp_servers"] = [
                {"type": "url", "name": "unapproved", "url": "https://unapproved.invalid/mcp"}
            ]
        elif fault == f"{stage}_skills":
            raw["agent"]["skills"] = [{"type": "custom", "skill_id": "unapproved", "version": "1"}]
        elif fault == f"{stage}_missing":
            raw["agent"].pop("model")
        return raw

    def native(request, match):
        if request.method == "POST":
            body = json.loads(request.content)
            if request.url.path == "/v1/agents":
                return httpx.Response(
                    200, json=executing.model_dump(mode="json", exclude_unset=True)
                )
            result = _session_create_handler()(request).json()
            result.update(
                agent=executing.model_dump(mode="json"),
                metadata=body["metadata"],
                resources=body["resources"],
            )
            if "budget" in body:
                result["budget"] = body["budget"]
            snapshots[result["id"]] = result
            return httpx.Response(200, json=drift(json.loads(json.dumps(result)), "ack"))
        if "/environments/" in request.url.path:
            raw = environment.model_dump(mode="json", exclude_unset=True)
            if fault == "environment_snapshot":
                raw["description"] = "changed policy"
            return httpx.Response(200, json=raw)
        raw = json.loads(json.dumps(snapshots[request.url.path.rsplit("/", 1)[-1]]))
        if fault == "session":
            raw["id"] = "foreign-session"
        elif fault == "agent":
            raw["agent"]["id"] = "foreign-agent"
        elif fault == "environment":
            raw["environment_id"] = "foreign-environment"
        elif fault == "tenant":
            raw["metadata"]["daimon_tenant"] = str(uuid.uuid4())
        elif fault == "account":
            raw["metadata"]["daimon_account"] = str(uuid.uuid4())
        elif fault == "memory":
            raw["resources"][0]["memory_store_id"] = "foreign-memory"
        elif fault == "access":
            raw["resources"][0]["access"] = "read_write"
        elif fault == "instructions":
            raw["resources"][0]["instructions"] = "altered instructions"
        elif fault == "missing_mount":
            raw["resources"] = []
        elif fault == "duplicate_mount":
            raw["resources"] *= 2
        return httpx.Response(200, json=drift(raw, "get"))

    router.add("POST", r"/v1/agents", native)
    router.add(
        "POST",
        r"/v1/sessions/[^/]+/archive",
        lambda request, match: httpx.Response(
            200,
            json={
                **snapshots[request.url.path.split("/")[-2]],
                "archived_at": datetime.now(UTC).isoformat(),
            },
        ),
    )
    router.add("POST", r"/v1/sessions", native)
    router.add("GET", r"/v1/sessions/.*", native)
    router.add("GET", r"/v1/environments/.*", native)
    transport.router = router
    guards = []

    async def fence():
        guards.append("fresh")
        if (
            fault in ("memory_cleared_before_read", "memory_rebound_before_read")
            and len(guards) == 1
        ):
            from daimon.core.stores.agent_memory_stores import (
                clear_memory_store,
                insert_memory_store,
            )

            async with sessions.begin() as db:
                await clear_memory_store(db, tenant_id=tenant_id, agent_id=source.agent_id)
                if fault == "memory_rebound_before_read":
                    await insert_memory_store(
                        db,
                        tenant_id=tenant_id,
                        agent_id=source.agent_id,
                        memory_store_id="rebound-memory",
                    )
        if fault == "row" and len(guards) == 2:
            from daimon.core.stores.agent_memory_stores import clear_memory_store

            async with sessions.begin() as db:
                await clear_memory_store(db, tenant_id=tenant_id, agent_id=source.agent_id)

    async with transport.client() as client:
        executing = await create_agent(
            client, worker.spec, scope=worker.permissions.fleet.scope, key="warm-worker-agent"
        )
        if fault is not None:
            from mux.errors import ScopeViolation

            with pytest.raises(ScopeViolation):
                await create_session(
                    client,
                    agent=executing,
                    environment=environment,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    agent_uuid=source.agent_id,
                    session_factory=sessions,
                    resource_source=source,
                    worker=worker,
                    before_create=fence,
                    memory_read_only=True,
                )
            expected_posts = (
                0 if fault in ("memory_cleared_before_read", "memory_rebound_before_read") else 1
            )
            assert (
                sum(request.path == "/v1/sessions" for request in transport.requests)
                == expected_posts
            )
            assert not any("/memory_stores" in request.path for request in transport.requests)
            assert sum(request.path.endswith("/archive") for request in transport.requests) == (
                0
                if fault
                in {
                    "session",
                    "agent",
                    "environment",
                    "tenant",
                    "account",
                    "memory_cleared_before_read",
                    "memory_rebound_before_read",
                }
                else 1
            )
            if expected_posts == 0:
                async with sessions() as db:
                    assert await get_memory_store_id(
                        db, tenant_id=tenant_id, agent_id=source.agent_id
                    ) == (None if fault == "memory_cleared_before_read" else "rebound-memory")
            assert not any("/events" in request.path for request in transport.requests)
            transport.assert_consumed()
            return
        created = await create_session(
            client,
            agent=executing,
            environment=environment,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_uuid=source.agent_id,
            session_factory=sessions,
            resource_source=source,
            worker=worker,
            before_create=fence,
            memory_read_only=True,
        )
    transport.assert_consumed()
    assert created.agent.id == executing.id
    session_request = next(
        request.json() for request in transport.requests if request.path == "/v1/sessions"
    )
    assert session_request["agent"] == executing.id
    if native_budget is not None:
        assert worker.limits.max_turns == 1
        assert (
            worker.limits.deadline - datetime(2026, 10, 12, 18, tzinfo=UTC)
        ).total_seconds() == 120
        assert session_request["budget"] == {
            "type": "limit",
            "max_list_cost": {"amount": "1", "currency": "USD"},
        }
    else:
        assert "budget" not in session_request
    assert guards == ["fresh", "fresh", "fresh"]
    async with sessions() as db:
        source_memory = await get_memory_store_id(db, tenant_id=tenant_id, agent_id=source.agent_id)
        assert source_memory is not None
        assert (
            await get_memory_store_id(
                db,
                tenant_id=tenant_id,
                agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=executing.id),
            )
            is None
        )
    mount = next(
        resource for resource in session_request["resources"] if resource["type"] == "memory_store"
    )
    assert mount["memory_store_id"] == source_memory and mount["access"] == "read_only"


@pytest.mark.parametrize(
    "changed",
    [
        "source_uuid",
        "tenant",
        "account",
        "snapshot",
        "environment",
        "environment_policy",
        "missing_environment",
        "environment_ref_kind",
        "environment_ref_provider",
        "environment_ref_tenant",
        "environment_ref_account",
        "environment_ref_space",
        "guard",
        "drops",
    ],
)
async def test_worker_source_context_refuses_before_resources_or_native_io(
    db_preflight, db_nullpool_engine, changed
):
    from dataclasses import replace

    from daimon.testing.ma_transport import ScriptedTransport
    from mux.errors import ScopeViolation, UnsupportedCapability

    (
        sessions,
        tenant_id,
        account_id,
        original,
        environment,
        worker,
        source,
    ) = await worker_source_context(db_nullpool_engine)
    executing = ma_agent(id="actual-worker", tenant_id=str(tenant_id), name="worker")
    source_uuid = source.agent_id

    async def fence():
        raise DaimonError("fresh source authority refused")

    if changed != "guard":

        async def fence():
            pass

    if changed == "source_uuid":
        source_uuid = uuid.uuid4()
    elif changed == "tenant":
        tenant_id = uuid.uuid4()
    elif changed == "account":
        account_id = uuid.uuid4()
    elif changed == "snapshot":
        source = source.model_copy(
            update={"source": source.source.model_copy(update={"native": None})}
        )
    elif changed == "environment":
        environment = ma_environment(id="neighbour-environment", tenant_id=str(tenant_id))
    elif changed == "environment_policy":
        environment = environment.model_copy(update={"description": "changed policy revision"})
    elif changed == "missing_environment":
        source = source.model_copy(update={"environment": None})
    elif changed.startswith("environment_ref_"):
        field = {
            "kind": "kind",
            "provider": "provider",
            "tenant": "tenant_id",
            "account": "account_id",
            "space": "account_scope_id",
        }[changed.removeprefix("environment_ref_")]
        source = source.model_copy(
            update={
                "environment": source.environment.model_copy(
                    update={"ref": source.environment.ref.model_copy(update={field: "foreign"})}
                )
            }
        )
    elif changed == "drops":
        worker = replace(
            worker, spec=worker.spec.model_copy(update={"declared_drops": ("memory",)})
        )
    trace = ScriptedTransport()
    async with trace.client() as client:
        with pytest.raises((ScopeViolation, UnsupportedCapability, DaimonError)):
            await create_session(
                client,
                agent=executing,
                environment=environment,
                tenant_id=tenant_id,
                account_id=account_id,
                agent_uuid=source_uuid,
                session_factory=sessions,
                resource_source=source,
                worker=worker,
                before_create=fence,
                memory_read_only=True,
            )
    assert trace.requests == []


@pytest.mark.parametrize(
    "fault",
    [
        "cold",
        "missing_grant",
        "duplicate",
        "tenant",
        "account",
        "provider",
        "memory",
        "access",
        "instructions",
        "version",
        "schema",
    ],
)
async def test_worker_memory_grant_refuses_before_resource_or_session_effects(
    db_preflight, db_nullpool_engine, fault
):
    from dataclasses import replace

    from daimon.testing.ma_transport import ScriptedTransport
    from mux.errors import ScopeViolation, UnsupportedCapability

    sessions, tenant, account, _, environment, worker, source = await worker_source_context(
        db_nullpool_engine, warm=fault != "cold"
    )
    if fault != "cold":
        binding = worker.resources[0]
        if fault == "missing_grant":
            resources = ()
        elif fault == "duplicate":
            resources = (binding, binding)
        else:
            if fault in {"tenant", "account", "provider", "memory"}:
                key = {
                    "tenant": "tenant_id",
                    "account": "account_id",
                    "provider": "provider",
                    "memory": "id",
                }[fault]
                binding = binding.model_copy(
                    update={"resource": binding.resource.model_copy(update={key: "foreign"})}
                )
            else:
                value = json.loads(json.dumps(dict(binding.native.value)))
                if fault == "access":
                    value["resource"]["access"] = "read_write"
                elif fault == "instructions":
                    value["resource"]["instructions"] = "changed"
                elif fault == "schema":
                    value["resource"]["unknown"] = "unrepresented"
                binding = binding.model_copy(
                    update={
                        "native": binding.native.model_copy(
                            update={"value": value, "version": 2 if fault == "version" else 1}
                        )
                    }
                )
            resources = (binding,)
        worker = replace(worker, resources=resources)
    trace = ScriptedTransport()

    async def guard():
        pass

    async with trace.client() as client:
        with pytest.raises((ScopeViolation, UnsupportedCapability)):
            await create_session(
                client,
                agent=ma_agent(id="actual-worker", tenant_id=str(tenant)),
                environment=environment,
                tenant_id=tenant,
                account_id=account,
                agent_uuid=source.agent_id,
                session_factory=sessions,
                worker=worker,
                resource_source=source,
                before_create=guard,
                memory_read_only=True,
            )
    assert trace.requests == []


async def test_worker_never_silently_loses_retained_memory_on_materialization_failure(
    db_preflight, db_nullpool_engine, monkeypatch
):
    from daimon.core.errors import StoreError
    from daimon.testing.ma_transport import ScriptedTransport

    (
        sessions,
        tenant_id,
        account_id,
        original,
        environment,
        worker,
        source,
    ) = await worker_source_context(db_nullpool_engine, warm=True)
    executing = ma_agent(id="actual-worker", tenant_id=str(tenant_id), name="worker")

    async def missing(*args, **kwargs):
        raise StoreError("retained memory unavailable")

    async def fence():
        pass

    monkeypatch.setattr("daimon.core.sessions.ensure_memory_store_and_mount", missing)
    trace = ScriptedTransport()
    async with trace.client() as client:
        with pytest.raises(StoreError):
            await create_session(
                client,
                agent=executing,
                environment=environment,
                tenant_id=tenant_id,
                account_id=account_id,
                agent_uuid=source.agent_id,
                session_factory=sessions,
                resource_source=source,
                worker=worker,
                before_create=fence,
                memory_read_only=True,
            )
    assert trace.requests == []


@pytest.mark.parametrize("custom", [False, True])
async def test_actual_sdk_projected_worker_source_normalizes_represented_native_intent(
    db_preflight, db_nullpool_engine, custom
):
    from dataclasses import replace

    from daimon.core.neutral_resources import resolve_worker_spec
    from daimon.core.turn.runtime_manifest import classify_source_obligations
    from mux.contracts.resources import ToolSpec
    from mux.drivers.anthropic.resources.agents import tool_payload

    from .test_fleet_permissions import NOW, POLICY

    tool = ToolSpec(
        name="owned-helper",
        kind="custom",
        description="Owned helper",
        input_schema={"type": "object"},
    )
    _, _, _, original, _, worker, captured = await worker_source_context(
        db_nullpool_engine,
        original_tools=(tool_payload(tool),) if custom else (),
        granted_tools=(tool,) if custom else (),
    )
    root = replace(
        worker.permissions,
        fleet=replace(worker.permissions.fleet, depth=0),
        agent_id=captured.agent_id,
    )
    namespaces = {item.namespace for item in captured.effective_spec.extensions}
    assert namespaces == {
        "anthropic.agent_tools",
        "anthropic.model_config",
        "anthropic.multiagent",
        "anthropic.agent_create_nulls",
    }
    assert captured.source.native == original.model_dump(mode="json", exclude_unset=True)
    requested = worker.spec.model_copy(update={"declared_drops": ("memory", "sandbox")})
    result = resolve_worker_spec(root, requested, policy=POLICY, now=NOW, captured_source=captured)
    assert result.spec.tools == ((tool,) if custom else ())
    assert result.spec.system.count("Protected root instruction") == 1
    assert not result.spec.extensions
    assert result.environment is None
    assert (
        classify_source_obligations(
            captured, result.spec, environment=result.environment, resources=result.resources
        )
        == frozenset()
    )
    assert (
        result.digest
        == resolve_worker_spec(
            root, requested, policy=POLICY, now=NOW, captured_source=captured
        ).digest
    )


@pytest.mark.parametrize(
    "failure", ["namespace", "model_option", "version", "tools", "memory", "sandbox"]
)
async def test_actual_sdk_source_unrepresented_intent_refuses_before_allocation(
    db_preflight, db_nullpool_engine, failure
):
    from dataclasses import replace

    from daimon.core.neutral_resources import WorkerSpecRefused, resolve_worker_spec
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.resources import ToolSpec
    from mux.drivers.anthropic.resources.agents import tool_payload

    from .test_fleet_permissions import NOW, POLICY

    tool = ToolSpec(
        name="owned-helper",
        kind="custom",
        description="Owned helper",
        input_schema={"type": "object"},
    )
    _, _, _, _, _, worker, captured = await worker_source_context(
        db_nullpool_engine, original_tools=(tool_payload(tool),), granted_tools=(tool,)
    )
    root = replace(
        worker.permissions,
        fleet=replace(worker.permissions.fleet, depth=0),
        agent_id=captured.agent_id,
    )
    requested = worker.spec.model_copy(update={"declared_drops": ("memory", "sandbox")})
    if failure in ("namespace", "model_option", "version"):
        extensions = captured.effective_spec.extensions
        if failure == "namespace":
            extensions += (ExtensionConfig(namespace="anthropic.opaque", version=1, value={}),)
        elif failure == "model_option":
            extensions = tuple(
                item.model_copy(update={"value": {"model": {"id": "other", "speed": "fast"}}})
                if item.namespace == "anthropic.model_config"
                else item
                for item in extensions
            )
        else:
            extensions = tuple(item.model_copy(update={"version": 2}) for item in extensions)
        captured = captured.model_copy(
            update={
                "effective_spec": captured.effective_spec.model_copy(
                    update={"extensions": extensions}
                )
            }
        )
    elif failure == "tools":
        requested = requested.model_copy(update={"tools": ()})
    else:
        requested = requested.model_copy(
            update={
                "declared_drops": tuple(
                    item for item in requested.declared_drops if item != failure
                )
            }
        )
    with pytest.raises(WorkerSpecRefused):
        resolve_worker_spec(root, requested, policy=POLICY, now=NOW, captured_source=captured)


@pytest.mark.parametrize("network", ["unrestricted", "limited"])
async def test_actual_sdk_source_explicit_cloud_policy_is_preserved_by_normalization(
    db_preflight, db_nullpool_engine, network
):
    from dataclasses import replace

    from daimon.core.neutral_resources import resolve_worker_spec
    from daimon.core.turn.runtime_manifest import classify_source_obligations

    from .test_fleet_permissions import NOW, POLICY

    networking = {"type": network}
    if network == "limited":
        networking.update(
            allowed_hosts=["pypi.org"], allow_mcp_servers=False, allow_package_managers=False
        )
    config = {
        "type": "cloud",
        "networking": networking,
        "packages": {
            "apt": ["git"],
            "pip": ["pandas"],
            "npm": [],
            "cargo": [],
            "gem": [],
            "go": [],
        },
    }
    _, _, _, _, environment, worker, captured = await worker_source_context(
        db_nullpool_engine, environment_config=config
    )
    assert captured.environment.native == environment.model_dump(mode="json", exclude_unset=True)
    projected = captured.environment.spec
    assert projected.native_config is None
    assert projected.network.mode == network
    assert projected.packages == {"system": ("git",), "python": ("pandas",), "npm": ()}
    assert projected.sources == ()
    assert projected.metadata == environment.metadata
    root = replace(
        worker.permissions,
        fleet=replace(worker.permissions.fleet, depth=0),
        agent_id=captured.agent_id,
        environment=projected,
    )
    requested = worker.spec.model_copy(update={"declared_drops": ("memory",)})
    resolved = resolve_worker_spec(
        root, requested, policy=POLICY, now=NOW, captured_source=captured
    )
    assert resolved.environment == projected
    assert not classify_source_obligations(
        captured, resolved.spec, environment=resolved.environment
    )


@pytest.mark.parametrize(
    "failure",
    [
        "missing_network",
        "missing_packages",
        "package_default",
        "extra_manager",
        "network_permission",
        "network_default",
        "unknown",
        "self_hosted",
    ],
)
async def test_source_environment_projection_retains_unrepresented_native_policy(failure):
    from daimon.core.mux_compat import retrieve_environment, source_environment_record
    from daimon.testing.ma import MARouter
    from daimon.testing.ma_transport import ScriptedTransport
    from mux.contracts.ids import Scope

    config = {
        "type": "cloud",
        "networking": {
            "type": "limited",
            "allowed_hosts": [],
            "allow_mcp_servers": False,
            "allow_package_managers": False,
        },
        "packages": {"apt": [], "pip": [], "npm": [], "cargo": [], "gem": [], "go": []},
    }
    if failure == "missing_network":
        del config["networking"]
    elif failure == "missing_packages":
        del config["packages"]
    elif failure == "package_default":
        del config["packages"]["pip"]
    elif failure == "extra_manager":
        config["packages"]["cargo"] = ["ripgrep"]
    elif failure == "network_permission":
        config["networking"]["allow_mcp_servers"] = True
    elif failure == "network_default":
        del config["networking"]["allow_package_managers"]
    elif failure == "unknown":
        config["future_policy"] = {"secrets": "unrepresented"}
    else:
        config = {"type": "self_hosted"}
    raw = ma_environment(tenant_id="tenant").model_dump(mode="json")
    raw["config"] = config
    scope = Scope(
        tenant_id="tenant", account_id="account", principal_id="caller", authorization_id="owned"
    )
    router = MARouter()
    router.add("GET", r"/v1/environments/.*", lambda request, match: httpx.Response(200, json=raw))
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        environment = await retrieve_environment(client, raw["id"], scope=scope)
    projected = source_environment_record(environment, scope)
    assert projected.spec.native_config is not None
    assert projected.spec.native_config.value["config"] == config
    assert projected.native == environment.model_dump(mode="json", exclude_unset=True)
    transport.assert_consumed()
    assert [request.method for request in transport.requests] == ["GET"]


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing_capture",
        "missing_grant",
        "foreign_account",
        "unbound_account",
        "foreign_tenant",
        "capture_scope",
        "stale_revision",
    ],
)
async def test_actual_tenant_scoped_source_clone_requires_the_root_account_capture_and_pin(
    db_preflight, db_nullpool_engine, failure
):
    from dataclasses import replace

    from daimon.core.mux_backend import resource_scope
    from daimon.core.neutral_resources import WorkerSpecRefused, resolve_worker_spec
    from mux.contracts.resources import AgentPatch

    from .test_fleet_permissions import NOW, POLICY

    _, _, _, original, _, worker, captured = await worker_source_context(db_nullpool_engine)
    actual = captured.source
    assert actual.ref.account_id == resource_scope(tenant_id=actual.ref.tenant_id).account_id
    assert actual.native == original.model_dump(mode="json", exclude_unset=True)
    root = replace(
        worker.permissions,
        fleet=replace(worker.permissions.fleet, depth=0),
        agent_id=captured.agent_id,
        spec=worker.spec.model_copy(update={"metadata": actual.spec.metadata}),
        source_agents=(actual.ref,),
        source_revisions=((actual.ref, actual.revision),),
    )
    desired = root.spec.model_copy(update={"declared_drops": ("memory", "sandbox")})
    if failure == "missing_capture":
        captured = None
    elif failure == "missing_grant":
        root = replace(root, source_agents=())
    elif failure in {"foreign_account", "unbound_account", "foreign_tenant"}:
        ref = actual.ref.model_copy(
            update={
                "tenant_id" if failure == "foreign_tenant" else "account_id": None
                if failure == "unbound_account"
                else str(uuid.uuid4())
            }
        )
        actual = actual.model_copy(update={"ref": ref})
        captured = captured.model_copy(update={"source": actual})
        root = replace(root, source_agents=(ref,), source_revisions=((ref, actual.revision),))
    elif failure == "capture_scope":
        captured = captured.model_copy(
            update={
                "resource_scope": captured.resource_scope.model_copy(
                    update={"account_id": str(uuid.uuid4())}
                )
            }
        )
    elif failure == "stale_revision":
        root = replace(root, source_revisions=())
    kwargs = {
        "policy": POLICY,
        "now": NOW,
        "captured_source": captured,
        "overrides": AgentPatch.model_validate(desired.model_dump(mode="json")),
    }
    if failure is not None:
        with pytest.raises(WorkerSpecRefused):
            resolve_worker_spec(root, actual, **kwargs)
        return
    result = resolve_worker_spec(root, actual, **kwargs)
    assert result.source_agent == (actual.ref, actual.revision)
    assert result.source_agent[0] == actual.ref
    assert result.permissions.fleet.scope.account_id == root.fleet.scope.account_id
    assert result.spec.metadata == root.spec.metadata
    assert result.digest == resolve_worker_spec(root, actual, **kwargs).digest


@pytest.mark.parametrize(
    "fault", ["ack_budget", "get_budget", "ack_budget_missing", "get_budget_missing"]
)
async def test_worker_budget_echo_mismatch_refuses_owned_session(
    db_preflight, db_nullpool_engine, fault
):
    await test_worker_executes_new_agent_with_original_source_memory_and_resource_identity(
        db_preflight, db_nullpool_engine, fault, Decimal("0.01")
    )
