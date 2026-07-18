"""Tests for daimon.core.skill_sync.resync (Plan 56-04).

Patterns:
- Real Postgres via db_session_factory.
- Transport-level fakes for MA (make_fake_ma_handler / build_fake_anthropic) and
  GitHub tarball fetch — no AsyncMock on client.beta.*, no model_construct.
- SDK response objects constructed inline via real constructors.
- Descriptive assertion messages on every assert.
"""

from __future__ import annotations

import io
import re
import tarfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core.github_credentials import encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_sync.orchestrator import sync_agent_skills
from daimon.core.skill_sync.resync import resync_bound_repo, should_resync
from daimon.core.specs import SkillRepo
from daimon.core.stores import agent_github_binding as ag_binding_store
from daimon.core.stores import agent_repo_binding as binding_store
from daimon.core.stores import github_app_installations as install_store
from daimon.core.stores import github_credentials as cred_store
from daimon.testing.factories import make_account, make_cli_principal, make_tenant
from daimon.testing.ma import (
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_APP_ID = "123456"


def _make_fernet() -> MultiFernet:
    return MultiFernet([Fernet(Fernet.generate_key())])


def _make_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_tarball_handler(tarball: bytes) -> tuple[list[httpx.Request], httpx.MockTransport]:
    """Return (captured_requests, transport) for a tarball-serving GitHub mock."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=tarball)

    return captured, httpx.MockTransport(handler)


async def _setup_agent_in_ma(
    *,
    fake_ma_handler: object,  # stateful handler from make_fake_ma_handler()
    anthropic_client: object,  # AsyncAnthropic backed by the handler
    tenant_id: uuid.UUID,
    agent_name: str,
    daimon_name: str | None = None,
) -> str:
    """Create an agent in the fake MA store; return its MA id string."""
    from anthropic import AsyncAnthropic

    client: AsyncAnthropic = anthropic_client  # type: ignore[assignment]
    agent = await client.beta.agents.create(
        name=agent_name,
        model="claude-sonnet-4-6",
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": daimon_name or agent_name,
        },
    )
    return agent.id


async def _setup_binding(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
    default_branch: str = "main",
) -> None:
    await binding_store.set_binding(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_url=repo_url,
        default_branch=default_branch,
        ma_secret_ref="stub",
    )


# ---------------------------------------------------------------------------
# Pure helper tests (no DB needed)
# ---------------------------------------------------------------------------


def test_resync_skips_non_default_branch_tag() -> None:
    assert not should_resync("refs/tags/v1", "main"), "tag ref must not trigger a resync"


def test_resync_skips_non_default_branch_other() -> None:
    assert not should_resync("refs/heads/feature-xyz", "main"), (
        "push to a non-default branch must not trigger a resync"
    )


def test_resync_skips_non_default_branch_when_default_is_custom() -> None:
    assert not should_resync("refs/heads/main", "develop"), (
        "push to main must not resync when binding's default_branch is develop"
    )


def test_resync_allows_default_branch_main() -> None:
    assert should_resync("refs/heads/main", "main"), (
        "push to refs/heads/main must trigger a resync when default_branch is main"
    )


def test_resync_allows_default_branch_custom() -> None:
    assert should_resync("refs/heads/develop", "develop"), (
        "push to refs/heads/develop must trigger resync when default_branch is develop"
    )


# ---------------------------------------------------------------------------
# Integration tests (real Postgres + transport-level MA + httpx fakes)
# ---------------------------------------------------------------------------


async def test_resync_persists_last_sync_on_success(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """resync_bound_repo calls update_last_sync with last_sync_at set + last_sync_error=None on success."""
    fernet = _make_fernet()
    cli = await make_cli_principal(db_session, os_user="resync-success")
    tenant_id = cli.tenant_id
    repo_url = "owner/persist-test-repo"

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    _, tarball_transport = _make_tarball_handler(tarball)
    http_client = httpx.AsyncClient(transport=tarball_transport)

    ma_handler = make_fake_ma_handler()
    anthropic_client = build_fake_anthropic(ma_handler)

    # Create agent in fake MA so bridge resolution can find it
    ma_agent_id = await _setup_agent_in_ma(
        fake_ma_handler=ma_handler,
        anthropic_client=anthropic_client,
        tenant_id=tenant_id,
        agent_name="resync-success",
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    await _setup_binding(db_session, tenant_id=tenant_id, agent_id=agent_id, repo_url=repo_url)
    await db_session.commit()

    before = datetime.now(UTC)
    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/main",
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    # Verify last_sync_at was updated and no error
    async with db_session_factory() as check_session:
        row = await binding_store.get_binding(check_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, "binding row must still exist after resync"
    assert row.last_sync_at is not None, "last_sync_at must be set after successful resync"
    assert row.last_sync_at >= before, "last_sync_at must be after resync started"
    assert row.last_sync_error is None, "last_sync_error must be None on success"


async def test_resync_records_error_on_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When sync_agent_skills raises, update_last_sync is called with a non-None last_sync_error.

    We force the failure by having the MA agent-list call raise an httpx.TransportError,
    which propagates out of _resolve_agent_name_and_principal and gets caught at the
    _resync_one_binding named boundary, recording last_sync_error.
    """
    fernet = _make_fernet()
    cli = await make_cli_principal(db_session, os_user="resync-fail")
    tenant_id = cli.tenant_id
    repo_url = "owner/error-test-repo"

    ma_agent_id = "agent_fail_probe_001"
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    await _setup_binding(db_session, tenant_id=tenant_id, agent_id=agent_id, repo_url=repo_url)
    await db_session.commit()

    # MA raises TransportError so _resolve_agent_name_and_principal fails
    def ma_transport_error(_request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("simulated MA outage")

    anthropic_client = build_fake_anthropic(ma_transport_error)
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b""))
    )

    # Should NOT raise — resync catches and persists error
    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/main",
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    async with db_session_factory() as check_session:
        row = await binding_store.get_binding(check_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, "binding row must still exist after failed resync"
    assert row.last_sync_error is not None, (
        "last_sync_error must be set when the resync fails at the named boundary"
    )


async def test_resync_skips_push_to_non_default_branch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A push to a non-default branch must not trigger a resync (no last_sync_at update)."""
    fernet = _make_fernet()
    cli = await make_cli_principal(db_session, os_user="resync-branch")
    tenant_id = cli.tenant_id
    repo_url = "owner/branch-filter-repo"

    ma_handler = make_fake_ma_handler()
    anthropic_client = build_fake_anthropic(ma_handler)
    ma_agent_id = await _setup_agent_in_ma(
        fake_ma_handler=ma_handler,
        anthropic_client=anthropic_client,
        tenant_id=tenant_id,
        agent_name="resync-branch",
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    await _setup_binding(
        db_session, tenant_id=tenant_id, agent_id=agent_id, repo_url=repo_url, default_branch="main"
    )
    await db_session.commit()

    fetch_calls: list[httpx.Request] = []

    def should_not_call(request: httpx.Request) -> httpx.Response:
        fetch_calls.append(request)
        return httpx.Response(200, content=b"")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(should_not_call))

    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/feature-branch",  # not the default branch
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert len(fetch_calls) == 0, "no GitHub fetch must happen when push is to a non-default branch"

    async with db_session_factory() as check_session:
        row = await binding_store.get_binding(check_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, "binding row must still exist"
    assert row.last_sync_at is None, (
        "last_sync_at must NOT be set when branch filter skips the resync"
    )


async def test_resync_prefers_installation_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When get_for_repo returns an installation, the resync mints an installation token
    and the token-exchange endpoint is called. Without an installation, it falls back
    to per-agent PAT / anon (no token exchange).
    """
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from daimon.core.config import GithubSettings

    fernet = _make_fernet()
    cli = await make_cli_principal(db_session, os_user="resync-apptoken")
    tenant_id = cli.tenant_id
    repo_url = "owner/app-token-repo"

    ma_handler = make_fake_ma_handler()
    anthropic_client = build_fake_anthropic(ma_handler)
    ma_agent_id = await _setup_agent_in_ma(
        fake_ma_handler=ma_handler,
        anthropic_client=anthropic_client,
        tenant_id=tenant_id,
        agent_name="resync-apptoken",
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    await _setup_binding(db_session, tenant_id=tenant_id, agent_id=agent_id, repo_url=repo_url)
    # Persist an App installation for the repo
    await install_store.upsert(
        db_session,
        installation_id=9001,
        account_login="owner",
        repo_full_names=["owner/app-token-repo"],
    )
    await db_session.commit()

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})

    token_exchange_calls: list[httpx.Request] = []
    tarball_calls: list[httpx.Request] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        if "access_tokens" in request.url.path:
            token_exchange_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "token": "ghs_app_installation_token_xyz",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )
        tarball_calls.append(request)
        return httpx.Response(200, content=tarball)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    # Generate a real RSA key for build_app_jwt
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    github_settings = GithubSettings(
        app_id=_TEST_APP_ID,
        app_private_key=pem,  # type: ignore[arg-type]
        webhook_secret="whsec_test",  # type: ignore[arg-type]
    )

    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/main",
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
        github_settings=github_settings,
    )

    assert len(token_exchange_calls) == 1, (
        "installation token exchange endpoint must be called when an App installation exists"
    )
    assert len(tarball_calls) >= 1, "tarball fetch must happen after token exchange"
    auth_header = tarball_calls[0].headers.get("authorization", "")
    assert "ghs_app_installation_token_xyz" in auth_header, (
        f"tarball fetch must carry the minted installation token, got: {auth_header!r}"
    )


