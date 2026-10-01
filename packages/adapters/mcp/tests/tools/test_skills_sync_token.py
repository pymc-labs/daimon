"""sync_skills credential resolution — private-repo fetch via bound PAT, and
the App-installation tier reached through the shared credential seam.

The MCP session JWT carries no agent_id claim (SC-4), so `sync_skills` cannot
resolve credentials from auth alone. Resolution goes URL → the caller-tenant's
agent_repo_binding → that agent's PAT overlay → (via the shared
daimon.core.github_repo_auth seam) a GitHub App installation token → the
operator fallback → anonymous. Without any of it, private bootstrap repos
404 on anonymous fetch (found live: test-guild bootstrap run
sesn_01S1PW8nFn9tZongAokvVpzd).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.skills import _resolve_sync_token
from daimon.core.config import AnthropicSettings, DatabaseSettings, GithubSettings, Settings
from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.agent_skill_repo_credentials import (
    list_skill_repo_credentials_for_repo,
    set_skill_repo_credential,
)
from daimon.core.stores.domain import RepoAccessProof
from daimon.testing.factories import make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

FERNET_KEY = "x" * 43 + "="  # length-44 urlsafe base64 — valid Fernet key shape


def _generate_rsa_keypair() -> str:
    """Return a PEM-encoded RSA private key string for App-JWT tests."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),
        settings=settings if settings is not None else MagicMock(),  # type: ignore[arg-type]
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


