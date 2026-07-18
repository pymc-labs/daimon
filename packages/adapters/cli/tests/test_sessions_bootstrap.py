from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.adapters.cli.sessions_bootstrap import (
    SessionBootstrapError,
    check_preconditions,
    resolve_agent_and_environment,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, TenantScopeRef, UserScopeRef
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from daimon.testing.ma import EMPTY_CLOUD_CONFIG, MARouter, build_stub_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _agent_body(agent_id: str, agent_name: str, tenant_id: uuid.UUID) -> dict[str, object]:
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=agent_name,
        model={"id": "claude-sonnet-4-6"},  # pyright: ignore[reportArgumentType]
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: agent_name,
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


def _env_body(env_id: str, env_name: str, tenant_id: uuid.UUID) -> dict[str, object]:
    return BetaEnvironment(
        id=env_id,
        type="environment",
        name=env_name,
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: env_name,
        },
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")


def _make_client(
    *,
    tenant_id: uuid.UUID,
    agent_name: str | None = None,
    agent_id: str | None = None,
    env_name: str | None = None,
    env_id: str | None = None,
) -> object:
    """Build a stub AsyncAnthropic with MA list + retrieve routes."""
    router = MARouter()

    if agent_name is not None and agent_id is not None:
        body = _agent_body(agent_id, agent_name, tenant_id)
        router.add(
            "GET",
            rf"/v1/agents/{agent_id}",
            lambda req, _m, _b=body: httpx.Response(200, json=_b),
        )
        router.add("GET", r"/v1/agents", lambda req, _m, _b=body: list_response([_b]))
    else:
        router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))

    if env_name is not None and env_id is not None:
        body = _env_body(env_id, env_name, tenant_id)
        router.add(
            "GET",
            rf"/v1/environments/{env_id}",
            lambda req, _m, _b=body: httpx.Response(200, json=_b),
        )
        router.add("GET", r"/v1/environments", lambda req, _m, _b=body: list_response([_b]))
    else:
        router.add("GET", r"/v1/environments", lambda req, _m: list_response([]))

    return build_stub_anthropic(router.dispatch)


@pytest.mark.asyncio
async def test_check_preconditions_passes_when_system_config_has_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant.id),
        tenant_id=tenant.id,
        agent_name="daimon",
        environment_name=None,
    )
    await db_session.flush()
    await check_preconditions(db_session_factory, tenant_id=tenant.id, default=DeploymentDefault())


