"""Tests for github_repo_auth: the pure select_clone_auth / select_skill_sync_auth
decision tables and the shell resolve_clone_token / resolve_skill_sync_token
orchestrators.

The pure decision tables are tested without I/O. The shell functions use
httpx.MockTransport (transport-level fake — guideline:testing); each test
owns its own RSA key material inline. The injected installation lookup for
resolve_skill_sync_token is a plain async function, never a mock, per
guideline:testing's mock-at-boundaries rule — it is a first-party seam, not
an external API.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Callable

import httpx
import pytest
import structlog.testing
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import (
    derive_repo_access,
    render_clone_refusal,
    resolve_clone_token,
    resolve_skill_sync_token,
    select_clone_auth,
    select_skill_sync_auth,
)
from daimon.core.stores.domain import AgentRepoBindingRow, RepoProofKind
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


def _make_binding(
    *,
    repo_url: str,
    ma_secret_ref: str,
    proof_kind: RepoProofKind | None = None,
    proof_account_id: uuid.UUID | None = None,
) -> AgentRepoBindingRow:
    now = dt.datetime.now(dt.UTC)
    return AgentRepoBindingRow(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        repo_url=repo_url,
        default_branch="main",
        ma_secret_ref=ma_secret_ref,
        last_sync_at=None,
        last_sync_error=None,
        proof_kind=proof_kind,
        proof_at=now if proof_kind is not None else None,
        proof_account_id=proof_account_id,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Task 2: select_clone_auth (pure decision table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_per_agent_pat", "app_installed", "proof_kind", "has_fallback_pat", "expected"),
    [
        # PAT always wins, regardless of the other inputs -- proof is irrelevant.
        (True, False, None, False, "pat"),
        (True, True, "public", True, "pat"),
        (True, False, "pat", True, "pat"),
        # No PAT, App installed, a recorded proof (either kind) -> app mode.
        (False, True, "pat", False, "app"),
        (False, True, "public", True, "app"),
        # No PAT, App installed, NO recorded proof -> none, even with a
        # fallback PAT configured. This is the single most important case in
        # the phase: a no-token binding on a private App-covered repo must
        # refuse, not silently fall through to the public-read-only operator
        # token.
        (False, True, None, False, "none"),
        (False, True, None, True, "none"),
        # No PAT, App absent, a verified-public proof + fallback PAT -> public mode.
        (False, False, "public", True, "public"),
        # No PAT, App absent, a verified-public proof but NO fallback PAT -> none.
        (False, False, "public", False, "none"),
        # A pat-kind proof does not authorize the public-only operator token.
        (False, False, "pat", True, "none"),
        # No PAT, App absent, no recorded proof -> none regardless of fallback.
        (False, False, None, True, "none"),
        (False, False, None, False, "none"),
    ],
)
def test_select_clone_auth_table(
    has_per_agent_pat: bool,
    app_installed: bool,
    proof_kind: RepoProofKind | None,
    has_fallback_pat: bool,
    expected: str,
) -> None:
    """select_clone_auth follows the precedence table: pat -> app -> public -> none."""
    mode = select_clone_auth(
        has_per_agent_pat=has_per_agent_pat,
        app_installed=app_installed,
        proof_kind=proof_kind,
        has_fallback_pat=has_fallback_pat,
    )

    assert mode == expected, (
        f"select_clone_auth(has_per_agent_pat={has_per_agent_pat}, "
        f"app_installed={app_installed}, proof_kind={proof_kind!r}, "
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
    binding = _make_binding(
        repo_url="acme/widgets", ma_secret_ref="inline-pat:agent-1", proof_kind="pat"
    )

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
    """No PAT, App not installed (404), a verified-public proof + fallback PAT
    -> public mode."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/oss-repo/installation":
            return httpx.Response(status_code=404)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:", proof_kind="public")

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
    must NOT crash the clone — it degrades to 'App unavailable' so a binding
    with a recorded verified-public proof and an operator fallback PAT still
    resolves."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/oss-repo/installation":
            return httpx.Response(status_code=403)  # not 404 -> raise_for_status inside
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:", proof_kind="public")

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
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:", proof_kind="public")

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
    """A fallback PAT never applies to a binding with no recorded proof, even
    if the App is not installed — only a binding with a recorded
    verified-public proof may use the fallback."""

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


