from __future__ import annotations

import json
import uuid
from io import StringIO
from pathlib import Path
from typing import Any, cast

import httpx
import jwt as pyjwt
import pytest
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsModelConfig,
    BetaManagedAgentsSession,
    BetaManagedAgentsSessionAgent,
)
from daimon.adapters.cli.commands.sessions import (
    sessions_create,
    sessions_get,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import GithubSettings, Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    EMPTY_SESSION_STATS,
    EMPTY_SESSION_USAGE,
    MARouter,
    list_response,
)
from pydantic import HttpUrl, SecretStr
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _FakeCli:
    local_user = "testuser"


class _FakeMcp:
    jwt_secret = None
    public_url = None


class _FakeSettings:
    cli = _FakeCli()
    mcp = _FakeMcp()
    github = GithubSettings()
    defaults_root = Path("defaults")


def _build_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
) -> CliRuntime:
    return CliRuntime(
        settings=cast(Settings, _FakeSettings()),
        anthropic=anthropic,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _session_body(
    *,
    session_id: str,
    agent_id: str,
    environment_id: str,
    agent_name: str = "daimon",
    model_id: str = "claude-opus-4-7",
) -> dict[str, Any]:
    """Build a BetaManagedAgentsSession JSON body via SDK Pydantic models."""
    return BetaManagedAgentsSession(
        id=session_id,
        type="session",
        status="idle",
        agent=BetaManagedAgentsSessionAgent(
            id=agent_id,
            type="agent",
            name=agent_name,
            model=BetaManagedAgentsModelConfig(id=model_id),
            tools=[],
            skills=[],
            mcp_servers=[],
            version=1,
        ),
        environment_id=environment_id,
        metadata={},
        resources=[],
        stats=EMPTY_SESSION_STATS,
        usage=EMPTY_SESSION_USAGE,
        vault_ids=[],
        created_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
        updated_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
    ).model_dump(mode="json")


async def _seed_system_defaults(db_session: AsyncSession) -> uuid.UUID:
    """Seed tenant_system_config for the cli:local tenant and return its id.

    The cli:local tenant itself is seeded by the db_session_factory fixture;
    the sessions-create flow resolves its agent/environment against that tenant
    (discover_tenant derives cli:local), so the system defaults must hang off
    the same id.
    """
    tenant_id = derive_tenant_uuid(platform="cli", workspace_id="local")
    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_id),
        tenant_id=tenant_id,
        agent_name="agent-sys",
        environment_name="env-sys",
    )
    await db_session.commit()
    return tenant_id


def _create_router(
    tenant_id: uuid.UUID,
    session_id: str = "sess_test_01",
) -> MARouter:
    """Build a MARouter for the sessions-create flow.

    Provides GET /v1/agents and GET /v1/environments (needed by
    resolve_agent_and_environment) and POST /v1/sessions.
    """
    agent_item = BetaManagedAgentsAgent(
        id="agent_sys",
        type="agent",
        name="agent-sys",
        model={"id": "claude-sonnet-4-6"},  # pyright: ignore[reportArgumentType]
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "agent-sys",
        },
        description=None,
        created_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
        updated_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    env_item = BetaEnvironment(
        id="env_sys",
        type="environment",
        name="env-sys",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "env-sys",
        },
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_item]))
    router.add("GET", r"/v1/agents/agent_sys", lambda req, _m: httpx.Response(200, json=agent_item))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([env_item]))
    router.add(
        "GET", r"/v1/environments/env_sys", lambda req, _m: httpx.Response(200, json=env_item)
    )

    def _sessions_handler(req: httpx.Request, _m: object) -> httpx.Response:
        assert req.method == "POST"
        body = json.loads(req.content)
        return httpx.Response(
            200,
            json=_session_body(
                session_id=session_id,
                agent_id=body["agent"],
                environment_id=body["environment_id"],
            ),
        )

    router.add("POST", r"/v1/sessions", _sessions_handler)
    return router


