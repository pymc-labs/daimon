from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
from cryptography.fernet import Fernet
from daimon.core._models import GitHubAppSessionVault
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.config import GithubAppSettings, McpSettings
from daimon.core.credential_requests import mint_request_token
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_CHANNEL,
)
from daimon.core.errors import UserFacingError
from daimon.core.github_app_auth import BotIdentity
from daimon.core.github_app_session import AppSessionAccess, AppToken
from daimon.core.github_credentials import build_multifernet
from daimon.core.memory_resource import (
    CHANNEL_MEMORY_INSTRUCTIONS,
    MEMORY_INSTRUCTIONS,
    PERSONAL_MEMORY_INSTRUCTIONS,
    READ_ONLY_CHANNEL_MEMORY_INSTRUCTIONS,
    READ_ONLY_MEMORY_INSTRUCTIONS,
    READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS,
    SHARED_ONLY_MEMORY_INSTRUCTIONS,
)
from daimon.core.sessions import create_isolated_session, create_session
from daimon.core.skills.unresolved import SkillUnavailable
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.posture import ExemptReason
from daimon.core.turn_hold import TurnHold
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    NotHandled,
    combine_handlers,
    json_body,
    make_fake_memory_store_handler,
)
from mux.testing.drivers.anthropic.ma import build_fake_anthropic as build_fake_anthropic_http
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
def empty_app_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let resource tests focus on their mounts while App vaults use their own tests."""
    monkeypatch.setattr(
        "daimon.core.sessions.create_session_vault",
        AsyncMock(return_value="vlt_app_test"),
    )
    monkeypatch.setattr("daimon.core.sessions.add_app_credentials", AsyncMock())


async def test_create_session_calls_ma_api_and_returns_sdk_type() -> None:
    agent = _make_agent(anthropic_id="ag_create")
    env = _make_env(anthropic_id="env_create")
    client = build_fake_anthropic_http(_session_create_handler("sess_1"))

    result = await create_session(
        client,
        agent=agent,
        environment=env,
        tool_safety=OPEN_TOOL_SAFETY,
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
            tool_safety=OPEN_TOOL_SAFETY,
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
        await create_session(
            client, agent=agent, environment=_make_env(), tool_safety=OPEN_TOOL_SAFETY
        )

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
            tool_safety=OPEN_TOOL_SAFETY,
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
            tool_safety=OPEN_TOOL_SAFETY,
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
            build_fake_anthropic_http(_bad),
            agent=_make_agent(),
            environment=_make_env(),
            tool_safety=OPEN_TOOL_SAFETY,
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
        tool_safety=OPEN_TOOL_SAFETY,
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

    registered_metadata = await _registered_vault_metadata(
        db_session_factory, account_id, agent_uuid
    )

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
                            "metadata": registered_metadata,
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
        tool_safety=OPEN_TOOL_SAFETY,
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


@pytest.mark.usefixtures("empty_app_vault")
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
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert len(bodies) == 1, "exactly one session-create call"
    resources = _without_memory_resource(bodies[0].get("resources"))
    assert resources == [{"type": "file", "file_id": "file_env123", "mount_path": ".env"}], (
        "session-create body must carry the .env file resource when the agent has secrets"
    )


@pytest.mark.usefixtures("empty_app_vault")
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
        tool_safety=OPEN_TOOL_SAFETY,
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

    await create_session(client, agent=agent, environment=env, tool_safety=OPEN_TOOL_SAFETY)

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
        tool_safety=OPEN_TOOL_SAFETY,
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
        tool_safety=OPEN_TOOL_SAFETY,
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
        tool_safety=OPEN_TOOL_SAFETY,
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
        tool_safety=OPEN_TOOL_SAFETY,
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
        tool_safety=OPEN_TOOL_SAFETY,
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

    registered_metadata = await _registered_vault_metadata(
        db_session_factory, account_id, agent_uuid, tenant_id=tenant.id
    )

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
                            "metadata": registered_metadata,
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
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert len(bodies) == 1, "exactly one session-create call"
    assert bodies[0].get("vault_ids") == ["vlt_existing"], (
        "the caller vault must be mounted alongside resources when no App grant exists"
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
            tool_safety=OPEN_TOOL_SAFETY,
        )


# --- Dev-agent port: github_repository clone resource injection (Task 1) -----


@pytest.mark.usefixtures("empty_app_vault")
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
        tool_safety=OPEN_TOOL_SAFETY,
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
        tool_safety=OPEN_TOOL_SAFETY,
    )
    async with db_session_factory() as session:
        registered = await session.get(GitHubAppSessionVault, "sess_zero_grants")
    assert registered is None
    assert bodies[0].get("vault_ids") is None
    assert not any(r["type"] == "github_repository" for r in bodies[0].get("resources", []))
    vault.assert_not_awaited()
    credentials.assert_not_awaited()


@pytest.mark.parametrize("identity_resolves", [True, False])
async def test_create_session_app_mode_mounts_the_bot_commit_identity_when_a_token_exists(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    identity_resolves: bool,
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await db_session.commit()
    identity = BotIdentity(name="daimon[bot]", email="7+daimon[bot]@users.noreply.github.com")
    token = AppToken(uuid.uuid4(), "ghs_x", 1, (1,), "write", "GH_TOKEN_ACME_WRITE")
    monkeypatch.setattr("daimon.core.sessions.create_session_vault", AsyncMock(return_value="v"))
    grant_view = AsyncMock(
        return_value=(((None, SimpleNamespace(installation_id=1), "write"),), None, None)
    )
    monkeypatch.setattr("daimon.core.sessions.effective_app_grant_view", grant_view)
    monkeypatch.setattr(
        "daimon.core.sessions.prepare_app_access",
        AsyncMock(
            return_value=AppSessionAccess(
                (), (token,), "ghs_x", ("https://github.com/example/unavailable",)
            )
        ),
    )
    monkeypatch.setattr("daimon.core.sessions.add_app_credentials", AsyncMock())
    monkeypatch.setattr("daimon.core.sessions.finish_app_delivery", AsyncMock())
    upload_helper = AsyncMock()
    monkeypatch.setattr("daimon.core.sessions.upload_github_repo_helpers_and_mount", upload_helper)
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

    observed_access: list[AppSessionAccess] = []
    await create_session(
        build_fake_anthropic_http(handler),
        agent=_make_agent(anthropic_id="ag_identity"),
        environment=_make_env(anthropic_id="env_identity"),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        agent_github_app=GithubAppSettings(app_slug="daimon"),
        on_app_access=observed_access.append,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    resolve.assert_awaited_once()
    grant_view.assert_awaited_once()
    upload_helper.assert_not_awaited()
    assert observed_access[0].excluded_repo_urls == ("https://github.com/example/unavailable",)
    assert "github_unavailable_repo_urls" not in bodies[0]["metadata"]
    assert bodies[0]["agent"] == "ag_identity"
    assert resolve.await_args is not None and resolve.await_args.kwargs["slug"] == "daimon"
    mounted = [r for r in bodies[0].get("resources", []) if r["type"] == "file"]
    if identity_resolves:
        assert mounted == [
            {"type": "file", "file_id": "file_id", "mount_path": "git-identity.sh"}
        ], "a session holding an App token carries the bot's commit identity"
    else:
        assert mounted == [], "an unresolved identity leaves the session without one"
        mount.assert_not_awaited()


@pytest.mark.parametrize("delivered_installations", [0, 1, 2, -1])
async def test_multi_installation_helper_requires_a_delivered_named_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    delivered_installations: int,
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid = uuid.uuid4()
    await db_session.commit()
    grant_view = AsyncMock(
        return_value=(
            (
                (None, SimpleNamespace(installation_id=101), "write"),
                (None, SimpleNamespace(installation_id=202), "write"),
            ),
            None,
            None,
        )
    )
    monkeypatch.setattr("daimon.core.sessions.effective_app_grant_view", grant_view)
    monkeypatch.setattr(
        "daimon.core.sessions.create_session_vault", AsyncMock(return_value="vlt_multi")
    )
    all_tokens = (
        AppToken(uuid.uuid4(), "token-a", 101, (11,), "write", "GH_TOKEN_I101_0", 1, 0),
        AppToken(uuid.uuid4(), "token-b", 202, (22,), "write", "GH_TOKEN_I202_0", 1, 0),
    )
    all_entries = (
        ("a/one", "GH_TOKEN_I101_0"),
        ("b/two", "GH_TOKEN_I202_0"),
    )
    tokens = (
        all_tokens[1:] if delivered_installations == -1 else all_tokens[:delivered_installations]
    )
    entries = (
        all_entries[1:] if delivered_installations == -1 else all_entries[:delivered_installations]
    )
    access = AppSessionAccess(
        (),
        tokens,
        "token-a" if tokens and delivered_installations != -1 else None,
        multi_installation=True,
        repo_credential_map=entries,
    )
    monkeypatch.setattr("daimon.core.sessions.prepare_app_access", AsyncMock(return_value=access))
    monkeypatch.setattr("daimon.core.sessions.add_app_credentials", AsyncMock())
    monkeypatch.setattr("daimon.core.sessions.finish_app_delivery", AsyncMock())
    monkeypatch.setattr("daimon.core.sessions.revoke_app_access", AsyncMock())
    monkeypatch.setattr("daimon.core.sessions.archive_app_vault", AsyncMock())
    monkeypatch.setattr("daimon.core.sessions.resolve_bot_identity", AsyncMock(return_value=None))
    upload_helper = AsyncMock(
        return_value=(
            {"type": "file", "file_id": "file_map", "mount_path": "github-repos.tsv"},
            {"type": "file", "file_id": "file_script", "mount_path": "github-repo.sh"},
        )
    )
    monkeypatch.setattr("daimon.core.sessions.upload_github_repo_helpers_and_mount", upload_helper)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/sessions"
        body = json_body(request)
        bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_multi_helper",
                agent_id="ag_multi_helper",
                environment_id=body["environment_id"],
            ),
        )

    await create_session(
        build_fake_anthropic_http(handler),
        agent=_make_agent(anthropic_id="ag_multi_helper"),
        environment=_make_env(anthropic_id="env_multi_helper"),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        tool_safety=OPEN_TOOL_SAFETY,
    )
    grant_view.assert_awaited_once()
    if tokens:
        assert upload_helper.await_args is not None
        assert upload_helper.await_args.kwargs["entries"] == entries
        assert {resource["mount_path"] for resource in bodies[0]["resources"]} >= {
            "github-repos.tsv",
            "github-repo.sh",
        }
        assert bodies[0]["metadata"]["daimon_github_repo_map_sha256"]
        if delivered_installations == -1:
            assert access.working_token is None
            assert entries == (("b/two", "GH_TOKEN_I202_0"),)
    else:
        upload_helper.assert_not_awaited()
        assert not any(
            resource.get("mount_path") in ("github-repos.tsv", "github-repo.sh")
            for resource in bodies[0].get("resources", [])
        )
        assert "daimon_github_repo_map_sha256" not in bodies[0]["metadata"]
        assert "daimon_github_repo_script_sha256" not in bodies[0]["metadata"]
    assert bodies[0]["agent"] == "ag_multi_helper"


async def test_app_session_attaches_caller_and_session_vaults(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    agent_uuid, account_id = uuid.uuid4(), uuid.uuid4()
    await db_session.commit()
    monkeypatch.setattr(
        "daimon.core.sessions.create_session_vault", AsyncMock(return_value="vlt_session")
    )
    grant_view = AsyncMock(
        return_value=(((None, SimpleNamespace(installation_id=1), "write"),), None, None)
    )
    monkeypatch.setattr("daimon.core.sessions.effective_app_grant_view", grant_view)
    monkeypatch.setattr(
        "daimon.core.sessions.prepare_app_access",
        AsyncMock(return_value=AppSessionAccess((), (), None)),
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
        tool_safety=OPEN_TOOL_SAFETY,
    )
    assert bodies[0]["vault_ids"] == ["vlt_caller", "vlt_session"]
    grant_view.assert_awaited_once()
    assert mirror.await_args.kwargs["vault_id"] == "vlt_caller"
    assert add_app.await_args.kwargs["vault_id"] == "vlt_session"


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


# --- Operator fallback PAT for public-repo clones (quick task 260616-45k) -----


# --- Agent memory: per-agent memory store attach -----------------------------


@pytest.mark.usefixtures("empty_app_vault")
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
        tool_safety=OPEN_TOOL_SAFETY,
    )

    memory_resources = [r for r in captured.get("resources", []) if r.get("type") == "memory_store"]
    assert len(memory_resources) == 1, (
        "session-create body must carry exactly one memory_store resource"
    )
    assert memory_resources[0]["access"] == "read_write"
    assert memory_resources[0]["memory_store_id"] in mem_state.stores


@pytest.mark.usefixtures("empty_app_vault")
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
        tool_safety=OPEN_TOOL_SAFETY,
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


@pytest.mark.usefixtures("empty_app_vault")
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
            tool_safety=OPEN_TOOL_SAFETY,
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


@pytest.mark.usefixtures("empty_app_vault")
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
        tool_safety=OPEN_TOOL_SAFETY,
    )

    (shared,) = _memory_mounts(bodies[0])
    assert shared["instructions"] == SHARED_ONLY_MEMORY_INSTRUCTIONS
    assert len(mem_state.stores) == 1, "no personal store is provisioned"


@pytest.mark.usefixtures("empty_app_vault")
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
        tool_safety=OPEN_TOOL_SAFETY,
    )

    (shared,) = _memory_mounts(bodies[0])
    assert shared["instructions"] == SHARED_ONLY_MEMORY_INSTRUCTIONS, (
        "with no personal store the shared one must not point personal facts anywhere"
    )


_HELD_PLACE = TurnHold(platform="slack", channel_id="C-SYN-1", client=False)


@pytest.mark.usefixtures("empty_app_vault")
@pytest.mark.parametrize("read_only", [False, True])
async def test_a_held_session_mounts_its_channel_store_alone(
    read_only: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Held, a session mounts only its channel's store: no agent-wide store, no
    personal one even though the caller asked for it, and a second channel gets
    its own. The same caller unheld still gets agent-wide plus personal."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    agent_uuid = uuid.uuid4()
    mem_state = FakeMemoryStoreState()
    bodies: list[dict[str, Any]] = []
    client = _capturing_memory_client(mem_state, bodies)

    for hold in (
        _HELD_PLACE,
        TurnHold(platform="slack", channel_id="C-SYN-2", client=False),
        None,
    ):
        await create_session(
            client,
            agent=_make_agent(anthropic_id="ag_held", name="daimon-held"),
            environment=_make_env(anthropic_id="env_held"),
            account_id=account.id,
            tenant_id=tenant.id,
            agent_uuid=agent_uuid,
            session_factory=db_session_factory,
            memory_read_only=read_only,
            personal_memory=True,
            memory_hold=hold,
            tool_safety=OPEN_TOOL_SAFETY,
        )

    (first,), (second,), unheld = (_memory_mounts(body) for body in bodies)
    assert first["memory_store_id"] != second["memory_store_id"]
    assert first["instructions"] == (
        READ_ONLY_CHANNEL_MEMORY_INSTRUCTIONS if read_only else CHANNEL_MEMORY_INSTRUCTIONS
    )
    assert first["access"] == ("read_only" if read_only else "read_write")
    assert first["memory_store_id"] not in {m["memory_store_id"] for m in unheld}
    assert len(unheld) == 2, "an unheld session mounts the agent-wide and the personal store"
    assert {mem_state.stores[m["memory_store_id"]]["name"] for m in (first, second)} == {
        "channel slack C-SYN-1",
        "channel slack C-SYN-2",
    }


@pytest.mark.usefixtures("empty_app_vault")
async def test_a_held_session_without_a_caller_still_mounts_the_channel_store(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A shared thread's session has no caller account, hence no credential hold,
    but its memory is the place's."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    bodies: list[dict[str, Any]] = []

    await create_session(
        _capturing_memory_client(FakeMemoryStoreState(), bodies),
        agent=_make_agent(anthropic_id="ag_thread"),
        environment=_make_env(anthropic_id="env_thread"),
        tenant_id=tenant.id,
        agent_uuid=uuid.uuid4(),
        session_factory=db_session_factory,
        memory_hold=_HELD_PLACE,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    (mounted,) = _memory_mounts(bodies[0])
    assert mounted["instructions"] == CHANNEL_MEMORY_INSTRUCTIONS


@pytest.mark.usefixtures("empty_app_vault")
async def test_a_channel_store_outage_leaves_a_held_session_without_memory_not_with_agent_wide(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    bodies: list[dict[str, Any]] = []
    memory_handler = make_fake_memory_store_handler(FakeMemoryStoreState())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/memory_stores":
            return httpx.Response(
                500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}
            )
        try:
            return memory_handler(request)
        except NotHandled:
            pass
        body = json_body(request)
        bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_1", agent_id=body["agent"], environment_id=body["environment_id"]
            ),
        )

    await create_session(
        build_fake_anthropic_http(handler),
        agent=_make_agent(anthropic_id="ag_outage"),
        environment=_make_env(anthropic_id="env_outage"),
        account_id=account.id,
        tenant_id=tenant.id,
        agent_uuid=uuid.uuid4(),
        session_factory=db_session_factory,
        personal_memory=True,
        memory_hold=_HELD_PLACE,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    assert _memory_mounts(bodies[0]) == [], "nothing agent-wide or personal fills in for it"


# --- Agent-scoped external MCP credentials mirror into the caller's vault ----


async def _registered_vault_metadata(factory, account_id, agent_id, tenant_id=None):
    from daimon.core._models import Account
    from daimon.core.mcp_vault import chat_mcp_credential

    async with factory() as db, db.begin():
        if await db.get(Account, account_id) is None:
            if tenant_id is None:
                tenant = await make_tenant(db)
                tenant_id = tenant.id
            db.add(Account(id=account_id, tenant_id=tenant_id))
            await db.flush()
        payload = await chat_mcp_credential(
            db,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=b"x" * 32,
            public_url="https://mcp.example.com/mcp",
            now=datetime.now(UTC),
        )
    return payload["metadata"]


def _warm_vault_mirror_handler(
    *,
    chat_metadata: dict[str, str],
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
        if request.method == "POST" and request.url.path == "/v1/vaults":
            body = json.loads(request.content)
            assert body["display_name"].startswith(display + ":session:")
            return httpx.Response(
                200,
                json={
                    "id": "vlt_execution",
                    "type": "vault",
                    "display_name": body["display_name"],
                    "metadata": None,
                    "archived_at": None,
                    "created_at": "2026-04-01T00:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/vaults/vlt_execution/credentials":
            body = json.loads(request.content)
            assert body["metadata"]["daimon_chat_session"]
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_execution",
                    "type": "credential",
                    "vault_id": "vlt_execution",
                    "metadata": body["metadata"],
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": body["auth"]["mcp_server_url"],
                    },
                },
            )
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
                            "metadata": chat_metadata,
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
            chat_metadata=await _registered_vault_metadata(
                db_session_factory, other_account_id, agent_uuid, tenant_id=tenant.id
            ),
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
        tool_safety=OPEN_TOOL_SAFETY,
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
        "the mirrored credential rides the caller's vault without an App grant"
    )


@pytest.mark.usefixtures("empty_app_vault")
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
            chat_metadata=await _registered_vault_metadata(
                db_session_factory, account_id, agent_uuid, tenant_id=tenant.id
            ),
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
        tool_safety=OPEN_TOOL_SAFETY,
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


@pytest.mark.usefixtures("empty_app_vault")
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
            chat_metadata=await _registered_vault_metadata(
                db_session_factory, bystander_account_id, agent_uuid, tenant_id=tenant.id
            ),
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
        tool_safety=OPEN_TOOL_SAFETY,
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


@pytest.mark.usefixtures("empty_app_vault")
async def test_create_session_keeps_the_server_for_the_member_who_signed_in(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Hiding is per caller: the person who drove the sign-in still gets the
    server alongside the session-scoped Daimon identity."""
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
            chat_metadata=await _registered_vault_metadata(
                db_session_factory, connected_account_id, agent_uuid, tenant_id=tenant.id
            ),
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
        tool_safety=OPEN_TOOL_SAFETY,
    )

    sent_agent = session_bodies[0]["agent"]
    assert sent_agent["id"] == "ag_personal"
    assert [server["name"] for server in sent_agent["mcp_servers"]] == ["docs", "daimon-mcp"]
    assert sent_agent["mcp_servers"][0]["url"] == "https://mcp.example.com/docs"
    assert "daimon_session=" in sent_agent["mcp_servers"][1]["url"]
    assert session_bodies[0]["vault_ids"] == ["vlt_execution", "vlt_caller"]