@pytest.mark.asyncio
async def test_resolve_clone_token_no_proof_raises_naming_rebind_fix() -> None:
    """This is the single most important case in the phase: a no-token
    binding on a private App-covered repo, with an operator fallback PAT
    configured, must refuse rather than silently fall through to the
    public-read-only fallback token. The App is genuinely installed (the
    lookup succeeds), a fallback PAT is configured, but the binding recorded
    no proof — resolve_clone_token must raise, and the message must name the
    actual fix (re-bind with a token), not misdiagnose App coverage."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/secret-repo/installation":
            return httpx.Response(status_code=200, json={"id": 555})
        if request.url.path == "/app/installations/555/access_tokens":
            # The best-effort App path mints eagerly, before the proof gate
            # decides whether to use the result — this token is minted but
            # then correctly discarded because the binding recorded no proof.
            return httpx.Response(status_code=201, json={"token": "ghs_unused_installation_token"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(repo_url="acme/secret-repo", ma_secret_ref="anon:")
    assert binding.proof_kind is None, "this test's premise is a proof-NULL binding"

    with pytest.raises(DaimonError, match="[Rr]e-bind") as exc_info:
        await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=None,
            fallback_pat="ghp_operator_fallback",
            app_id="12345",
            app_private_key=SecretStr(_generate_rsa_keypair()),
            now=1_000_000,
        )

    assert "token" in str(exc_info.value).lower(), (
        "the refusal must name the fix (re-bind with a token), not just say no credential exists"
    )


@pytest.mark.asyncio
async def test_resolve_clone_token_per_agent_pat_short_circuits_before_any_proof_check() -> None:
    """A per-agent PAT wins unconditionally and needs no recorded proof at
    all — it IS the caller-supplied credential. Zero GitHub HTTP requests are
    issued, pinning that the short-circuit precedes the App lookup that a
    proof check would otherwise gate."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"GitHub transport must not be called on the PAT path; got {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(repo_url="acme/private-repo", ma_secret_ref="inline-pat:agent-1")
    assert binding.proof_kind is None, "PAT must win even with no recorded proof"

    token = await resolve_clone_token(
        client,
        binding=binding,
        per_agent_pat="ghp_per_agent_token",
        fallback_pat="ghp_fallback_token",
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        now=1_000_000,
    )

    assert token == "ghp_per_agent_token", "per-agent PAT must win with zero proof/GitHub calls"


@pytest.mark.asyncio
async def test_resolve_clone_token_pat_kind_proof_never_unlocks_the_fallback_tier() -> None:
    """A pat-kind proof demonstrates the binder could read the repo with a
    token — it does not demonstrate the repo is public. It must never unlock
    the operator's public-read-only fallback PAT."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"App is not configured; no request expected: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(
        repo_url="acme/private-repo", ma_secret_ref="inline-pat:agent-1", proof_kind="pat"
    )

    with pytest.raises(DaimonError):
        await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=None,
            fallback_pat="ghp_operator_fallback",
            app_id=None,
            app_private_key=None,
            now=1_000_000,
        )


# ---------------------------------------------------------------------------
# Operator-config failures vs user re-bind failures (split failure copy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_clone_token_public_proof_no_fallback_names_operator_gap() -> None:
    """A correctly-bound public repo (verified-public proof) failing only
    because the deployment has no public-clone credential (no fallback PAT,
    App not configured) is an OPERATOR config gap. The message must name the
    deployment/operator credential gap and must NOT tell the user to re-bind
    a binding that is already correct."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"App is not configured; no request expected: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:", proof_kind="public")

    with pytest.raises(DaimonError) as exc_info:
        await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=None,
            fallback_pat=None,
            app_id=None,
            app_private_key=None,
            now=1_000_000,
        )

    message = str(exc_info.value)
    assert "deployment" in message.lower(), (
        "the operator-gap copy must name the deployment as what lacks the credential"
    )
    assert "operator" in message.lower(), (
        "the operator-gap copy must say an operator has to configure credentials"
    )
    assert re.search(r"[Rr]e-bind", message) is None, (
        "a correctly-bound public repo must never be blamed on the user's binding — "
        "the incident's misdiagnosis, asserted as absent"
    )