async def test_sessions_create_json_prints_session_id_agent_env(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant_id = await _seed_system_defaults(db_session)
    rt = _build_rt(
        db_session_factory,
        make_stub_anthropic(_create_router(tenant_id, "sess_test_01").dispatch),
    )
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    await sessions_create(
        rt=rt,
        console=console,
        agent_flag="agent-sys",
        environment_flag="env-sys",
        as_json=True,
    )

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload == {
        "session_id": "sess_test_01",
        "agent": "agent-sys",
        "environment": "env-sys",
    }


async def test_sessions_create_bare_prints_session_id(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant_id = await _seed_system_defaults(db_session)
    rt = _build_rt(
        db_session_factory,
        make_stub_anthropic(_create_router(tenant_id, "sess_bare_42").dispatch),
    )
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    await sessions_create(
        rt=rt,
        console=console,
        agent_flag="agent-sys",
        environment_flag="env-sys",
        as_json=False,
    )

    out = capsys.readouterr().out.strip()
    assert out == "sess_bare_42"


async def test_sessions_create_without_defaults_exits_one_with_apply_hint(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    from daimon.adapters.cli.sessions_bootstrap import SessionBootstrapError

    rt = _build_rt(db_session_factory, stub_anthropic)
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    with pytest.raises(SessionBootstrapError) as exc_info:
        await sessions_create(
            rt=rt,
            console=console,
            agent_flag=None,
            environment_flag=None,
            as_json=False,
        )
    assert exc_info.value.kind == "defaults_missing"
    assert "daimon defaults apply" in exc_info.value.message


def _retrieve_handler(session_id: str) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert session_id in request.url.path
        return httpx.Response(
            200,
            json=_session_body(
                session_id=session_id,
                agent_id="agent_sys",
                environment_id="env_sys",
                agent_name="agent-sys",
                model_id="m",
            ),
        )

    return handler


async def test_sessions_get_json_prints_session_body(
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Any,
) -> None:
    rt = _build_rt(db_session_factory, make_stub_anthropic(_retrieve_handler("sess_xyz")))
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)

    await sessions_get(rt=rt, console=console, session_id="sess_xyz", as_json=True)

    out = buf.getvalue().strip()
    payload = json.loads(out)
    assert isinstance(payload, list) and len(payload) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert payload[0]["id"] == "sess_xyz"
    assert payload[0]["environment_id"] == "env_sys"


async def test_sessions_get_human_renders_table(
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Any,
) -> None:
    rt = _build_rt(db_session_factory, make_stub_anthropic(_retrieve_handler("sess_human")))
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)

    await sessions_get(rt=rt, console=console, session_id="sess_human", as_json=False)

    out = buf.getvalue()
    assert "sess_human" in out
    assert "env_sys" in out
    assert "idle" in out


async def test_sessions_get_missing_raises_api_error(
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Any,
) -> None:
    def not_found(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": {"type": "not_found_error", "message": "not found"}}
        )

    rt = _build_rt(db_session_factory, make_stub_anthropic(not_found))
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    with pytest.raises(APIError):
        await sessions_get(rt=rt, console=console, session_id="sess_missing", as_json=True)


class _ConfiguredMcp:
    jwt_secret = SecretStr("x" * 32)
    public_url = HttpUrl("https://mcp.example.com/mcp")


class _ConfiguredSettings:
    cli = _FakeCli()
    mcp = _ConfiguredMcp()
    github = GithubSettings()
    defaults_root = Path("defaults")


def _create_router_with_vault(
    tenant_id: uuid.UUID,
    captured_credential_bodies: list[dict[str, Any]],
    session_id: str = "sess_cli_vault_01",
) -> MARouter:
    """Build a MARouter for the sessions-create flow with full vault cold path.

    Adds vault routes on top of `_create_router` so `ensure_mcp_vault` runs
    end-to-end: GET /v1/vaults (empty), POST /v1/vaults, POST credentials.
    """
    router = _create_router(tenant_id, session_id)

    router.add(
        "GET",
        r"/v1/vaults",
        lambda req, _m: list_response([]),
    )

    def _vaults_create(req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "vlt_cli_new",
                "type": "vault",
                "display_name": json.loads(req.content)["display_name"],
                "metadata": None,
                "archived_at": None,
                "created_at": "2026-04-24T00:00:00Z",
            },
        )

    router.add("POST", r"/v1/vaults", _vaults_create)

    def _credentials_create(req: httpx.Request, _m: object) -> httpx.Response:
        captured_credential_bodies.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "id": "vcrd_cli_new",
                "type": "vault_credential",
                "vault_id": "vlt_cli_new",
                "metadata": {},
                "created_at": "2026-04-24T00:00:00Z",
                "updated_at": "2026-04-24T00:00:00Z",
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": "https://mcp.example.com/mcp",
                },
            },
        )

    router.add("POST", r"/v1/vaults/vlt_cli_new/credentials", _credentials_create)
    return router