@pytest.mark.parametrize(
    "warm,read_only", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_read_only_source_projection_keeps_implicit_memory_without_provisioning(
    db_preflight, db_committing_engine, warm, read_only, monkeypatch
):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.memory_resource import memory_instructions
    from daimon.core.mux_backend import resource_scope
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores import agent_memory_stores
    from daimon.testing.db import require_owned_sessionmaker
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
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
            tool_safety=OPEN_TOOL_SAFETY,
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


async def test_source_projection_requires_app_vault_only_for_a_live_grant(
    db_preflight, db_committing_engine
):
    from daimon.core._models import TenantGitHubRepo
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_backend import resource_scope
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores import github_access, github_app_installations
    from daimon.testing.db import require_owned_sessionmaker
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    agent = ma_agent(id="agent-source", tenant_id=str(tenant.id), name="source")
    identity = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
    scope = resource_scope(tenant_id=str(tenant.id), account_id=str(account.id))
    transport = ScriptedTransport()
    async with transport.client() as client:
        source = await load_backend_agent(
            client, sessions, tenant_id=tenant.id, agent_name="source", actual_agent=agent
        )

    async def project():
        return await project_session_resources(
            source,
            agent=agent,
            environment=ma_environment(tenant_id=str(tenant.id)),
            session_factory=sessions,
            tenant_id=tenant.id,
            agent_uuid=identity,
            account_id=account.id,
            memory_read_only=False,
            resource_scope=scope,
            tool_safety=OPEN_TOOL_SAFETY,
        )

    empty = await project()
    assert not any(r.id.startswith("app-session-vault:") for r in empty.resource_requirements)
    async with sessions.begin() as db:
        await github_app_installations.upsert(
            db, installation_id=77, account_login="example", repo_full_names=["example/widgets"]
        )
        db.add(
            TenantGitHubRepo(
                tenant_id=tenant.id,
                repo_id=101,
                owner_id=1,
                installation_id=77,
                repo_full_name="example/widgets",
                max_access="read",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
        await db.flush()
        await github_access.stage_grant(
            db,
            tenant_id=tenant.id,
            agent_id=identity,
            repo_id=101,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=None,
        )
    granted = await project()
    assert any(r.id.startswith("app-session-vault:") for r in granted.resource_requirements)
    assert [repo.repository_url for repo in granted.repository_sources] == [
        "https://github.com/example/widgets"
    ]


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
            tool_safety=OPEN_TOOL_SAFETY,
        )


async def test_projection_reads_bound_resource_intent_without_resolving_secrets(
    db_preflight, db_committing_engine, monkeypatch
):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_backend import resource_scope
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores import agent_mcp_credentials as credentials
    from daimon.testing.db import require_owned_sessionmaker
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
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

    def no_secret_resolution(*args, **kwargs):
        raise AssertionError("projection tried to resolve or export a secret")

    monkeypatch.setattr("daimon.core.stores.agent_files.decode_value", no_secret_resolution)
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
            tool_safety=OPEN_TOOL_SAFETY,
        )
    assert {
        resource.credential_ref
        for resource in projected.resource_requirements
        if resource.credential_ref is not None
    } == {
        f"agent-env:{tenant.id}:{identity}:SOURCE_KEY",
        f"agent-mcp:{credential.id}",
        f"caller-mcp:{tenant.id}:{account.id}:{identity}",
    }
    assert projected.repository_sources == ()
    assert len(projected.resource_generations) == 2
    for output in (repr(projected), projected.model_dump_json()):
        assert "private-env-value" not in output and "private-ciphertext" not in output
        assert "owned-repo-credential" not in output and "SOURCE_KEY" not in output
    assert not transport.requests and not transport.violations


async def test_shared_projection_lists_agent_tokens_but_no_caller_vault(
    db_preflight, db_nullpool_engine
):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.mux_backend import resource_scope
    from daimon.core.sessions import project_session_resources
    from daimon.core.stores import agent_mcp_credentials as credentials
    from daimon.testing.db import require_owned_sessionmaker
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    owner = uuid.uuid4()
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        agent = ma_agent(id="agent-resources", tenant_id=str(tenant.id), name="source")
        identity = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
        credential = await credentials.upsert_credential(
            db,
            tenant_id=tenant.id,
            agent_id=identity,
            mcp_server_url="https://source.example.test",
            encrypted_token=b"private-ciphertext",
        )
    transport = ScriptedTransport()
    async with transport.client() as client:
        source = await load_backend_agent(
            client, sessions, tenant_id=tenant.id, agent_name="source", actual_agent=agent
        )
        projected = await project_session_resources(
            source,
            agent=agent,
            tool_safety=OPEN_TOOL_SAFETY,
            environment=ma_environment(tenant_id=str(tenant.id)),
            session_factory=sessions,
            tenant_id=tenant.id,
            agent_uuid=identity,
            account_id=None,
            hide_personal_for=owner,
            resource_scope=resource_scope(tenant_id=str(tenant.id), account_id=str(owner)),
            memory_read_only=True,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            mcp_settings=McpSettings(
                public_url=HttpUrl("https://mcp.example.test/mcp"), jwt_secret=SecretStr("test-jwt")
            ),
        )
    assert {
        resource.credential_ref
        for resource in projected.resource_requirements
        if resource.credential_ref is not None
        and not resource.credential_ref.startswith("agent-env:")
    } == {f"agent-mcp:{credential.id}"}
    assert not transport.requests and not transport.violations


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
    from mux.contracts.ids import Scope
    from mux.testing.drivers.anthropic.ma import MARouter
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

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


@pytest.mark.parametrize("routine", [False, True])
async def test_each_daimon_session_mounts_its_own_signed_origin_credential(
    db_session_factory: async_sessionmaker[AsyncSession],
    routine: bool,
) -> None:
    import asyncio

    import jwt
    from daimon.core.lead_intake_execution import current_intake_execution
    from daimon.core.mcp_server_url import is_session_mcp_url
    from daimon.core.session_snapshot import snapshot_from_created_session
    from daimon.core.session_update_ops import apply_update_ops
    from mux.testing.isolated_sdk import build_async_anthropic
    from sqlalchemy import text

    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    public_url = "https://daimon.example/mcp"
    shared_id = "vlt_shared"
    vaults = [
        {
            "id": shared_id,
            "type": "vault",
            "display_name": f"daimon-mcp:{account.id}:{agent_id}",
            "created_at": now.isoformat(),
            "archived_at": None,
            "metadata": None,
        }
    ]
    slots: dict[str, list[dict[str, Any]]] = {shared_id: []}
    tokens: list[dict[str, Any]] = []
    sessions: dict[str, dict[str, Any]] = {}
    execution = (uuid.uuid4(), uuid.uuid4())
    ready, release = asyncio.Event(), asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/vaults":
            if request.method == "GET":
                return httpx.Response(200, json={"data": vaults, "has_more": False})
            body = json.loads(request.content)
            new = {
                **vaults[0],
                "id": f"vlt_identity_{len(vaults)}",
                "display_name": body["display_name"],
            }
            vaults.append(new)
            slots[new["id"]] = []
            return httpx.Response(200, json=new)
        if path.startswith("/v1/vaults/") and "/credentials" in path:
            vault_id = path.split("/")[3]
            if request.method == "GET":
                return httpx.Response(200, json={"data": slots[vault_id], "has_more": False})
            body = json.loads(request.content)
            token = jwt.decode(
                body["auth"]["token"], "s" * 32, algorithms=["HS256"], options={"verify_iat": False}
            )
            tokens.append(token)
            existing = slots[vault_id][0] if slots[vault_id] else None
            url = body["auth"].get("mcp_server_url") or existing["auth"]["mcp_server_url"]
            slot = {
                "id": f"cred_{vault_id}",
                "type": "vault_credential",
                "vault_id": vault_id,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "metadata": body["metadata"],
                "auth": {"type": "static_bearer", "mcp_server_url": url},
            }
            slots[vault_id] = [slot]
            return httpx.Response(200, json=slot)
        if path == "/v1/sessions" and request.method == "POST":
            body = json.loads(request.content)
            sent_agent = body["agent"]
            frozen = ma_agent(
                id=agent.id, mcp_servers=sent_agent["mcp_servers"], tools=sent_agent["tools"]
            )
            native = ma_session(
                id=f"sess_{len(sessions)}",
                agent=frozen,
                metadata=body["metadata"],
                vault_ids=body["vault_ids"],
            ).model_dump(mode="json")
            sessions[native["id"]] = native
            return httpx.Response(200, json=native)
        if path.startswith("/v1/sessions/") and request.method == "GET":
            return httpx.Response(200, json=sessions[path.split("/")[3]])
        raise NotHandled

    agent = ma_agent(
        id="agent_scoped", mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": public_url}]
    )
    mcp = McpSettings(jwt_secret=SecretStr("s" * 32), public_url=HttpUrl(public_url))
    combined = combine_handlers(handler, make_fake_memory_store_handler())

    async def interleaved(request: httpx.Request) -> httpx.Response:
        if (
            routine
            and request.method == "POST"
            and request.url.path == "/v1/sessions"
            and current_intake_execution.get() == execution
        ):
            ready.set()
            await release.wait()
        return combined(request)

    client = build_async_anthropic(transport=httpx.MockTransport(interleaved))

    async def create() -> BetaManagedAgentsSession:
        return await create_session(
            client,
            agent=agent,
            environment=ma_environment(),
            tenant_id=tenant.id,
            account_id=account.id,
            agent_uuid=agent_id,
            session_factory=db_session_factory,
            mcp_settings=mcp,
            tool_safety=OPEN_TOOL_SAFETY,
        )

    if routine:

        async def create_routine() -> BetaManagedAgentsSession:
            context = current_intake_execution.set(execution)
            try:
                return await create()
            finally:
                current_intake_execution.reset(context)

        pending = asyncio.create_task(create_routine())
        await asyncio.wait_for(ready.wait(), 20)
        # The routine has assembled its credentials and released the vault lock,
        # but its provider session has not been created. Force a human refresh
        # of the common credential slot inside exactly that concurrency window.
        shared_jti = slots[shared_id][0]["metadata"]["daimon_chat_jti"]
        async with db_session_factory.begin() as db:
            await db.execute(
                text("UPDATE mcp_tokens SET revoked_at=now() WHERE jti=:jti"),
                {"jti": uuid.UUID(shared_jti)},
            )
        try:
            human = await create()
        finally:
            release.set()
        first_routine = await pending
        created = [first_routine, human]
        assert slots[shared_id][0]["metadata"]["daimon_chat_jti"] != shared_jti
    else:
        created = [await create(), await create()]
    first, second = created
    assert first.metadata["daimon_chat_session_id"] != second.metadata["daimon_chat_session_id"]
    assert set(first.vault_ids) & set(second.vault_ids) == {shared_id}
    assert first.agent.mcp_servers[0].url != second.agent.mcp_servers[0].url
    assert all(
        is_session_mcp_url(session.agent.mcp_servers[0].url, public_url) for session in created
    )
    assert {token.get("chat_session_id") for token in tokens} == {
        None,
        first.metadata["daimon_chat_session_id"],
        second.metadata["daimon_chat_session_id"],
    }
    assert all(
        "routine_id" not in token and "execution_id" not in token
        for token in tokens
        if not token.get("chat_session_id")
    )
    routine_token = next(
        token
        for token in tokens
        if token.get("chat_session_id") == first.metadata["daimon_chat_session_id"]
    )
    if routine:
        assert (routine_token["routine_id"], routine_token["execution_id"]) == tuple(
            map(str, execution)
        )
    else:
        assert "execution_id" not in routine_token
    recorded = snapshot_from_created_session(
        first,
        env_sha256=None,
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id=shared_id,
        sent_skills=(),
    )
    await apply_update_ops(
        client,
        db_session_factory,
        ops=(),
        session_id=first.id,
        recorded=recorded,
        agent=agent,
        tenant_id=tenant.id,
        agent_uuid=agent_id,
        account_id=account.id,
        mcp=mcp,
        tool_safety=OPEN_TOOL_SAFETY,
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        now=now + timedelta(minutes=12),
    )
    assert tokens[-2].get("chat_session_id") == first.metadata["daimon_chat_session_id"]
    assert tokens[-1].get("chat_session_id") is None

    if routine:
        assert (tokens[-2]["routine_id"], tokens[-2]["execution_id"]) == tuple(map(str, execution))
    assert "routine_id" not in tokens[-1] and "execution_id" not in tokens[-1]
    await client.close()


@pytest.mark.usefixtures("empty_app_vault")
@pytest.mark.parametrize("held", [False, True])
async def test_create_session_mounts_the_vault_of_the_turns_hold(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    held: bool,
) -> None:
    """A held turn's session mounts a vault named for its hold, whose credential
    carries the signed hold; an unheld turn's session mounts the ordinary one."""
    import jwt as pyjwt
    from daimon.core.mcp_hold import HOLD_CLAIM, hold_vault_suffix
    from daimon.core.turn_hold import TurnHold

    tenant = await make_tenant(db_session, platform="slack")
    account = await make_account(db_session, tenant=tenant)
    agent_uuid = uuid.uuid4()
    hold = TurnHold(platform="slack", channel_id="C-SYN-1", client=True) if held else None
    created_vaults: list[str] = []
    credential_bodies: list[dict[str, Any]] = []
    session_bodies: list[dict[str, Any]] = []
    memory_handler = make_fake_memory_store_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return memory_handler(request)
        except NotHandled:
            pass
        if request.method == "GET" and request.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if request.method == "POST" and request.url.path == "/v1/vaults":
            name = json.loads(request.content)["display_name"]
            created_vaults.append(name)
            return httpx.Response(
                200,
                json={
                    "id": "vlt_new",
                    "type": "vault",
                    "display_name": name,
                    "metadata": None,
                    "archived_at": None,
                    "created_at": "2026-04-01T00:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/vaults/vlt_new/credentials":
            body = json.loads(request.content)
            credential_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_new",
                    "type": "credential",
                    "vault_id": "vlt_new",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": body["auth"]["mcp_server_url"],
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/v1/sessions"):
            body = json.loads(request.content)
            session_bodies.append(body)
            sent_agent = body["agent"]
            return httpx.Response(
                200,
                json=_session_body(
                    session_id="sess_held",
                    agent_id=sent_agent if isinstance(sent_agent, str) else sent_agent["id"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    await create_session(
        build_fake_anthropic_http(handler),
        agent=_make_agent(anthropic_id="ag_held"),
        environment=_make_env(anthropic_id="env_held"),
        account_id=account.id,
        mcp_settings=McpSettings(
            jwt_secret=SecretStr("x" * 32), public_url=HttpUrl("https://mcp.example.com/mcp")
        ),
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        session_factory=db_session_factory,
        hold=hold,
        tool_safety=OPEN_TOOL_SAFETY,
    )

    ordinary = f"daimon-mcp:{account.id}:{agent_uuid}"
    assert created_vaults == [ordinary + (hold_vault_suffix(hold) if hold else "")]
    claims = pyjwt.decode(
        credential_bodies[0]["auth"]["token"],
        "x" * 32,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert (HOLD_CLAIM in claims) is held
    assert session_bodies[0]["vault_ids"] == ["vlt_new"]


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


async def test_identity_relay_gets_a_vault_of_the_sessions_own_mounted_first(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The relay credential never shares the caller's vault, so it can name the session."""
    relay = "https://mcp.example.com/mcp-identity/tools"
    agent = ma_agent(
        id="ag_relay",
        name="Helper",
        model="claude-opus-4-7",
        mcp_servers=[{"name": "tools", "type": "url", "url": relay}],
    )
    created_relay = AsyncMock(return_value="vlt_relay")
    monkeypatch.setattr(
        "daimon.core.sessions.ensure_agent_mcp_vault", AsyncMock(return_value="vlt_caller")
    )
    monkeypatch.setattr("daimon.core.sessions.create_relay_vault", created_relay)
    bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json=_session_body(
                session_id="sess_relay",
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    key = uuid.uuid4()
    await create_session(
        build_fake_anthropic_http(_handler),
        agent=agent,
        environment=_make_env(),
        account_id=uuid.uuid4(),
        agent_uuid=uuid.uuid4(),
        mcp_settings=McpSettings(
            jwt_secret=SecretStr("x" * 32), public_url=HttpUrl("https://mcp.example.com/mcp")
        ),
        session_factory=db_session_factory,
        relay_session_key=key,
    )

    assert bodies[0]["vault_ids"] == ["vlt_relay", "vlt_caller"], (
        "the relay vault goes first; the snapshot records the last, the caller's"
    )
    call = created_relay.await_args
    assert call is not None and call.kwargs["relay_urls"] == (relay,), "the attached relay URL"
    assert call.kwargs["relay"].session_key == key, "the credential names the mapping row"
    assert call.kwargs["relay"].agent_name == "Helper", "and the agent"