async def test_resync_app_token_wins_over_per_agent_pat(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When BOTH an App installation AND a per-agent PAT exist, the App
    installation token must win (App > PAT > anon). Previously the fetcher re-resolved
    and unconditionally sent the per-agent PAT, silently shadowing the App token.
    """
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from daimon.core.config import GithubSettings

    fernet = _make_fernet()
    cli = await make_cli_principal(db_session, os_user="resync-apptoken-wins")
    tenant_id = cli.tenant_id
    repo_url = "owner/app-beats-pat-repo"

    ma_handler = make_fake_ma_handler()
    anthropic_client = build_fake_anthropic(ma_handler)
    ma_agent_id = await _setup_agent_in_ma(
        fake_ma_handler=ma_handler,
        anthropic_client=anthropic_client,
        tenant_id=tenant_id,
        agent_name="resync-apptoken-wins",
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    await _setup_binding(db_session, tenant_id=tenant_id, agent_id=agent_id, repo_url=repo_url)
    await install_store.upsert(
        db_session,
        installation_id=9002,
        account_login="owner",
        repo_full_names=[repo_url],
    )
    # ALSO give the agent a per-agent PAT overlay — the App token must still win.
    await cred_store.upsert_credential(
        db_session,
        principal_id=agent_id,
        github_login="agent-login",
        encrypted_token=encrypt_token(fernet, "ghp_per_agent_pat_should_lose"),
        scopes=("repo",),
    )
    await ag_binding_store.set_agent_github_binding(
        db_session,
        agent_id=agent_id,
        principal_id=agent_id,
    )
    await db_session.commit()

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    tarball_calls: list[httpx.Request] = []

    def github_handler(request: httpx.Request) -> httpx.Response:
        if "access_tokens" in request.url.path:
            return httpx.Response(
                200,
                json={"token": "ghs_app_token_wins", "expires_at": "2099-01-01T00:00:00Z"},
            )
        tarball_calls.append(request)
        return httpx.Response(200, content=tarball)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(github_handler))

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    github_settings = GithubSettings(
        app_id=_TEST_APP_ID,
        app_private_key=pem,  # type: ignore[arg-type]
        webhook_secret="whsec_test",  # type: ignore[arg-type]
    )

    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/main",
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
        github_settings=github_settings,
    )

    assert len(tarball_calls) >= 1, "tarball fetch must happen"
    auth_header = tarball_calls[0].headers.get("authorization", "")
    assert "ghs_app_token_wins" in auth_header, (
        f"App installation token must win over the per-agent PAT; got: {auth_header!r}"
    )
    assert "ghp_per_agent_pat_should_lose" not in auth_header, (
        "per-agent PAT must NOT be sent when an App installation token is available"
    )


async def test_resync_pat_tier_is_per_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per-agent credential isolation: two agents bound to the same repo.
    Only agent A has a per-agent credential overlay.
    Agent A's resync fetches with A's PAT.
    Agent B's resync fetches with NO credential (anon/public).
    Neither ever resolves the principal-default credential.
    """
    fernet = _make_fernet()
    tenant = await make_tenant(db_session)
    tenant_id = tenant.id
    repo_url = "owner/d25-isolation-repo"

    # Create agents in separate stateful MA fakes to avoid cross-interference
    ma_handler_a = make_fake_ma_handler()
    anthropic_a = build_fake_anthropic(ma_handler_a)
    ma_id_a = await _setup_agent_in_ma(
        fake_ma_handler=ma_handler_a,
        anthropic_client=anthropic_a,
        tenant_id=tenant_id,
        agent_name="agent-a",
    )

    ma_handler_b = make_fake_ma_handler()
    anthropic_b = build_fake_anthropic(ma_handler_b)
    ma_id_b = await _setup_agent_in_ma(
        fake_ma_handler=ma_handler_b,
        anthropic_client=anthropic_b,
        tenant_id=tenant_id,
        agent_name="agent-b",
    )

    agent_id_a = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_id_a)
    agent_id_b = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_id_b)

    # Bind both agents to the same repo
    await _setup_binding(db_session, tenant_id=tenant_id, agent_id=agent_id_a, repo_url=repo_url)
    await _setup_binding(db_session, tenant_id=tenant_id, agent_id=agent_id_b, repo_url=repo_url)

    # Only agent A gets a per-agent credential overlay
    pat_a = "ghp_agent_a_token_xyz"
    await cred_store.upsert_credential(
        db_session,
        principal_id=agent_id_a,
        github_login="agent-a-login",
        encrypted_token=encrypt_token(fernet, pat_a),
        scopes=("repo",),
    )
    await ag_binding_store.set_agent_github_binding(
        db_session,
        agent_id=agent_id_a,
        principal_id=agent_id_a,
    )
    await db_session.commit()

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})

    # --- Run resync for agent A (has per-agent PAT) ---
    auth_headers_a: list[str | None] = []

    def tarball_handler_a(request: httpx.Request) -> httpx.Response:
        auth_headers_a.append(request.headers.get("authorization"))
        return httpx.Response(200, content=tarball)

    http_client_a = httpx.AsyncClient(transport=httpx.MockTransport(tarball_handler_a))
    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/main",
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client_a,
        anthropic_client=anthropic_a,
    )

    # --- Run resync for agent B (NO per-agent credential) ---
    auth_headers_b: list[str | None] = []

    def tarball_handler_b(request: httpx.Request) -> httpx.Response:
        auth_headers_b.append(request.headers.get("authorization"))
        return httpx.Response(200, content=tarball)

    http_client_b = httpx.AsyncClient(transport=httpx.MockTransport(tarball_handler_b))
    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/main",
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client_b,
        anthropic_client=anthropic_b,
    )

    # Agent A must fetch with its PAT
    assert len(auth_headers_a) >= 1, "agent A must trigger a tarball fetch"
    auth_a = auth_headers_a[0]
    assert auth_a is not None, "agent A must carry an Authorization header"
    assert pat_a in auth_a, f"agent A's fetch must use agent A's PAT; got header: {auth_a!r}"

    # Agent B must fetch with NO credential (anon) — never principal-default
    assert len(auth_headers_b) >= 1, "agent B must trigger a tarball fetch"
    auth_b = auth_headers_b[0]
    assert auth_b is None, (
        f"agent B has no per-agent credential — fetch must be unauthenticated (anon); got: {auth_b!r}"
    )


