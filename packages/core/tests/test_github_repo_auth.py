"""Tests for github_repo_auth: the pure select_clone_auth decision table, the
shell resolve_clone_token orchestrator, and the is_app_installed_for_repo
panel-coverage probe.

The pure decision table is tested without I/O. The shell functions use
httpx.MockTransport (transport-level fake — guideline:testing); each test
owns its own RSA key material inline.
"""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import (
    is_app_installed_for_repo,
    resolve_clone_token,
    select_clone_auth,
)
from daimon.core.stores.domain import AgentRepoBindingRow
from pydantic import SecretStr

# ---------------------------------------------------------------------------
# RSA key pair helper (inline — each test owns its key material)
# ---------------------------------------------------------------------------


def _generate_rsa_keypair() -> str:
    """Return a PEM-encoded RSA private key string for App-JWT tests."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _make_binding(*, repo_url: str, ma_secret_ref: str) -> AgentRepoBindingRow:
    now = dt.datetime.now(dt.UTC)
    return AgentRepoBindingRow(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        repo_url=repo_url,
        default_branch="main",
        ma_secret_ref=ma_secret_ref,
        last_sync_at=None,
        last_sync_error=None,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Task 2: select_clone_auth (pure decision table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_per_agent_pat", "app_installed", "binding_is_public", "has_fallback_pat", "expected"),
    [
        # PAT always wins, regardless of the other inputs.
        (True, False, False, False, "pat"),
        (True, True, True, True, "pat"),
        (True, False, True, True, "pat"),
        # No PAT, App installed -> app mode, regardless of public/fallback.
        (False, True, False, False, "app"),
        (False, True, True, True, "app"),
        # No PAT, no App, public binding + fallback PAT -> public mode.
        (False, False, True, True, "public"),
        # No PAT, no App, public binding but NO fallback PAT -> none (fail loud).
        (False, False, True, False, "none"),
        # No PAT, no App, private binding -> none regardless of fallback.
        (False, False, False, True, "none"),
        (False, False, False, False, "none"),
    ],
)
def test_select_clone_auth_table(
    has_per_agent_pat: bool,
    app_installed: bool,
    binding_is_public: bool,
    has_fallback_pat: bool,
    expected: str,
) -> None:
    """select_clone_auth follows the precedence table: pat -> app -> public -> none."""
    mode = select_clone_auth(
        has_per_agent_pat=has_per_agent_pat,
        app_installed=app_installed,
        binding_is_public=binding_is_public,
        has_fallback_pat=has_fallback_pat,
    )

    assert mode == expected, (
        f"select_clone_auth(has_per_agent_pat={has_per_agent_pat}, "
        f"app_installed={app_installed}, binding_is_public={binding_is_public}, "
        f"has_fallback_pat={has_fallback_pat}) should be {expected!r}, got {mode!r}"
    )


# ---------------------------------------------------------------------------
# Task 2: resolve_clone_token (shell, MockTransport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_clone_token_pat_short_circuits_with_zero_github_calls() -> None:
    """When a per-agent PAT is present, resolve_clone_token returns it and issues
    ZERO GitHub HTTP requests — no JWT mint, no installation lookup."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"GitHub transport must not be called on the PAT path; got {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    binding = _make_binding(repo_url="acme/private-repo", ma_secret_ref="inline-pat:agent-1")

    token = await resolve_clone_token(
        client,
        binding=binding,
        per_agent_pat="ghp_per_agent_token",
        fallback_pat="ghp_fallback_token",
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        now=1_000_000,
    )

    assert token == "ghp_per_agent_token", "PAT must win over App/public"


@pytest.mark.asyncio
async def test_resolve_clone_token_app_installed_mints_installation_token() -> None:
    """No PAT + App installed -> mints and returns the installation token."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(status_code=200, json={"id": 777})
        if request.url.path == "/app/installations/777/access_tokens":
            return httpx.Response(status_code=201, json={"token": "ghs_installation_token"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    binding = _make_binding(repo_url="acme/widgets", ma_secret_ref="inline-pat:agent-1")

    token = await resolve_clone_token(
        client,
        binding=binding,
        per_agent_pat=None,
        fallback_pat=None,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        now=1_000_000,
    )

    assert token == "ghs_installation_token", "App mode must return the minted installation token"
    assert len(captured) == 2, "must issue exactly one lookup and one mint request"


@pytest.mark.asyncio
async def test_resolve_clone_token_app_not_installed_falls_back_to_public() -> None:
    """No PAT, App not installed (404), public (anon:) binding + fallback PAT -> public mode."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/oss-repo/installation":
            return httpx.Response(status_code=404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:")

    token = await resolve_clone_token(
        client,
        binding=binding,
        per_agent_pat=None,
        fallback_pat="ghp_operator_fallback",
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        now=1_000_000,
    )

    assert token == "ghp_operator_fallback", (
        "public binding with no App coverage must use the fallback PAT"
    )


@pytest.mark.asyncio
async def test_resolve_clone_token_app_lookup_error_falls_through_to_public_fallback() -> None:
    """A transient App-installation-lookup failure (e.g. 403 secondary rate-limit)
    must NOT crash the clone — it degrades to 'App unavailable' so a public (anon:)
    binding with an operator fallback PAT still resolves."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/oss-repo/installation":
            return httpx.Response(status_code=403)  # not 404 -> raise_for_status inside
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:")

    token = await resolve_clone_token(
        client,
        binding=binding,
        per_agent_pat=None,
        fallback_pat="ghp_operator_fallback",
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        now=1_000_000,
    )

    assert token == "ghp_operator_fallback", (
        "a transient App-lookup error must degrade to the fallback PAT, not crash"
    )


@pytest.mark.asyncio
async def test_resolve_clone_token_empty_string_pat_is_treated_as_no_token() -> None:
    """An empty-string per-agent PAT must not be returned verbatim (that would emit
    an empty authorization_token, which MA 400s). It falls through to the App/
    fallback/none decision like any other 'no token'."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/oss-repo/installation":
            return httpx.Response(status_code=404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:")

    token = await resolve_clone_token(
        client,
        binding=binding,
        per_agent_pat="",  # empty stored PAT -> "no token", not an empty clone token
        fallback_pat="ghp_operator_fallback",
        app_id=None,
        app_private_key=None,
        now=1_000_000,
    )

    assert token == "ghp_operator_fallback", (
        "empty-string PAT must fall through to the fallback, never be emitted verbatim"
    )


@pytest.mark.asyncio
async def test_resolve_clone_token_raises_when_no_credential_resolves() -> None:
    """Private binding, no PAT, no App configured, no fallback -> raises (step 4);
    never returns an empty authorization_token."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    binding = _make_binding(repo_url="acme/private-repo", ma_secret_ref="inline-pat:agent-1")

    with pytest.raises(DaimonError):
        await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=None,
            fallback_pat=None,
            app_id=None,
            app_private_key=None,
            now=1_000_000,
        )


@pytest.mark.asyncio
async def test_resolve_clone_token_raises_for_private_binding_even_with_fallback_pat() -> None:
    """A fallback PAT never applies to a private (inline-pat:) binding, even if the
    App is not installed — only anon: (public) bindings may use the fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/private-repo/installation":
            return httpx.Response(status_code=404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    binding = _make_binding(repo_url="acme/private-repo", ma_secret_ref="inline-pat:agent-1")

    with pytest.raises(DaimonError):
        await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=None,
            fallback_pat="ghp_operator_fallback",
            app_id="12345",
            app_private_key=SecretStr(_generate_rsa_keypair()),
            now=1_000_000,
        )


# ---------------------------------------------------------------------------
# Task 2: is_app_installed_for_repo (shell, MockTransport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_app_installed_for_repo_false_when_app_creds_unset() -> None:
    """No app_id/app_private_key -> returns False with zero HTTP calls (non-App deployments)."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"must not call GitHub when App creds are unset; got {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    installed = await is_app_installed_for_repo(
        client,
        app_id=None,
        app_private_key=None,
        owner="acme",
        repo="widgets",
        now=1_000_000,
    )

    assert installed is False, "must return False (not raise) when App creds are unset"


@pytest.mark.asyncio
async def test_is_app_installed_for_repo_true_when_installed() -> None:
    """App creds set and the App is installed on the repo -> True."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json={"id": 42})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    installed = await is_app_installed_for_repo(
        client,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        owner="acme",
        repo="widgets",
        now=1_000_000,
    )

    assert installed is True


@pytest.mark.asyncio
async def test_is_app_installed_for_repo_false_when_not_installed() -> None:
    """App creds set but the App is not installed on the repo (404) -> False."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    installed = await is_app_installed_for_repo(
        client,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        owner="acme",
        repo="widgets",
        now=1_000_000,
    )

    assert installed is False