@pytest.mark.asyncio
async def test_check_preconditions_passes_when_deployment_default_has_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One-click path: a fresh tenant with zero config rows is ready when the
    injected deployment default supplies an agent name."""
    tenant = await make_tenant(db_session)
    await db_session.flush()
    await check_preconditions(
        db_session_factory,
        tenant_id=tenant.id,
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )


@pytest.mark.asyncio
async def test_check_preconditions_raises_when_no_config_and_no_deployment_default(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.flush()
    with pytest.raises(SessionBootstrapError) as exc_info:
        await check_preconditions(
            db_session_factory, tenant_id=tenant.id, default=DeploymentDefault()
        )
    assert exc_info.value.kind == "defaults_missing"


@pytest.mark.asyncio
async def test_resolve_uses_flag_over_config(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.flush()
    client = _make_client(
        tenant_id=tenant.id,
        agent_name="flag-agent",
        agent_id="ag_f",
        env_name="flag-env",
        env_id="env_f",
    )
    account_id = uuid.uuid4()
    agent, env = await resolve_agent_and_environment(
        db_session_factory,
        client,  # type: ignore[arg-type]
        tenant_id=tenant.id,
        account_id=account_id,
        agent_flag="flag-agent",
        environment_flag="flag-env",
        defaults_root=tmp_path,
        default=DeploymentDefault(),
        cache=new_resolver_cache(),
    )
    assert agent.name == "flag-agent", "flag should override config"
    assert env.name == "flag-env", "flag should override config"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_tenant_system(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    scope = TenantScopeRef(tenant_id=tenant.id)
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="sys-agent",
        environment_name="sys-env",
    )
    await db_session.flush()
    client = _make_client(
        tenant_id=tenant.id,
        agent_name="sys-agent",
        agent_id="ag_s",
        env_name="sys-env",
        env_id="env_s",
    )
    account_id = uuid.uuid4()
    agent, env = await resolve_agent_and_environment(
        db_session_factory,
        client,  # type: ignore[arg-type]
        tenant_id=tenant.id,
        account_id=account_id,
        agent_flag=None,
        environment_flag=None,
        defaults_root=tmp_path,
        default=DeploymentDefault(),
        cache=new_resolver_cache(),
    )
    assert agent.name == "sys-agent", "should fall back to tenant_system config"
    assert env.name == "sys-env", "should fall back to tenant_system config"


@pytest.mark.asyncio
async def test_resolve_ignores_user_scope_and_falls_to_tenant_system(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The per-user-active scope tier is retired.

    Even when a UserScopeRef config row exists, the resolver no longer consults
    it — the cascade is channel→workspace→tenant_system. With only a user-scoped
    and a tenant_system config set, resolution falls to tenant_system.
    """
    from daimon.core._models import Account

    tenant = await make_tenant(db_session)
    account = Account(tenant_id=tenant.id)
    db_session.add(account)
    await db_session.flush()

    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant.id),
        tenant_id=tenant.id,
        agent_name="sys-agent",
        environment_name="sys-env",
    )
    # A user-scoped config is written but MUST be ignored.
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=account.id),
        tenant_id=tenant.id,
        agent_name="user-agent",
        environment_name="user-env",
    )

    # MA returns both agents/envs; resolver picks the tenant_system names (user tier gone).
    router = MARouter()

    def _agents_handler(req: httpx.Request, _m: object) -> httpx.Response:
        agents = [
            BetaManagedAgentsAgent(
                id=aid,
                type="agent",
                name=name,
                model={"id": "claude-sonnet-4-6"},  # pyright: ignore[reportArgumentType]
                metadata={
                    MA_METADATA_KEY_TENANT: str(tenant.id),
                    MA_METADATA_KEY_NAME: name,
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
            for name, aid in [("user-agent", "ag_u"), ("sys-agent", "ag_s")]
        ]
        return list_response(agents)

    def _envs_handler(req: httpx.Request, _m: object) -> httpx.Response:
        envs = [
            BetaEnvironment(
                id=eid,
                type="environment",
                name=name,
                config=EMPTY_CLOUD_CONFIG,
                metadata={
                    MA_METADATA_KEY_TENANT: str(tenant.id),
                    MA_METADATA_KEY_NAME: name,
                },
                description="",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json")
            for name, eid in [("user-env", "env_u"), ("sys-env", "env_s")]
        ]
        return list_response(envs)

    router.add("GET", r"/v1/agents", _agents_handler)
    router.add("GET", r"/v1/environments", _envs_handler)

    # Retrieve routes for the (a)-pattern re-fetch path.
    def _agent_retrieve(req: httpx.Request, _m: object) -> httpx.Response:
        # URL path: /v1/agents/{id}
        aid = req.url.path.rsplit("/", 1)[-1]
        names = {"ag_u": "user-agent", "ag_s": "sys-agent"}
        return httpx.Response(200, json=_agent_body(aid, names[aid], tenant.id))

    def _env_retrieve(req: httpx.Request, _m: object) -> httpx.Response:
        eid = req.url.path.rsplit("/", 1)[-1]
        names = {"env_u": "user-env", "env_s": "sys-env"}
        return httpx.Response(200, json=_env_body(eid, names[eid], tenant.id))

    router.add("GET", r"/v1/agents/ag_[us]", _agent_retrieve)
    router.add("GET", r"/v1/environments/env_[us]", _env_retrieve)
    client = build_stub_anthropic(router.dispatch)

    await db_session.flush()
    agent, env = await resolve_agent_and_environment(
        db_session_factory,
        client,  # type: ignore[arg-type]
        tenant_id=tenant.id,
        account_id=account.id,
        agent_flag=None,
        environment_flag=None,
        defaults_root=tmp_path,
        default=DeploymentDefault(),
        cache=new_resolver_cache(),
    )
    assert agent.name == "sys-agent", "user scope is retired; tenant_system applies"
    assert env.name == "sys-env", "user scope is retired; tenant_system applies"


@pytest.mark.asyncio
async def test_resolve_raises_when_no_agent_configured(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.flush()
    client = _make_client(tenant_id=tenant.id)
    with pytest.raises(SessionBootstrapError) as exc_info:
        await resolve_agent_and_environment(
            db_session_factory,
            client,  # type: ignore[arg-type]
            tenant_id=tenant.id,
            account_id=uuid.uuid4(),
            agent_flag=None,
            environment_flag=None,
            defaults_root=tmp_path,
            default=DeploymentDefault(),
            cache=new_resolver_cache(),
        )
    assert exc_info.value.kind == "no_default_agent"


@pytest.mark.asyncio
async def test_resolve_raises_when_agent_not_found(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.flush()
    # MA has no agent named "nonexistent"; env present for env lookup.
    client = _make_client(
        tenant_id=tenant.id,
        env_name="e",
        env_id="env_e",
    )
    with pytest.raises(SessionBootstrapError) as exc_info:
        await resolve_agent_and_environment(
            db_session_factory,
            client,  # type: ignore[arg-type]
            tenant_id=tenant.id,
            account_id=uuid.uuid4(),
            agent_flag="nonexistent",
            environment_flag="e",
            defaults_root=tmp_path,
            default=DeploymentDefault(),
            cache=new_resolver_cache(),
        )
    assert exc_info.value.kind == "agent_not_found"