# ---------------------------------------------------------------------------
# CR-01: panel sync and webhook resync must share ONE user_skills ledger
# ---------------------------------------------------------------------------


def _make_skills_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Stateful POST /v1/skills + versions handler (in-memory skill store).

    create -> assigns sk_N, latest_version="1"
    versions.create -> bumps the version on an existing skill
    Lets agents requests fall through (raises NotHandled).
    """
    skills: dict[str, dict[str, object]] = {}
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "POST" and path == "/v1/skills":
            counter["n"] += 1
            skill_id = f"sk_{counter['n']}"
            skills[skill_id] = {"id": skill_id, "version": "1"}
            return httpx.Response(
                200,
                json={
                    "id": skill_id,
                    "type": "custom",
                    "display_title": "x",
                    "latest_version": "1",
                    "created_at": "2026-04-21T00:00:00Z",
                    "updated_at": "2026-04-21T00:00:00Z",
                    "source": "custom",
                },
            )
        m = re.match(r"^/v1/skills/(?P<id>[^/]+)/versions$", path)
        if m and method == "POST":
            skill_id = m.group("id")
            new_version = str(int(str(skills.get(skill_id, {}).get("version", "1"))) + 1)
            skills.setdefault(skill_id, {"id": skill_id})["version"] = new_version
            return httpx.Response(
                200,
                json={
                    "id": skill_id,
                    "skill_id": skill_id,
                    "version": new_version,
                    "created_at": "2026-04-21T00:00:00Z",
                },
            )
        raise NotHandled

    return handler


async def test_panel_and_webhook_share_one_skill_ledger(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CR-01: the panel sync (Discord-user account principal) and the webhook resync
    (synthetic webhook principal) for the SAME (tenant, agent, repo) must write to ONE
    user_skills ledger.

    Drives the panel principal through sync_agent_skills first (creates the skill,
    synced=1), then runs the webhook resync (resync_bound_repo). With a shared ledger,
    the second run dedups: synced=0, updated=0, and skills.create fires exactly once.
    """
    fernet = _make_fernet()
    tenant = await make_tenant(db_session)
    tenant_id = tenant.id
    # Distinct Discord-user account (panel principal) — NOT the webhook system account.
    panel_account = await make_account(db_session, tenant=tenant)
    repo_url = "owner/cr01-shared-ledger-repo"

    skills_handler = _make_skills_handler()
    agents_handler = make_fake_ma_handler()
    anthropic_client = build_fake_anthropic(combine_handlers(skills_handler, agents_handler))

    # Stable agent on MA (daimon_tenant-tagged) so BOTH paths resolve the same agent.
    ma_agent = await anthropic_client.beta.agents.create(
        name="cr01-agent",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "cr01-agent"},
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent.id)

    await _setup_binding(db_session, tenant_id=tenant_id, agent_id=agent_id, repo_url=repo_url)
    await db_session.commit()

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})

    # --- Panel sync: Discord-user account principal ---
    panel_http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )
    panel_report = await sync_agent_skills(
        principal_id=panel_account.id,
        tenant_id=tenant_id,
        agent_name="cr01-agent",
        repos=[SkillRepo(url=repo_url, branch="main")],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=panel_http,
        anthropic_client=anthropic_client,
    )
    assert panel_report.synced == 1, (
        f"panel sync must create the skill on first run; got {panel_report}"
    )

    # --- Webhook resync: synthetic webhook system principal ---
    webhook_http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )
    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/main",
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=webhook_http,
        anthropic_client=anthropic_client,
    )

    # Inspect the ledger: there must be exactly ONE user_skills row for this agent
    # (under the shared, agent-stable key) — not two disjoint ledgers.
    from daimon.core.stores.user_skills import list_user_skills_for_agent  # noqa: PLC0415

    async with db_session_factory() as check:
        rows_under_agent = await list_user_skills_for_agent(
            check, tenant_id=tenant_id, principal_id=agent_id, agent_name="cr01-agent"
        )
    assert len(rows_under_agent) == 1, (
        "panel + webhook must share ONE ledger keyed on the agent's stable identity; "
        f"got {len(rows_under_agent)} rows under agent_id"
    )

    # And the webhook resync must have re-run with dedup — last_sync_error None proves
    # the resync completed; the single ledger row proves no duplicate re-upload.
    async with db_session_factory() as check:
        binding_row = await binding_store.get_binding(check, tenant_id=tenant_id, agent_id=agent_id)
    assert binding_row is not None and binding_row.last_sync_error is None, (
        "webhook resync must complete without error"
    )