@pytest.mark.asyncio
async def test_resolve_clone_token_public_proof_no_fallback_emits_operator_event() -> None:
    """The operator-gap refusal must also emit an operator-facing log event
    (github_repo_auth.operator_credential_missing) carrying the repo_url, so
    the config gap is loud in operator logs, not only in the user's thread."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"App is not configured; no request expected: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:", proof_kind="public")

    with structlog.testing.capture_logs() as logs, pytest.raises(DaimonError):
        await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=None,
            fallback_pat=None,
            app_id=None,
            app_private_key=None,
            now=1_000_000,
        )

    operator_events = [
        e for e in logs if e.get("event") == "github_repo_auth.operator_credential_missing"
    ]
    assert len(operator_events) == 1, (
        "exactly one operator_credential_missing event must be emitted; "
        f"captured events: {[e.get('event') for e in logs]}"
    )
    assert operator_events[0].get("repo_url") == "acme/oss-repo", (
        "the operator event must carry the repo_url so the log alone identifies the repo"
    )


@pytest.mark.asyncio
async def test_resolve_clone_token_pat_proof_app_unavailable_keeps_rebind_and_logs() -> None:
    """A binding with a recorded pat-kind proof, a fallback PAT configured,
    but the App tier structurally unavailable (app_id=None) still raises the
    re-bind copy — a token re-bind genuinely fixes that case — AND emits an
    operator event recording that the App tier was unavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"App is not configured; no request expected: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(
        repo_url="acme/private-repo", ma_secret_ref="inline-pat:agent-1", proof_kind="pat"
    )

    with structlog.testing.capture_logs() as logs, pytest.raises(DaimonError, match="[Rr]e-bind"):
        await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=None,
            fallback_pat="ghp_operator_fallback",
            app_id=None,
            app_private_key=None,
            now=1_000_000,
        )

    app_events = [e for e in logs if e.get("event") == "github_repo_auth.app_not_configured"]
    assert len(app_events) == 1, (
        "an app_not_configured operator event must record the structurally unavailable "
        f"App tier; captured events: {[e.get('event') for e in logs]}"
    )
    assert app_events[0].get("repo_url") == "acme/private-repo", (
        "the app_not_configured event must carry the repo_url"
    )


# ---------------------------------------------------------------------------
# render_clone_refusal + derive_repo_access (pure, no I/O)
# ---------------------------------------------------------------------------


def test_render_clone_refusal_names_the_operator_gap_for_a_public_binding_with_no_fallback() -> (
    None
):
    """A correct public binding on a deployment with no public-clone credential
    is the operator's gap, so the sentence must not tell the user to re-bind."""
    message = render_clone_refusal(
        repo_url="acme/oss-repo", proof_kind="public", has_fallback_pat=False
    )
    assert "operator" in message.lower() and "deployment" in message.lower(), (
        "the operator-gap sentence must name the deployment and the operator"
    )
    assert re.search(r"[Rr]e-bind", message) is None, (
        "a correctly-bound public repo must never be blamed on the user's binding"
    )


def test_render_clone_refusal_names_the_rebind_fix_for_every_other_row() -> None:
    """Anything the user can fix is fixed by re-binding with a token that reads the repo."""
    message = render_clone_refusal(repo_url="acme/secret", proof_kind=None, has_fallback_pat=True)
    assert "acme/secret" in message, "the refusal must name the repo it is about"
    assert re.search(r"[Rr]e-bind", message) is not None, (
        "the refusal must name the fix (re-bind with a token) for a binding the user can correct"
    )


def test_derive_repo_access_reports_connected_per_agent_token_when_a_check_was_recorded() -> None:
    """A per-agent token with a recorded access check needs nothing else to clone."""
    binding = _make_binding(
        repo_url="acme/private-repo", ma_secret_ref="inline-pat:agent-1", proof_kind="pat"
    )
    access = derive_repo_access(binding, has_fallback_pat=False, app_configured=False)
    assert access.kind == "connected", "a checked per-agent token is a settled, working credential"
    assert access.credential == "per_agent_token", "the per-agent token is what the clone uses"
    assert access.corrective is None, "nothing needs correcting when the clone would work"