def _unreachable_client() -> httpx.AsyncClient:
    """An httpx.AsyncClient that fails the test loudly if any request reaches
    it — for resolver paths that must not touch the network at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"no outbound HTTP request expected; got {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


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
            proof=None,
        )


def _minimal_settings(
    *,
    fallback_pat: SecretStr | None = None,
    app_id: str | None = None,
    app_private_key: SecretStr | None = None,
) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://daimon:daimon@localhost:5432/daimon"),
        ),
        anthropic=AnthropicSettings(
            api_key=SecretStr("sk-test"),
            base_url=HttpUrl("https://api.anthropic.com"),
        ),
        github=GithubSettings(
            fallback_pat=fallback_pat, app_id=app_id, app_private_key=app_private_key
        ),
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

    token = await _resolve_sync_token(
        _make_runtime(sessionmaker), _identity(tenant.id), url, _unreachable_client()
    )
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

    settings = _minimal_settings()
    token = await _resolve_sync_token(
        _make_runtime(sessionmaker, settings=settings),
        _identity(other_tenant.id),
        url,
        _unreachable_client(),
    )
    assert token is None, "a tenant without its own binding must not resolve another tenant's PAT"


async def test_resolve_sync_token_returns_none_when_no_binding(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))

    settings = _minimal_settings()
    token = await _resolve_sync_token(
        _make_runtime(sessionmaker, settings=settings),
        _identity(tenant.id),
        "https://github.com/example-org/example-agent",
        _unreachable_client(),
    )
    assert token is None, (
        "no binding, no App, no fallback configured -> anonymous public-repo fetch"
    )


async def test_resolve_sync_token_returns_fallback_for_tenant_binding_with_no_overlay(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant-matching binding whose agent has no overlay PAT, and no App
    configured, still resolves the configured operator fallback, so a fresh
    agent's public skill sync isn't limited to the unauthenticated GitHub rate
    limit — matching today's shipped behavior."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    agent_id = uuid.uuid4()
    url = "https://github.com/example-org/example-agent"
    async with sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            repo_url=url,
            default_branch="main",
            ma_secret_ref="anon:",
            proof=None,
        )

    settings = _minimal_settings(fallback_pat=SecretStr("ghp_operator_fallback"))
    token = await _resolve_sync_token(
        _make_runtime(sessionmaker, settings=settings),
        _identity(tenant.id),
        url,
        _unreachable_client(),
    )
    assert token == "ghp_operator_fallback", (
        "a tenant binding with no overlay PAT and no App configured must resolve the "
        "configured operator fallback"
    )


async def test_resolve_sync_token_app_covered_repo_resolves_installation_token(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A repo covered by an installed GitHub App, with no per-agent credential
    bound but a recorded proof this tenant already demonstrated access to
    the repo, must resolve to a minted installation token from the
    interactive sync tool."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    agent_id = uuid.uuid4()
    url = "https://github.com/example-org/example-agent"
    async with sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            repo_url=url,
            default_branch="main",
            ma_secret_ref="anon:",
            proof=RepoAccessProof(kind="pat", at=datetime.now(UTC), account_id=None),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/example-org/example-agent/installation":
            return httpx.Response(200, json={"id": 555})
        if request.url.path == "/app/installations/555/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    settings = _minimal_settings(app_id="12345", app_private_key=SecretStr(_generate_rsa_keypair()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await _resolve_sync_token(
            _make_runtime(sessionmaker, settings=settings), _identity(tenant.id), url, client
        )
    assert token == "ghs_installation_token", (
        "an App-covered repo, with a recorded proof this tenant already demonstrated "
        "access to it, must resolve to the minted installation token"
    )


async def test_resolve_sync_token_app_covered_repo_without_proof_is_never_resolved(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The regression test for the cross-tenant read: a repo covered by an
    installed GitHub App, with no per-agent credential AND no recorded proof
    this tenant ever demonstrated access to it, must NOT resolve an
    installation token — the live installation lookup must never even be
    invoked. Before the proof gate, this exact call (a repo the App covers,
    no per-agent credential, no fallback) minted and returned an installation
    token regardless of whether this tenant had ever proven it could read the
    repo; confirmed failing prior to the proof_kind gate."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    agent_id = uuid.uuid4()
    url = "https://github.com/example-org/example-agent"
    async with sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            repo_url=url,
            default_branch="main",
            ma_secret_ref="anon:",
            proof=None,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"no GitHub App request expected without a recorded proof; got {request.url}"
        )

    settings = _minimal_settings(app_id="12345", app_private_key=SecretStr(_generate_rsa_keypair()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await _resolve_sync_token(
            _make_runtime(sessionmaker, settings=settings), _identity(tenant.id), url, client
        )
    assert token is None, (
        "an App-covered repo this tenant never demonstrated access to must resolve to "
        "an anonymous fetch, not a minted installation token — App installation coverage "
        "belongs to whoever installed the App for their own repo, not to this tenant"
    )


async def test_resolve_sync_token_per_agent_pat_wins_with_zero_lookup_requests(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A per-agent credential still wins over an App installation, and the
    live installation lookup must never be invoked — pinned as zero requests
    reaching the GitHub installation-lookup endpoint, not by reading a
    docstring, matching the clone path's short-circuit ordering."""
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

    lookup_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lookup_requests.append(request)
        pytest.fail(
            f"GitHub transport must not be reached on the per-agent-PAT path; got {request.url}"
        )

    settings = _minimal_settings(app_id="12345", app_private_key=SecretStr(_generate_rsa_keypair()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await _resolve_sync_token(
            _make_runtime(sessionmaker, settings=settings), _identity(tenant.id), url, client
        )
    assert token == "github_pat_test_secret", "per-agent token must win over App installation"
    assert len(lookup_requests) == 0, (
        "the live installation lookup must be invoked zero times when a per-agent "
        "credential already resolved"
    )


# ---------------------------------------------------------------------------
# Two-tier credential storage: agent_skill_repo_credentials, then the legacy
# agent_repo_binding. The fallback is what keeps tenants enrolled before the
# credential table existed syncing with zero data migration.
# ---------------------------------------------------------------------------


async def _seed_skill_repo_pat(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
    plaintext_pat: str,
) -> None:
    """Mirror the skill-repo enrollment write path: credential + PAT overlay."""
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


async def test_sync_token_prefers_a_skill_repo_credential_over_a_working_repo_binding(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Both storage tiers hold a usable PAT for the same repo, on different
    agents. The skill-repo credential is the one skill sync is about, so it
    wins; the working-repo binding's PAT must not be reached."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    url = "https://github.com/example-org/example-agent"
    await _seed_bound_pat(
        sessionmaker,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        repo_url=url,
        plaintext_pat="ghp_working_repo_binding",
    )
    await _seed_skill_repo_pat(
        sessionmaker,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        repo_url=url,
        plaintext_pat="ghp_skill_repo_credential",
    )

    token = await _resolve_sync_token(
        _make_runtime(sessionmaker, settings=_minimal_settings()),
        _identity(tenant.id),
        url,
        _unreachable_client(),
    )
    assert token == "ghp_skill_repo_credential", (
        "the skill-repo credential must be consulted before the working-repo binding"
    )


async def test_sync_token_falls_back_to_a_legacy_binding_when_no_skill_credential_exists(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant enrolled before the credential table has only an
    agent_repo_binding row. Its PAT must still resolve — that fallback is
    what makes this change need no data migration."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    url = "https://github.com/example-org/example-agent"
    await _seed_bound_pat(
        sessionmaker,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        repo_url=url,
        plaintext_pat="ghp_legacy_binding_pat",
    )

    async with sessionmaker() as session:
        credentials = await list_skill_repo_credentials_for_repo(
            session, tenant_id=tenant.id, repo_url=url
        )
    assert credentials == [], "precondition: this tenant has no skill-repo credential row"

    token = await _resolve_sync_token(
        _make_runtime(sessionmaker, settings=_minimal_settings()),
        _identity(tenant.id),
        url,
        _unreachable_client(),
    )
    assert token == "ghp_legacy_binding_pat", (
        "with no skill-repo credential, the legacy binding's PAT must still resolve"
    )


async def test_sync_token_uses_the_skill_credential_proof_kind(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The App tier's proof gate reads the skill-repo credential's proof: a
    credential recording proof (and no PAT anywhere, no binding at all) is
    enough for this tenant to reach a minted installation token."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    url = "https://github.com/example-org/example-agent"
    async with sessionmaker.begin() as session:
        await set_skill_repo_credential(
            session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            repo_url=url,
            default_branch="main",
            path="skills",
            ma_secret_ref="anon:",
            proof=RepoAccessProof(kind="pat", at=datetime.now(UTC), account_id=None),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/example-org/example-agent/installation":
            return httpx.Response(200, json={"id": 555})
        if request.url.path == "/app/installations/555/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    settings = _minimal_settings(app_id="12345", app_private_key=SecretStr(_generate_rsa_keypair()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await _resolve_sync_token(
            _make_runtime(sessionmaker, settings=settings), _identity(tenant.id), url, client
        )
    assert token == "ghs_installation_token", (
        "a proof recorded on the skill-repo credential must open the App tier, exactly "
        "as a proof recorded on a binding does"
    )


async def test_sync_token_takes_a_legacy_pat_when_the_skill_credential_has_only_a_proof(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant mid-migration: the skill-repo credential records a proof but
    its agent has no PAT overlay, while a legacy binding still holds a usable
    PAT. The PAT and the proof resolve independently, so the credential's
    proof must not suppress the binding's PAT — otherwise enrolling a skill
    repo would downgrade a tenant that used to sync authenticated."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(uuid.uuid4()))
    url = "https://github.com/example-org/example-agent"
    await _seed_bound_pat(
        sessionmaker,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        repo_url=url,
        plaintext_pat="ghp_legacy_binding_pat",
    )
    async with sessionmaker.begin() as session:
        await set_skill_repo_credential(
            session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),  # a different agent, with no PAT overlay
            repo_url=url,
            default_branch="main",
            path="skills",
            ma_secret_ref="anon:",
            proof=RepoAccessProof(kind="pat", at=datetime.now(UTC), account_id=None),
        )

    token = await _resolve_sync_token(
        _make_runtime(sessionmaker, settings=_minimal_settings()),
        _identity(tenant.id),
        url,
        _unreachable_client(),
    )
    assert token == "ghp_legacy_binding_pat", (
        "a skill-repo credential carrying only a proof must still fall through to the "
        "legacy binding's PAT — the two tiers resolve the PAT and the proof independently"
    )
