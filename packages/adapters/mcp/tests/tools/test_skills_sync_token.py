"""sync_skills credential resolution — private-repo fetch via bound PAT.

The MCP session JWT carries no agent_id claim (SC-4), so `sync_skills` cannot
resolve credentials from auth alone. Resolution goes URL → the caller-tenant's
agent_repo_binding → that agent's PAT overlay. Without it, private
bootstrap repos 404 on anonymous fetch (found live: test-guild bootstrap run
sesn_01S1PW8nFn9tZongAokvVpzd).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.skills import _resolve_sync_token
from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

FERNET_KEY = "x" * 43 + "="  # length-44 urlsafe base64 — valid Fernet key shape


def _make_runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=MagicMock(),  # type: ignore[arg-type]
        fernet=build_multifernet((FERNET_KEY,)),
        deployment_default=DeploymentDefault(),
    )


def _identity(tenant_id: uuid.UUID) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        is_admin=True,
    )


async def _seed_bound_pat(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
    plaintext_pat: str,
) -> None:
    """Mirror the Repo+Auth modal write path: binding + per-agent PAT overlay."""
    fernet = build_multifernet((FERNET_KEY,))
    await upsert_credential_encrypted(
        sessionmaker=sessionmaker,
        fernet=fernet,
        principal_id=agent_id,
        github_login="(inline-pat)",
        plaintext_token=plaintext_pat,
        scopes=("repo",),
    )
    async with sessionmaker.begin() as session:
        await set_agent_github_binding(session, agent_id=agent_id, principal_id=agent_id)
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url=repo_url,
            default_branch="main",
            ma_secret_ref=f"inline-pat:{agent_id}",
        )


async def test_resolve_sync_token_returns_bound_pat_when_tenant_binding_exists(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    agent_id = uuid.uuid4()
    url = "https://github.com/example-org/example-agent"
    await _seed_bound_pat(
        sessionmaker,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url=url,
        plaintext_pat="github_pat_test_secret",
    )

    token = await _resolve_sync_token(_make_runtime(sessionmaker), _identity(tenant.id), url)
    assert token == "github_pat_test_secret", (
        "sync token must resolve from the caller-tenant's repo binding PAT overlay"
    )


async def test_resolve_sync_token_ignores_other_tenants_binding(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        owner_tenant = await make_tenant(
            session, platform="discord", workspace_id=str(uuid.uuid4())
        )
        other_tenant = await make_tenant(
            session, platform="discord", workspace_id=str(uuid.uuid4())
        )
    agent_id = uuid.uuid4()
    url = "https://github.com/example-org/example-agent"
    await _seed_bound_pat(
        sessionmaker,
        tenant_id=owner_tenant.id,
        agent_id=agent_id,
        repo_url=url,
        plaintext_pat="github_pat_test_secret",
    )

    token = await _resolve_sync_token(_make_runtime(sessionmaker), _identity(other_tenant.id), url)
    assert token is None, "a tenant without its own binding must not resolve another tenant's PAT"


async def test_resolve_sync_token_returns_none_when_no_binding(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))

    token = await _resolve_sync_token(
        _make_runtime(sessionmaker),
        _identity(tenant.id),
        "https://github.com/example-org/example-agent",
    )
    assert token is None, "no binding for the repo means anonymous fetch (public-repo path)"