def test_derive_repo_access_reports_not_checked_when_a_pat_is_attached_without_a_proof() -> None:
    """A token nobody ever demonstrated against this repo is attached, not verified."""
    binding = _make_binding(repo_url="acme/private-repo", ma_secret_ref="inline-pat:agent-1")
    access = derive_repo_access(binding, has_fallback_pat=True, app_configured=True)
    assert access.kind == "not_checked", (
        "an attached token with no recorded access check must not be reported as connected"
    )
    assert access.credential == "per_agent_token", "the attached token is still the credential"
    assert access.checked_at is None, "there is no check timestamp to report"


def test_derive_repo_access_reports_deployment_public_when_a_public_proof_meets_a_fallback() -> (
    None
):
    """A verified-public repo plus the deployment's public-read credential clones."""
    binding = _make_binding(repo_url="acme/oss-repo", ma_secret_ref="anon:", proof_kind="public")
    access = derive_repo_access(binding, has_fallback_pat=True, app_configured=False)
    assert access.kind == "connected", "a public repo with the deployment credential is settled"
    assert access.credential == "deployment_public", (
        "the deployment's public-read token is what serves this clone"
    )


def test_derive_repo_access_reports_github_app_when_only_app_coverage_could_serve_it() -> None:
    """A recorded check with no other credential leaves the App, which is only
    resolved at clone time, so the state is 'checked' rather than 'connected'."""
    binding = _make_binding(repo_url="acme/private-repo", ma_secret_ref="anon:", proof_kind="pat")
    access = derive_repo_access(binding, has_fallback_pat=False, app_configured=True)
    assert access.kind == "checked", (
        "App coverage for this specific repo is decided at clone time, not here"
    )
    assert access.credential == "github_app", "the App is the only remaining credential"
    assert access.corrective is None, "a recorded check is not something to correct"


def test_derive_repo_access_needs_attention_with_a_corrective_when_nothing_authorizes() -> None:
    """No token, no proof, no App: the reader gets the same sentence a clone would raise."""
    binding = _make_binding(repo_url="acme/secret-repo", ma_secret_ref="anon:")
    access = derive_repo_access(binding, has_fallback_pat=True, app_configured=False)
    assert access.kind == "needs_attention", "nothing would authorize this clone"
    assert access.credential == "none", "there is no credential to name"
    assert access.corrective is not None and "acme/secret-repo" in access.corrective, (
        "the corrective must name the repo the reader has to fix"
    )


def test_derive_repo_access_carries_the_recorded_check_timestamp_and_account() -> None:
    """Attribution comes off the binding's recorded proof, not a fresh probe."""
    binder = uuid.uuid4()
    binding = _make_binding(
        repo_url="acme/oss-repo",
        ma_secret_ref="anon:",
        proof_kind="public",
        proof_account_id=binder,
    )
    access = derive_repo_access(binding, has_fallback_pat=True, app_configured=False)
    assert access.checked_at == binding.proof_at, "checked_at is the binding's recorded proof_at"
    assert access.checked_by_account_id == binder, (
        "checked_by_account_id is whoever demonstrated access at bind time"
    )


