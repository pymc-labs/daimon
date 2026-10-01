"""Transport-fake unit tests for `daimon repo-bindings`.

Tests:
- test_list_proofless_returns_only_bindings_with_no_recorded_proof_across_tenants
- test_list_proofless_check_visibility_issues_one_request_per_distinct_repo_url
- test_list_proofless_check_visibility_renders_probe_error_when_probe_raises
- test_backfill_dry_run_writes_nothing
- test_backfill_yes_stamps_public_anon_binding
- test_backfill_does_not_stamp_private_repo
- test_backfill_does_not_stamp_404_repo
- test_backfill_does_not_stamp_token_backed_binding
- test_backfill_second_run_is_idempotent_and_issues_zero_requests
- test_cli_list_proofless_help_is_registered
"""

from __future__ import annotations

import uuid
from io import StringIO

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands.repo_bindings import backfill_public_proof, list_proofless
from daimon.adapters.cli.main import app
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.domain import RepoAccessProof
from daimon.testing.factories import make_agent_repo_binding, make_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from ..harness import build_cli_runtime


class _FakeCli:
    local_user = "testuser"


class _FakeSettings:
    cli = _FakeCli()


def _github_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://api.github.com")


# ---------------------------------------------------------------------------
# list-proofless
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_proofless_returns_only_bindings_with_no_recorded_proof_across_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """Two tenants each have one proofless binding; a third binding already
    carrying proof must be omitted from the listing."""
    async with db_session_factory() as session, session.begin():
        tenant_a = await make_tenant(session, workspace_id="repo-bindings-tenant-a")
        tenant_b = await make_tenant(session, workspace_id="repo-bindings-tenant-b")
        proofless_a = await make_agent_repo_binding(
            session, tenant=tenant_a, repo_url="owner/proofless-a", ma_secret_ref="anon:"
        )
        proofless_b = await make_agent_repo_binding(
            session, tenant=tenant_b, repo_url="owner/proofless-b", ma_secret_ref="anon:"
        )
        proven = await make_agent_repo_binding(
            session,
            tenant=tenant_a,
            repo_url="owner/proven",
            ma_secret_ref="anon:",
            proof=RepoAccessProof(kind="public", at=proofless_a.created_at, account_id=None),
        )

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=200)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    await list_proofless(rt=rt, console=console, check_visibility=False)

    output = out.getvalue()
    assert str(proofless_a.agent_id) in output, "proofless binding for tenant A must be listed"
    assert str(proofless_b.agent_id) in output, "proofless binding for tenant B must be listed"
    assert str(proven.agent_id) not in output, "a binding with recorded proof must be omitted"


@pytest.mark.asyncio
async def test_list_proofless_check_visibility_issues_one_request_per_distinct_repo_url(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """Three bindings on the SAME repo must probe that repo exactly once."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="repo-bindings-shared-repo")
        for _ in range(3):
            await make_agent_repo_binding(
                session,
                tenant=tenant,
                agent_id=uuid.uuid4(),
                repo_url="shared/owner-repo",
                ma_secret_ref="anon:",
            )

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        assert request.url.path == "/repos/shared/owner-repo"
        return httpx.Response(200, json={"private": False})

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=200)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await list_proofless(rt=rt, console=console, check_visibility=True, http_client=http_client)

    assert request_count == 1, (
        f"3 bindings sharing one repo must issue exactly 1 probe request, got {request_count}"
    )


@pytest.mark.asyncio
async def test_list_proofless_check_visibility_renders_probe_error_when_probe_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """A repo whose probe raises must render 'probe-error' and not abort the
    listing — the other row must still render normally."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="repo-bindings-probe-error")
        healthy = await make_agent_repo_binding(
            session, tenant=tenant, repo_url="owner/healthy", ma_secret_ref="anon:"
        )
        unreachable = await make_agent_repo_binding(
            session, tenant=tenant, repo_url="owner/unreachable", ma_secret_ref="anon:"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if "unreachable" in request.url.path:
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json={"private": False})

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=200)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await list_proofless(rt=rt, console=console, check_visibility=True, http_client=http_client)

    output = out.getvalue()
    assert str(healthy.agent_id) in output, "the healthy row must still render"
    assert str(unreachable.agent_id) in output, "the failing row must still render"
    assert "probe-error" in output, "a raised probe must render as probe-error"
    assert "public" in output, "the healthy row's visibility must still render as public"