async def test_cli_sessions_create_lands_vault_credential_with_cli_local_scope(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant_id = await _seed_system_defaults(db_session)
    captured: list[dict[str, Any]] = []
    router = _create_router_with_vault(tenant_id, captured, "sess_cli_vault_01")

    rt = CliRuntime(
        settings=cast(Settings, _ConfiguredSettings()),
        anthropic=make_stub_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    await sessions_create(
        rt=rt,
        console=console,
        agent_flag="agent-sys",
        environment_flag="env-sys",
        as_json=False,
    )

    assert len(captured) == 1, "exactly one credential POST"
    token = captured[0]["auth"]["token"]
    secret = ("x" * 32).encode()
    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert "platform" not in decoded, "CLI mint no longer emits platform as a wire claim"
    assert "guild_id" not in decoded, "CLI mint no longer emits guild_id as a wire claim"


async def test_cli_sessions_create_back_compat_when_mcp_settings_unset(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When mcp_settings has no public_url/jwt_secret, no vault calls happen.

    Pinning this from the CLI side: passing session_context must NOT cause
    ensure_mcp_vault to be called when mcp_settings is unset. The router has
    no vault routes registered, so any vault request would raise.
    """
    tenant_id = await _seed_system_defaults(db_session)
    router = _create_router(tenant_id, "sess_cli_nomcp_01")

    rt = _build_rt(
        db_session_factory,
        make_stub_anthropic(router.dispatch),
    )
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    await sessions_create(
        rt=rt,
        console=console,
        agent_flag="agent-sys",
        environment_flag="env-sys",
        as_json=False,
    )

    out = capsys.readouterr().out.strip()
    assert out == "sess_cli_nomcp_01", "bare-id printed; no vault path taken"


async def test_sessions_create_threads_agent_uuid(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """sessions_create derives agent_uuid from (tenant_id, agent_row.id) and
    threads it into create_session so ensure_agent_mcp_vault uses the per-agent
    vault key daimon-mcp:{account_id}:{agent_uuid}.

    The vault router returns an empty list on GET /v1/vaults, then expects
    POST /v1/vaults with a display_name that encodes the per-agent UUID derived
    from the same (tenant_id, 'agent_sys') pair. If sessions_create omitted
    agent_uuid, create_session would raise ValueError (mcp_settings active +
    agent_uuid=None).
    """
    tenant_id = await _seed_system_defaults(db_session)
    # Pre-compute the expected agent_uuid — same derivation as sessions_create.
    expected_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agent_sys")

    vault_display_names_created: list[str] = []

    def vault_create_handler(req: httpx.Request, _m: object) -> httpx.Response:
        body = json.loads(req.content)
        vault_display_names_created.append(body.get("display_name", ""))
        return httpx.Response(
            200,
            json={
                "id": "vlt_agent_uuid_test",
                "type": "vault",
                "display_name": body.get("display_name", ""),
                "metadata": None,
                "archived_at": None,
                "created_at": "2026-04-24T00:00:00Z",
            },
        )

    def cred_create_handler(req: httpx.Request, _m: object) -> httpx.Response:
        body = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "id": "vcrd_agent_uuid_test",
                "type": "vault_credential",
                "vault_id": "vlt_agent_uuid_test",
                "metadata": {},
                "created_at": "2026-04-24T00:00:00Z",
                "updated_at": "2026-04-24T00:00:00Z",
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": body["auth"]["mcp_server_url"],
                },
            },
        )

    router = _create_router(tenant_id, "sess_agent_uuid_01")
    router.add("GET", r"/v1/vaults", lambda req, _m: list_response([]))
    router.add("POST", r"/v1/vaults", vault_create_handler)
    router.add("POST", r"/v1/vaults/vlt_agent_uuid_test/credentials", cred_create_handler)

    rt = CliRuntime(
        settings=cast(Settings, _ConfiguredSettings()),
        anthropic=make_stub_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)

    await sessions_create(
        rt=rt,
        console=console,
        agent_flag="agent-sys",
        environment_flag="env-sys",
        as_json=False,
    )

    assert len(vault_display_names_created) == 1, (
        "exactly one vault must be created (cold path — empty vault list)"
    )
    created_name = vault_display_names_created[0]
    assert str(expected_agent_uuid) in created_name, (
        f"vault display_name must encode the per-agent UUID derived from "
        f"(tenant_id, 'agent_sys'); got {created_name!r}"
    )
    assert "agent_sys" not in created_name, (
        "vault display_name must use the derived UUID, not the raw MA agent id"
    )