# --- resync edge honors github_settings.max_tarball_bytes ---


async def test_resync_honors_github_settings_max_tarball_bytes_cap(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An operator-configured tiny max_tarball_bytes cap must reach the
    fetcher on the webhook resync edge — an over-cap tarball is skipped (repo
    recorded as errored via last_sync_error) rather than being buffered whole,
    proving github_settings.max_tarball_bytes threads through sync_agent_skills
    into GitHubTarballFetcher on this path."""
    from daimon.core.config import GithubSettings

    fernet = _make_fernet()
    cli = await make_cli_principal(db_session, os_user="resync-tarball-cap")
    tenant_id = cli.tenant_id
    repo_url = "owner/tarball-cap-repo"

    ma_handler = make_fake_ma_handler()
    anthropic_client = build_fake_anthropic(ma_handler)
    ma_agent_id = await _setup_agent_in_ma(
        fake_ma_handler=ma_handler,
        anthropic_client=anthropic_client,
        tenant_id=tenant_id,
        agent_name="resync-tarball-cap",
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    await _setup_binding(db_session, tenant_id=tenant_id, agent_id=agent_id, repo_url=repo_url)
    await db_session.commit()

    # Over-cap tarball body — larger than the 64-byte cap configured below.
    over_cap_tarball = _make_tarball({"r-main/SKILL.md": b"x" * 4096})

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=over_cap_tarball))
    )

    github_settings = GithubSettings(max_tarball_bytes=64)

    await resync_bound_repo(
        repo_full_name=repo_url,
        ref="refs/heads/main",
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
        github_settings=github_settings,
    )

    async with db_session_factory() as check_session:
        row = await binding_store.get_binding(check_session, tenant_id=tenant_id, agent_id=agent_id)
    assert row is not None, "binding row must still exist after the capped resync"
    assert row.last_sync_at is not None, (
        "resync must still complete (last_sync_at set) even though the repo was skipped"
    )
    assert row.last_sync_error is None, (
        "sync_agent_skills records an over-cap tarball as a skipped_repos entry, not a "
        "raised exception — the resync itself succeeds with the repo skipped, proving "
        "github_settings.max_tarball_bytes reached the fetcher on this edge"
    )