# ---------------------------------------------------------------------------
# backfill-public-proof
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """--dry-run must never write; re-reading the candidate row after the run
    shows proof_kind still NULL."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="repo-bindings-dry-run")
        binding = await make_agent_repo_binding(
            session, tenant=tenant, repo_url="owner/dry-run-public", ma_secret_ref="anon:"
        )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"private": False})

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=200)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await backfill_public_proof(
            rt=rt, console=console, yes=True, dry_run=True, http_client=http_client
        )

    async with db_session_factory() as session:
        reread = await get_binding(session, tenant_id=binding.tenant_id, agent_id=binding.agent_id)

    assert reread is not None, "the binding must still exist"
    assert reread.proof_kind is None, "dry-run must write nothing; proof_kind must stay NULL"
    assert "dry-run" in out.getvalue().lower(), "dry-run must print a dry-run marker"


@pytest.mark.asyncio
async def test_backfill_yes_stamps_public_anon_binding(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """A no-token binding whose repo probes public gets proof_kind='public',
    a non-null proof_at, and a NULL proof_account_id."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="repo-bindings-stamp-public")
        binding = await make_agent_repo_binding(
            session, tenant=tenant, repo_url="owner/genuinely-public", ma_secret_ref="anon:"
        )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"private": False})

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=200)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await backfill_public_proof(
            rt=rt, console=console, yes=True, dry_run=False, http_client=http_client
        )

    async with db_session_factory() as session:
        reread = await get_binding(session, tenant_id=binding.tenant_id, agent_id=binding.agent_id)

    assert reread is not None
    assert reread.proof_kind == "public", "a verifiably-public anon: binding must be stamped"
    assert reread.proof_at is not None, "the stamped proof must carry a timestamp"
    assert reread.proof_account_id is None, (
        "a machine-probed proof must carry no account_id — no person asserted it"
    )


@pytest.mark.asyncio
async def test_backfill_does_not_stamp_private_repo(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """An anon: binding whose repo probes private must not be stamped."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="repo-bindings-private")
        binding = await make_agent_repo_binding(
            session, tenant=tenant, repo_url="owner/actually-private", ma_secret_ref="anon:"
        )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"private": True})

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=200)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await backfill_public_proof(
            rt=rt, console=console, yes=True, dry_run=False, http_client=http_client
        )

    async with db_session_factory() as session:
        reread = await get_binding(session, tenant_id=binding.tenant_id, agent_id=binding.agent_id)

    assert reread is not None
    assert reread.proof_kind is None, "a private repo must never be stamped, even for anon: binding"


@pytest.mark.asyncio
async def test_backfill_does_not_stamp_404_repo(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """An anon: binding whose repo 404s must not be stamped — a 404 is not
    verifiably public, matching is_public_repo's own semantic."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="repo-bindings-404")
        binding = await make_agent_repo_binding(
            session, tenant=tenant, repo_url="owner/does-not-exist", ma_secret_ref="anon:"
        )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=200)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await backfill_public_proof(
            rt=rt, console=console, yes=True, dry_run=False, http_client=http_client
        )

    async with db_session_factory() as session:
        reread = await get_binding(session, tenant_id=binding.tenant_id, agent_id=binding.agent_id)

    assert reread is not None
    assert reread.proof_kind is None, "a 404 repo must never be stamped"


@pytest.mark.asyncio
async def test_backfill_does_not_stamp_token_backed_binding(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """A binding whose ma_secret_ref starts inline-pat: must never be stamped,
    even when its repo probes public — only anon: bindings are candidates."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="repo-bindings-token-backed")
        binding = await make_agent_repo_binding(
            session,
            tenant=tenant,
            repo_url="owner/public-but-token-backed",
            ma_secret_ref="inline-pat:some-ref",
        )

    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"private": False})

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=200)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await backfill_public_proof(
            rt=rt, console=console, yes=True, dry_run=False, http_client=http_client
        )

    async with db_session_factory() as session:
        reread = await get_binding(session, tenant_id=binding.tenant_id, agent_id=binding.agent_id)

    assert reread is not None
    assert reread.proof_kind is None, (
        "a token-backed binding must never be a backfill candidate, so it stays unstamped"
    )
    assert request_count == 0, (
        "a token-backed binding must never even be probed — it is excluded before any HTTP call"
    )


@pytest.mark.asyncio
async def test_backfill_second_run_is_idempotent_and_issues_zero_requests(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """Running --yes twice in a row: the second run reports nothing to stamp
    and sends zero GitHub requests, because the first run's stamped binding
    dropped out of list_bindings_without_proof's candidate set."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, workspace_id="repo-bindings-idempotent")
        binding = await make_agent_repo_binding(
            session, tenant=tenant, repo_url="owner/idempotent-public", ma_secret_ref="anon:"
        )

    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"private": False})

    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    out1 = StringIO()
    console1 = Console(file=out1, force_terminal=False, highlight=False, width=200)
    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await backfill_public_proof(
            rt=rt, console=console1, yes=True, dry_run=False, http_client=http_client
        )
    assert request_count == 1, "the first run must probe the one distinct repo exactly once"

    out2 = StringIO()
    console2 = Console(file=out2, force_terminal=False, highlight=False, width=200)
    async with _github_client(httpx.MockTransport(handler)) as http_client:
        await backfill_public_proof(
            rt=rt, console=console2, yes=True, dry_run=False, http_client=http_client
        )

    assert request_count == 1, (
        f"the second run must issue zero further GitHub requests, total stayed {request_count}"
    )
    assert "No proofless anon: bindings to backfill" in out2.getvalue(), (
        "the second run must report nothing left to stamp"
    )

    async with db_session_factory() as session:
        reread = await get_binding(session, tenant_id=binding.tenant_id, agent_id=binding.agent_id)
    assert reread is not None
    assert reread.proof_kind == "public", "the binding stamped by the first run must stay stamped"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_list_proofless_help_is_registered() -> None:
    """Pin `repo-bindings` registration in main.py at the Typer level."""
    runner = CliRunner()
    result = runner.invoke(app, ["repo-bindings", "list-proofless", "--help"])
    assert result.exit_code == 0, result.output
