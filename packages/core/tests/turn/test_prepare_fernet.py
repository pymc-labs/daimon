"""SPEC Req 7(a) proof: `fernet=deps.fernet` reaches `create_session`
unconditionally through `bind_session`, and is asserted at the transport
layer (a captured `sessions.create` request body) rather than against a
stubbed `create_session` -- RESEARCH Pitfall 3 records that boundary-stubbing
`create_session` would mask exactly the historical Slack-specific bug this
plan fixes.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from cryptography.fernet import Fernet, MultiFernet
from daimon.core.config import McpSettings
from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import agent_github_binding as github_binding_store
from daimon.core.stores import agent_repo_binding as repo_binding_store
from daimon.core.stores.domain import RepoAccessProof
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import bind_session
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daimon.testing.factories import make_account, make_tenant  # isort: skip


def _admission(
    *, account_id: uuid.UUID, agent: BetaManagedAgentsAgent, env: BetaEnvironment
) -> Admission:
    return Admission(
        account_id=account_id,
        agent=agent,
        environment=env,
        config=ResolvedConfig(agent_name="daimon", environment_name="default"),
    )


def _without_memory_resource(resources: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Strip the memory_store entry attached unconditionally alongside the
    repo resource, so assertions here only cover the fernet-gated resource."""
    if resources is None:
        return []
    return [r for r in resources if r.get("type") != "memory_store"]


def _router_capturing_session_create(*, session_bodies: list[dict[str, Any]]) -> MARouter:
    router = MARouter()
    memory_handler = make_fake_memory_store_handler()

    def _memory(request: httpx.Request, _match: object) -> httpx.Response:
        return memory_handler(request)

    router.add("POST", r"/v1/memory_stores", _memory)

    def _session_create(request: httpx.Request, _match: object) -> httpx.Response:
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
                "resources": body.get("resources", []),
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
    fernet: MultiFernet | None,
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
        fernet=fernet,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )


async def _seed_repo_binding_with_pat(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    fernet: MultiFernet,
) -> None:
    await repo_binding_store.set_binding(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        repo_url="https://github.com/example-org/example-repo",
        default_branch="main",
        ma_secret_ref="anon:",
        proof=RepoAccessProof(kind="pat", at=datetime(2026, 5, 1, tzinfo=UTC), account_id=None),
    )
    await github_binding_store.set_agent_github_binding(
        db_session, agent_id=agent_uuid, principal_id=agent_uuid
    )
    await db_session.commit()
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=agent_uuid,
        github_login="dev-agent",
        plaintext_token="ghp_dev_agent_token",
        scopes=("repo",),
    )


async def test_bind_session_mounts_github_repo_resource_via_fernet_for_slack_and_discord(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_ma_id = "ag_repo"
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent_ma_id)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    await _seed_repo_binding_with_pat(
        db_session,
        db_session_factory,
        tenant_id=tenant.id,
        agent_uuid=agent_uuid,
        fernet=fernet,
    )

    agent = ma_agent(id=agent_ma_id, tenant_id=tenant.id)
    env = ma_environment(id="env_repo", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    expected_resource = {
        "type": "github_repository",
        "url": "https://github.com/example-org/example-repo",
        "authorization_token": "ghp_dev_agent_token",
        "checkout": {"type": "branch", "name": "main"},
    }

    for platform in ("slack", "discord"):
        session_bodies: list[dict[str, Any]] = []
        router = _router_capturing_session_create(session_bodies=session_bodies)
        deps = _deps(sessionmaker=db_session_factory, router=router, fernet=fernet)

        await bind_session(
            deps,
            admission,
            tenant_id=tenant.id,
            platform=platform,
            external_user_id="user-1",
            thread_id=f"thread-{platform}",
            session_account_id=account.id,
            reuse_existing=False,
        )

        assert len(session_bodies) == 1, f"exactly one session-create call for platform={platform}"
        resources = _without_memory_resource(session_bodies[0].get("resources"))
        assert resources == [expected_resource], (
            f"platform={platform} must carry the github_repository resource mounted via fernet"
        )


async def test_bind_session_omits_repo_resource_and_does_not_raise_when_fernet_none(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With no fernet configured and no repo binding for this agent, bind_session
    must succeed (no exception) and the session-create body must carry no
    github_repository resource -- proving fernet=None is a safe, non-fatal
    configuration, not merely a value that happens to be tolerated when a
    binding exists."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_ma_id = "ag_no_repo"
    agent = ma_agent(id=agent_ma_id, tenant_id=tenant.id)
    env = ma_environment(id="env_no_repo", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    await db_session.commit()

    session_bodies: list[dict[str, Any]] = []
    router = _router_capturing_session_create(session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, router=router, fernet=None)

    await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="slack",
        external_user_id="user-1",
        thread_id="thread-no-repo",
        session_account_id=account.id,
        reuse_existing=False,
    )

    resources = _without_memory_resource(session_bodies[0].get("resources"))
    assert resources == [], "no repo binding + no fernet must produce zero repo resources"
