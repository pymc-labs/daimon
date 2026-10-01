"""Scenario: an agent's working repo and its skill repos are separate things.

`agent_repo_binding` is 1:1 — one working repo per agent, the one it clones
and runs. Skill repos are not: an agent may be enrolled in any number of
them, and enrollment writes `agent_skill_repo_credentials` instead. Before
that table existed, enrolling a skill repo had nowhere to record its PAT but
the binding, so enrolling one could repoint the code the agent actually runs.

These tests pin the separation against a real Postgres:

- enrolling skill repos leaves the working-repo binding byte-identical, and
  leaves an earlier enrollment untouched. The comparison is whole-row
  equality on the Pydantic model the store returns, so a changed
  `repo_url`, `default_branch`, `ma_secret_ref` or proof column all fail it.
- the two storage tiers resolve independently: `_resolve_sync_token` answers
  a skill repo from the skill-repo credential and the working repo from the
  binding, each with its own agent's PAT overlay.

The token resolver is exercised through the MCP adapter's real
`_resolve_sync_token` — the precedence table it delegates to is unit-tested
in `packages/adapters/mcp/tests/tools/test_skills_sync_token.py`; what is
proved here is that two repos enrolled through two different write paths do
not collide in one tenant.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.skills import (
    _resolve_sync_token,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, GithubSettings, Settings
from daimon.core.github_credentials import upsert_credential_encrypted
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.agent_repo_binding import get_binding, set_binding
from daimon.core.stores.agent_skill_repo_credentials import (
    get_skill_repo_credential,
    set_skill_repo_credential,
)
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_WORKING_REPO = "https://github.com/example-org/working-repo"
_SKILL_REPO = "https://github.com/example-org/skill-repo"
_SECOND_SKILL_REPO = "https://github.com/example-org/other-skill-repo"


def _settings() -> Settings:
    """No fallback PAT and no GitHub App: the only tokens that can resolve
    are the ones these tests seeded, so a passing assertion names a real
    storage tier rather than the operator's catch-all."""
    return Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://daimon:daimon@localhost:5432/daimon"),
        ),
        anthropic=AnthropicSettings(
            api_key=SecretStr("sk-test"),
            base_url=HttpUrl("https://api.anthropic.com"),
        ),
        github=GithubSettings(fallback_pat=None, app_id=None, app_private_key=None),
    )


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], fernet: MultiFernet) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=build_stub_anthropic(),
        settings=_settings(),
        fernet=fernet,
        deployment_default=DeploymentDefault(),
    )


def _identity(tenant_id: uuid.UUID) -> AuthIdentity:
    return AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, is_admin=True)


def _offline_client() -> httpx.AsyncClient:
    """Fails the test loudly if resolution reaches the network: with no App
    and no fallback configured, every answer below must come from the DB."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"no outbound HTTP request expected; got {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _bind_working_repo(
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
    plaintext_pat: str,
) -> None:
    """Mirror the Repo+Auth modal write path: binding plus a per-agent PAT overlay."""
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
            proof=None,
        )


async def _enroll_skill_repo(
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
    plaintext_pat: str,
) -> None:
    """Mirror the skill-repo enrollment write path: credential plus PAT overlay."""
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
        await set_skill_repo_credential(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url=repo_url,
            default_branch="main",
            path="skills",
            ma_secret_ref=f"inline-pat:{agent_id}",
            proof=None,
        )


async def test_enrolling_skill_repos_never_moves_the_agents_working_repo_binding(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fernet = make_fernet()
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    agent_id = uuid.uuid4()
    await _bind_working_repo(
        db_session_factory,
        fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url=_WORKING_REPO,
        plaintext_pat="ghp_working_repo_pat",
    )

    async with db_session_factory() as session:
        before = await get_binding(session, tenant_id=tenant.id, agent_id=agent_id)
    assert before is not None, "precondition: the agent must have a working-repo binding"

    await _enroll_skill_repo(
        db_session_factory,
        fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url=_SKILL_REPO,
        plaintext_pat="ghp_working_repo_pat",
    )

    async with db_session_factory() as session:
        after_first = await get_binding(session, tenant_id=tenant.id, agent_id=agent_id)
        first_credential = await get_skill_repo_credential(
            session, tenant_id=tenant.id, agent_id=agent_id, repo_url=_SKILL_REPO
        )
    assert after_first == before, (
        "enrolling a skill repo must leave the working-repo binding byte-identical -- "
        "repo_url, default_branch, ma_secret_ref and every proof column"
    )
    assert first_credential is not None, "the enrollment must have written its own credential row"

    # A second skill repo on the same agent: still 1:1 for the binding, still
    # N for skill repos, and the first enrollment is not an upsert target.
    await _enroll_skill_repo(
        db_session_factory,
        fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url=_SECOND_SKILL_REPO,
        plaintext_pat="ghp_working_repo_pat",
    )

    async with db_session_factory() as session:
        after_second = await get_binding(session, tenant_id=tenant.id, agent_id=agent_id)
        first_again = await get_skill_repo_credential(
            session, tenant_id=tenant.id, agent_id=agent_id, repo_url=_SKILL_REPO
        )
        second_credential = await get_skill_repo_credential(
            session, tenant_id=tenant.id, agent_id=agent_id, repo_url=_SECOND_SKILL_REPO
        )
    assert after_second == before, (
        "a second skill-repo enrollment must still leave the working-repo binding untouched"
    )
    assert first_again == first_credential, (
        "skill repos are keyed per (tenant, agent, repo), so enrolling another one must "
        "not overwrite the first enrollment"
    )
    assert second_credential is not None, "the second enrollment must have its own row"
    assert second_credential.repo_url == "example-org/other-skill-repo", (
        "the second credential must be stored against its own normalized repo"
    )


async def test_each_repo_resolves_the_credential_of_its_own_storage_tier(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two private repos in one tenant, enrolled through the two different
    write paths, on two agents so the overlays are distinguishable: the skill
    repo must resolve the skill-repo credential's PAT and the working repo
    the binding's, neither leaking into the other."""
    fernet = make_fernet()
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    working_agent_id = uuid.uuid4()
    skill_agent_id = uuid.uuid4()

    await _bind_working_repo(
        db_session_factory,
        fernet,
        tenant_id=tenant.id,
        agent_id=working_agent_id,
        repo_url=_WORKING_REPO,
        plaintext_pat="ghp_working_repo_pat",
    )
    await _enroll_skill_repo(
        db_session_factory,
        fernet,
        tenant_id=tenant.id,
        agent_id=skill_agent_id,
        repo_url=_SKILL_REPO,
        plaintext_pat="ghp_skill_repo_pat",
    )

    runtime = _runtime(db_session_factory, fernet)
    identity = _identity(tenant.id)
    async with _offline_client() as client:
        skill_token = await _resolve_sync_token(runtime, identity, _SKILL_REPO, client)
        working_token = await _resolve_sync_token(runtime, identity, _WORKING_REPO, client)

    assert skill_token == "ghp_skill_repo_pat", (
        "the skill repo must resolve the PAT recorded by its own enrollment, not the working repo's"
    )
    assert working_token == "ghp_working_repo_pat", (
        "the working repo has no skill-repo credential, so it must still resolve its "
        "binding's PAT -- enrolling a skill repo must not downgrade it to anonymous"
    )

    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=tenant.id, agent_id=working_agent_id)
    assert binding is not None and binding.repo_url == "example-org/working-repo", (
        "the working agent's binding still points at the working repo after both writes"
    )