@pytest.mark.parametrize(
    ("ma_secret_ref", "proof_kind"),
    [("anon:", None), ("anon:", "public")],
)
@pytest.mark.asyncio
async def test_derive_repo_access_corrective_matches_resolve_clone_token_error_for_same_row(
    ma_secret_ref: str, proof_kind: RepoProofKind | None
) -> None:
    """The panel's corrective and the clone's refusal are the same sentence.

    Two surfaces describing one failure drift the moment each writes its own
    copy; this pins them to the same string for the rows that produce each of
    the two refusals.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"App is not configured; no request expected: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    binding = _make_binding(
        repo_url="acme/some-repo", ma_secret_ref=ma_secret_ref, proof_kind=proof_kind
    )
    access = derive_repo_access(binding, has_fallback_pat=False, app_configured=False)

    with pytest.raises(DaimonError) as exc_info:
        await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=None,
            fallback_pat=None,
            app_id=None,
            app_private_key=None,
            now=1_000_000,
        )

    assert access.kind == "needs_attention", "this row is the one a clone refuses"
    assert access.corrective == str(exc_info.value), (
        "derive_repo_access must hand back the exact sentence resolve_clone_token raises"
    )


# ---------------------------------------------------------------------------
# Task 1: select_skill_sync_auth (pure decision table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_per_agent_pat", "app_installed", "proof_kind", "has_fallback_pat", "expected"),
    [
        # PAT always wins, regardless of the other inputs -- proof is irrelevant.
        (True, False, None, False, "pat"),
        (True, True, "public", True, "pat"),
        (True, False, "pat", True, "pat"),
        # No PAT, App installed, a recorded proof (either kind) -> app mode,
        # regardless of fallback -- matches select_clone_auth's app-tier gate.
        (False, True, "pat", False, "app"),
        (False, True, "public", True, "app"),
        # No PAT, App installed, NO recorded proof -> the app tier is refused
        # even though the App genuinely covers the repo: App installation
        # coverage belongs to whoever installed the App for their own repo,
        # not to the tenant requesting this sync, so it cannot authorize a
        # read on its own. Falls through to fallback.
        (False, True, None, False, "none"),
        (False, True, None, True, "public"),
        # No PAT, App absent, no recorded proof, fallback configured ->
        # public (anonymous-but-authed) -- unlike the clone path, the
        # fallback tier here does NOT require proof_kind == "public": a
        # skill-sync URL may have no binding at all.
        (False, False, None, True, "public"),
        (False, False, "pat", True, "public"),
        (False, False, "public", True, "public"),
        # No PAT, App absent, no fallback -> none (legitimate anonymous fetch).
        (False, False, None, False, "none"),
    ],
)
def test_select_skill_sync_auth_table(
    has_per_agent_pat: bool,
    app_installed: bool,
    proof_kind: RepoProofKind | None,
    has_fallback_pat: bool,
    expected: str,
) -> None:
    """select_skill_sync_auth follows the precedence table: pat -> app -> public -> none.

    The app tier additionally requires proof_kind is not None: App
    installation coverage alone must never authorize a read of a repo this
    tenant never demonstrated access to.
    """
    mode = select_skill_sync_auth(
        has_per_agent_pat=has_per_agent_pat,
        app_installed=app_installed,
        proof_kind=proof_kind,
        has_fallback_pat=has_fallback_pat,
    )

    assert mode == expected, (
        f"select_skill_sync_auth(has_per_agent_pat={has_per_agent_pat}, "
        f"app_installed={app_installed}, proof_kind={proof_kind!r}, "
        f"has_fallback_pat={has_fallback_pat}) should be {expected!r}, got {mode!r}"
    )


# ---------------------------------------------------------------------------
# Task 1: the ordering-agreement test (D-27 mitigation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_per_agent_pat", "app_installed", "proof_kind", "has_fallback_pat"),
    [
        (True, False, None, False),
        (True, True, "pat", False),
        (True, False, "public", True),
        (True, True, None, True),
        (False, True, None, False),
        (False, True, "pat", True),
        (False, True, "public", True),
        (False, False, "public", True),
        (False, False, "pat", True),
        (False, False, None, True),
        (False, False, None, False),
    ],
)
def test_skill_sync_and_clone_selectors_agree_on_tier_ordering(
    has_per_agent_pat: bool,
    app_installed: bool,
    proof_kind: RepoProofKind | None,
    has_fallback_pat: bool,
) -> None:
    """select_skill_sync_auth and select_clone_auth must never drift apart on
    the pat/app tier ORDERING again — this ordering has already drifted
    apart on paper once in this codebase's planning history (two selectors
    were nearly designed with reversed App/PAT precedence before
    implementation evidence corrected it), and this test is what turns a
    future repeat of that drift into a failing test rather than a silent
    divergence discovered later.

    Both selectors now gate the App tier identically on ``proof_kind is not
    None`` — passing the SAME proof_kind to both functions must therefore
    produce the SAME pat/app decision. This is a strictly stronger check
    than before this gate existed (previously select_skill_sync_auth had no
    proof concept at all and reached "app" unconditionally on
    app_installed, which allowed a tenant to receive a minted installation
    token for a repo it never demonstrated access to, as long as the
    deployment's App happened to cover it for some unrelated tenant).

    The one REMAINING deliberate difference is the public (operator
    fallback) tier: select_clone_auth requires proof_kind == "public"
    specifically, while select_skill_sync_auth only requires
    has_fallback_pat, because a skill-sync URL may have no binding row at
    all to carry a proof_kind in the first place.
    """
    skill_sync_mode = select_skill_sync_auth(
        has_per_agent_pat=has_per_agent_pat,
        app_installed=app_installed,
        proof_kind=proof_kind,
        has_fallback_pat=has_fallback_pat,
    )
    clone_mode = select_clone_auth(
        has_per_agent_pat=has_per_agent_pat,
        app_installed=app_installed,
        proof_kind=proof_kind,
        has_fallback_pat=has_fallback_pat,
    )

    if has_per_agent_pat:
        assert skill_sync_mode == "pat" and clone_mode == "pat", (
            "a per-agent PAT must win identically on both selectors"
        )
        return

    if app_installed and proof_kind is not None:
        assert skill_sync_mode == "app" and clone_mode == "app", (
            "with a recorded proof, an installed App must win identically on both "
            f"selectors; got skill_sync={skill_sync_mode!r}, clone={clone_mode!r}"
        )
        return

    # App tier is refused on both (no proof, or App not installed). Only the
    # public/fallback tier may now diverge: select_clone_auth additionally
    # requires proof_kind == "public"; select_skill_sync_auth does not.
    assert clone_mode in ("public", "none"), clone_mode
    assert skill_sync_mode in ("public", "none"), skill_sync_mode
    if proof_kind == "public":
        assert skill_sync_mode == clone_mode, (
            "with a verified-public proof, both selectors' fallback tiers must agree; "
            f"got skill_sync={skill_sync_mode!r}, clone={clone_mode!r}"
        )
    elif has_fallback_pat:
        assert skill_sync_mode == "public" and clone_mode == "none", (
            "the remaining intentional divergence: select_skill_sync_auth's fallback "
            "tier does not require proof_kind == 'public' (no binding may exist at "
            "all), while select_clone_auth refuses without it -- "
            f"got skill_sync={skill_sync_mode!r}, clone={clone_mode!r}"
        )


# ---------------------------------------------------------------------------
# Task 2: resolve_skill_sync_token (shell, MockTransport + plain async lookup)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_pat_short_circuits_with_zero_calls() -> None:
    """A truthy per-agent token is returned with ZERO outbound HTTP requests
    AND zero invocations of the injected installation lookup -- this pins the
    per-agent-first short-circuit at the implementation level, not via a
    docstring claim."""
    outbound_requests: list[httpx.Request] = []
    lookup_calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        outbound_requests.append(request)
        pytest.fail(f"GitHub transport must not be called on the PAT path; got {request.url}")

    async def lookup(owner: str, repo: str) -> int | None:
        lookup_calls.append((owner, repo))
        return 999

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    token = await resolve_skill_sync_token(
        client,
        repo_url="acme/widgets",
        per_agent_pat="ghp_per_agent_token",
        proof_kind=None,
        fallback_pat="ghp_fallback_token",
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        installation_lookup=lookup,
        now=1_000_000,
    )

    assert token == "ghp_per_agent_token", "per-agent token must win"
    assert len(outbound_requests) == 0, "zero outbound HTTP requests on the PAT short-circuit"
    assert len(lookup_calls) == 0, "zero installation-lookup invocations on the PAT short-circuit"


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_app_installed_mints_installation_token() -> None:
    """No PAT, a recorded proof of access for this repo, and an injected
    lookup resolving an installation id -> mints and returns the
    installation token."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/777/access_tokens":
            return httpx.Response(status_code=201, json={"token": "ghs_installation_token"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def lookup(owner: str, repo: str) -> int | None:
        assert (owner, repo) == ("acme", "widgets")
        return 777

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    token = await resolve_skill_sync_token(
        client,
        repo_url="https://github.com/acme/widgets",
        per_agent_pat=None,
        proof_kind="pat",
        fallback_pat=None,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        installation_lookup=lookup,
        now=1_000_000,
    )

    assert token == "ghs_installation_token"


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_no_recorded_proof_never_invokes_installation_lookup() -> (
    None
):
    """The regression test for the cross-tenant read: no PAT, NO recorded
    proof of access for this repo, but the deployment's App genuinely covers
    it (the injected lookup WOULD resolve an installation, proven by a
    counting wrapper) -- the installation lookup must be invoked ZERO times,
    not just have its result discarded. Before the proof_kind gate, this
    exact call resolved and returned the minted installation token
    regardless of whether the caller's tenant had ever demonstrated it could
    read the repo; confirmed failing prior to the proof_kind gate."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"no GitHub App request expected without a recorded proof; got {request.url}"
        )

    lookup_calls = 0

    async def lookup(owner: str, repo: str) -> int | None:
        nonlocal lookup_calls
        lookup_calls += 1
        return 777  # would resolve if ever invoked -- proves the App really covers the repo

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    token = await resolve_skill_sync_token(
        client,
        repo_url="acme/widgets",
        per_agent_pat=None,
        proof_kind=None,
        fallback_pat=None,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        installation_lookup=lookup,
        now=1_000_000,
    )

    assert lookup_calls == 0, (
        "no recorded proof -> the installation lookup must never be invoked, "
        "not merely have its result discarded"
    )
    assert token is None, (
        "with no proof and no fallback PAT, the result must be the legitimate anonymous "
        "fetch, never a minted installation token for an unproven repo"
    )


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_no_installation_falls_back_to_public() -> None:
    """No PAT, injected lookup returns None (App not installed), a fallback
    PAT is configured -> fallback token."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no mint request expected: {request.method} {request.url}")

    async def lookup(owner: str, repo: str) -> int | None:
        return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    token = await resolve_skill_sync_token(
        client,
        repo_url="acme/oss-repo",
        per_agent_pat=None,
        proof_kind="pat",
        fallback_pat="ghp_operator_fallback",
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        installation_lookup=lookup,
        now=1_000_000,
    )

    assert token == "ghp_operator_fallback"


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_no_credential_returns_none_anonymous() -> None:
    """No PAT, no installation, no fallback -> None (the legitimate anonymous
    public-fetch case), never an empty string and never a raise."""

    async def lookup(owner: str, repo: str) -> int | None:
        return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    token = await resolve_skill_sync_token(
        client,
        repo_url="acme/public-repo",
        per_agent_pat=None,
        proof_kind="pat",
        fallback_pat=None,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        installation_lookup=lookup,
        now=1_000_000,
    )

    assert token is None, "no credential resolves -> None (anonymous), not an empty string"


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_lookup_error_degrades_to_fallback() -> None:
    """The injected lookup raising httpx.HTTPError degrades to the fallback
    tier rather than propagating -- a transient GitHub failure must not fail
    a sync a fallback token could still serve."""

    async def lookup(owner: str, repo: str) -> int | None:
        raise httpx.ConnectError("connection reset")

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    token = await resolve_skill_sync_token(
        client,
        repo_url="acme/oss-repo",
        per_agent_pat=None,
        proof_kind="pat",
        fallback_pat="ghp_operator_fallback",
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        installation_lookup=lookup,
        now=1_000_000,
    )

    assert token == "ghp_operator_fallback", "a lookup error must degrade to the fallback PAT"


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_none_lookup_skips_app_tier_entirely() -> None:
    """installation_lookup=None skips the App tier entirely -- zero outbound
    requests, no App JWT built -- and falls through to the fallback tier."""
    outbound_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        outbound_requests.append(request)
        raise AssertionError(f"no request expected when installation_lookup is None: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    token = await resolve_skill_sync_token(
        client,
        repo_url="acme/oss-repo",
        per_agent_pat=None,
        proof_kind="pat",
        fallback_pat="ghp_operator_fallback",
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        installation_lookup=None,
        now=1_000_000,
    )

    assert token == "ghp_operator_fallback"
    assert len(outbound_requests) == 0, (
        "installation_lookup=None must skip the App tier with zero requests"
    )


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_unnormalizable_repo_raises() -> None:
    """A repo reference that does not normalize to owner/repo (no slash) must
    raise DaimonError rather than silently falling through to an anonymous
    fetch of a different (or no) repo."""

    async def lookup(owner: str, repo: str) -> int | None:
        pytest.fail("lookup must not be invoked for an unnormalizable repo reference")

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    with pytest.raises(DaimonError):
        await resolve_skill_sync_token(
            client,
            repo_url="not-a-valid-repo-reference",
            per_agent_pat=None,
            proof_kind="pat",
            fallback_pat="ghp_operator_fallback",
            app_id="12345",
            app_private_key=SecretStr(_generate_rsa_keypair()),
            installation_lookup=lookup,
            now=1_000_000,
        )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Installation-token scope: a minted App token must cover only the bound repo
# ---------------------------------------------------------------------------
#
# The deployment's GitHub App is installed by repo owners for their own use,
# and one installation usually covers many repositories. A binding's recorded
# proof demonstrates access to ONE repository, so the token handed to that
# binding's session must be narrowed to that repository — never the whole
# installation. A verified-public proof demonstrates read access only, so its
# token must also be read-only.


def _capture_mint_body(
    captured: list[dict[str, object]],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(status_code=200, json={"id": 777})
        if request.url.path == "/app/installations/777/access_tokens":
            captured.append(json.loads(request.content or b"{}"))
            return httpx.Response(status_code=201, json={"token": "ghs_installation_token"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_kind", ["pat", "public"])
async def test_resolve_clone_token_app_token_is_scoped_to_the_bound_repository(
    proof_kind: RepoProofKind,
) -> None:
    """The clone token minted through the App covers only the bound repository."""
    captured: list[dict[str, object]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_capture_mint_body(captured)))
    binding = _make_binding(
        repo_url="acme/widgets", ma_secret_ref="anon:public", proof_kind=proof_kind
    )

    token = await resolve_clone_token(
        client,
        binding=binding,
        per_agent_pat=None,
        fallback_pat=None,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        now=1_000_000,
    )

    assert token == "ghs_installation_token"
    assert len(captured) == 1
    assert captured[0].get("repositories") == ["widgets"], (
        "an installation token must be narrowed to the bound repository, "
        f"not the whole installation; mint body was {captured[0]!r}"
    )


@pytest.mark.asyncio
async def test_resolve_clone_token_public_proof_app_token_is_read_only() -> None:
    """A verified-public proof demonstrates read access only: the App token
    minted for it must request read-only contents permission."""
    captured: list[dict[str, object]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_capture_mint_body(captured)))
    binding = _make_binding(
        repo_url="acme/widgets", ma_secret_ref="anon:public", proof_kind="public"
    )

    await resolve_clone_token(
        client,
        binding=binding,
        per_agent_pat=None,
        fallback_pat=None,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        now=1_000_000,
    )

    assert len(captured) == 1
    assert captured[0].get("permissions") == {"contents": "read"}, (
        f"a public-proof binding must get a read-only token; mint body was {captured[0]!r}"
    )


@pytest.mark.asyncio
async def test_resolve_skill_sync_token_app_token_is_read_only_and_repo_scoped() -> None:
    """Skill sync only reads the named repo: its App token is narrowed to that
    repository with read-only contents permission."""
    captured: list[dict[str, object]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_capture_mint_body(captured)))

    async def lookup(owner: str, repo: str) -> int | None:
        return 777

    token = await resolve_skill_sync_token(
        client,
        repo_url="https://github.com/acme/widgets",
        per_agent_pat=None,
        proof_kind="pat",
        fallback_pat=None,
        app_id="12345",
        app_private_key=SecretStr(_generate_rsa_keypair()),
        installation_lookup=lookup,
        now=1_000_000,
    )

    assert token == "ghs_installation_token"
    assert captured == [{"repositories": ["widgets"], "permissions": {"contents": "read"}}]
