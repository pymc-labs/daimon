"""Integration: `sessions create` with no flags succeeds after `defaults apply`
seeds `system_config`. Proves smoke-test finding #6 is fixed end-to-end."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsModelConfig,
    BetaManagedAgentsSession,
    BetaManagedAgentsSessionAgent,
)
from daimon.adapters.cli.commands.sessions import sessions_create
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import GithubSettings, Settings
from daimon.core.defaults import apply_defaults
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import ScopeContext
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    EMPTY_SESSION_STATS,
    EMPTY_SESSION_USAGE,
    MARouter,
    list_response,
)
from rich.console import Console
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _session_body(
    *,
    session_id: str,
    agent_id: str,
    environment_id: str,
    agent_name: str = "daimon",
    model_id: str = "claude-opus-4-7",
) -> dict[str, object]:
    """Build a BetaManagedAgentsSession JSON body via SDK Pydantic models."""
    return BetaManagedAgentsSession(
        outcome_evaluations=[],
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


def _build_settings(defaults_root: Path) -> Settings:
    class _Cli:
        local_user = "testuser"

    class _Mcp:
        jwt_secret = None
        public_url = None

    class _Settings:
        cli = _Cli()
        mcp = _Mcp()
        github = GithubSettings()

    settings = _Settings()
    settings.defaults_root = defaults_root  # type: ignore[attr-defined]
    return cast(Settings, settings)


def _make_rt(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    defaults_root: Path,
) -> CliRuntime:
    rt = cast(CliRuntime, object.__new__(CliRuntime))  # pyright: ignore[reportUnnecessaryCast]
    object.__setattr__(rt, "settings", _build_settings(defaults_root))
    object.__setattr__(rt, "anthropic", anthropic)
    object.__setattr__(rt, "sessionmaker", sessionmaker)
    object.__setattr__(rt, "deployment_default", parse_deployment_default(defaults_root))
    object.__setattr__(rt, "resolver_cache", new_resolver_cache())
    return rt


def _write_tree(root: Path) -> None:
    (root / "agents").mkdir(parents=True)
    (root / "environments").mkdir(parents=True)
    (root / "agents" / "daimon.yaml").write_text("name: daimon\nmodel: claude-sonnet-4-6\n")
    (root / "environments" / "default.yaml").write_text("name: default\n")
    (root / "config.yaml").write_text("agent_name: daimon\nenvironment_name: default\n")


def _apply_router() -> MARouter:
    """MA router used while `apply_defaults` runs against a fresh fake MA.

    GET list routes return empty (everything creates fresh). POST create
    routes return SDK-shaped rows via the shared `build_*_row` helpers.
    """
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([]))
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200,
            json=BetaEnvironment(
                id="env_1",
                type="environment",
                name="default",
                config=EMPTY_CLOUD_CONFIG,
                metadata={},
                description="",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_1",
                type="agent",
                name="daimon",
                model={"id": "claude-opus-4-7"},  # pyright: ignore[reportArgumentType]
                metadata={},
                description=None,
                created_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
                updated_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        ),
    )
    return router


def _session_router(tenant_id: uuid.UUID) -> MARouter:
    """MA router used during `sessions create`.

    Provides GET /v1/agents and GET /v1/environments so that
    `find_agent_by_daimon_tag` / `find_environment_by_daimon_tag` succeed,
    plus POST /v1/sessions to return a fake session.
    """
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT

    agent_item = BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name="daimon",
        model={"id": "claude-opus-4-7"},  # pyright: ignore[reportArgumentType]
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "daimon",
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
        id="env_1",
        type="environment",
        name="default",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "default",
        },
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([agent_item]),
    )
    router.add(
        "GET",
        r"/v1/agents/ag_1",
        lambda req, _m: httpx.Response(200, json=agent_item),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response([env_item]),
    )
    router.add(
        "GET",
        r"/v1/environments/env_1",
        lambda req, _m: httpx.Response(200, json=env_item),
    )
    router.add(
        "POST",
        r"/v1/sessions",
        lambda req, _m: httpx.Response(
            200,
            json=_session_body(
                session_id="sess_integ_1",
                agent_id="ag_1",
                environment_id="env_1",
            ),
        ),
    )
    return router


async def test_sessions_create_resolves_system_default_after_apply(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Callable[
        [Callable[[httpx.Request], httpx.Response] | None], AsyncAnthropic
    ],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_tree(tmp_path)

    # Step 1: run `defaults apply` against a fake MA; seeds MA agents/environments.
    apply_client = make_stub_anthropic(_apply_router().dispatch)
    report = await apply_defaults(
        db_session_factory, apply_client, tmp_path, dry_run=False, run_preflight=False
    )
    assert [o.action for o in report.agents], report.agents
    assert [o.action for o in report.environments], report.environments

    # Fetch the tenant that apply_defaults bootstrapped, then verify a fresh tenant
    # (no config rows) resolves to daimon/default via the injected DeploymentDefault.
    default = parse_deployment_default(tmp_path)
    async with db_session_factory() as s:
        from daimon.core._models import Tenant

        row = await s.execute(select(Tenant).limit(1))
        tenant = row.scalar_one()
        tenant_id = tenant.id
        context = ScopeContext(account_id=uuid.uuid4(), tenant_id=tenant_id)
        cfg = await resolve_config(s, context=context, default=default)
    assert cfg.agent_name == "daimon"
    assert cfg.environment_name == "default"

    # Step 2: `sessions create` with no flags on a fresh principal.
    session_anthropic = make_stub_anthropic(_session_router(tenant_id).dispatch)
    rt = _make_rt(
        anthropic=session_anthropic,
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
    )
    console = Console(highlight=False)

    await sessions_create(
        rt=rt,
        console=console,
        agent_flag=None,
        environment_flag=None,
        as_json=True,
    )
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload == {
        "session_id": "sess_integ_1",
        "agent": "daimon",
        "environment": "default",
    }


async def test_sessions_create_resolves_via_resolver_after_archive(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
    make_stub_anthropic: Callable[
        [Callable[[httpx.Request], httpx.Response] | None], AsyncAnthropic
    ],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolver self-heal: when the cached agent has been archived but a live
    tag-matching agent exists in the list response, sessions create succeeds
    against the freshly-resolved live id (rather than failing on the archived id)."""
    _write_tree(tmp_path)

    apply_client = make_stub_anthropic(_apply_router().dispatch)
    await apply_defaults(
        db_session_factory, apply_client, tmp_path, dry_run=False, run_preflight=False
    )

    async with db_session_factory() as s:
        from daimon.core._models import Tenant

        row = await s.execute(select(Tenant).limit(1))
        tenant_id = row.scalar_one().id

    # The CLI bootstrap passes cached_id=None to the resolver, so the resolver
    # path is: tag lookup -> live id. We stage the live tag-matching agent /
    # env in the list response (an archived "ag_stale" is not even referenced;
    # the CLI doesn't have a cache to populate from). This proves the resolver
    # is what bridges names to live ids in the CLI path.
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT

    live_agent = BetaManagedAgentsAgent(
        id="ag_fresh",
        type="agent",
        name="daimon",
        model={"id": "claude-opus-4-7"},  # pyright: ignore[reportArgumentType]
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "daimon",
        },
        description=None,
        created_at="2026-05-19T00:00:00Z",  # pyright: ignore[reportArgumentType]
        updated_at="2026-05-19T00:00:00Z",  # pyright: ignore[reportArgumentType]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    live_env = BetaEnvironment(
        id="env_fresh",
        type="environment",
        name="default",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "default",
        },
        description="",
        created_at="2026-05-19T00:00:00Z",
        updated_at="2026-05-19T00:00:00Z",
    ).model_dump(mode="json")

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([live_agent]))
    router.add("GET", r"/v1/agents/ag_fresh", lambda req, _m: httpx.Response(200, json=live_agent))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([live_env]))
    router.add(
        "GET",
        r"/v1/environments/env_fresh",
        lambda req, _m: httpx.Response(200, json=live_env),
    )
    router.add(
        "POST",
        r"/v1/sessions",
        lambda req, _m: httpx.Response(
            200,
            json=_session_body(
                session_id="sess_heal_1",
                agent_id="ag_fresh",
                environment_id="env_fresh",
            ),
        ),
    )

    session_anthropic = make_stub_anthropic(router.dispatch)
    rt = _make_rt(
        anthropic=session_anthropic,
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
    )
    await sessions_create(
        rt=rt,
        console=Console(highlight=False),
        agent_flag=None,
        environment_flag=None,
        as_json=True,
    )
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload == {
        "session_id": "sess_heal_1",
        "agent": "daimon",
        "environment": "default",
    }, "session should be created against the freshly-resolved live ids"
