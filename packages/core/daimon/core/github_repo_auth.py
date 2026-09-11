"""Clone-credential resolution for App-or-PAT repo auth.

Owns the mode decisions (pure) and the token-resolution orchestrations
(shell, injected httpx) for BOTH of daimon's independent GitHub-clone-auth
callers: the per-agent clone path (`agent_repo_binding`, a bound repo with a
recorded proof of access) and the skill-sync path (a bare repo URL that may
have no binding row at all). No DB access, no module-level singletons —
callers inject the `httpx.AsyncClient` and already-resolved per-agent PAT.

Pure functions:
  select_clone_auth — deterministic pat/app/public/none decision table for a
    bound repo, gated on a recorded proof of access.
  select_skill_sync_auth — the skill-sync sibling: same tier ORDERING
    (per-agent token -> App installation -> operator fallback -> anonymous)
    and the same App-tier proof gate, but the proof isn't read off a single
    bound row — a skill-sync URL may have no binding row at all, so callers
    resolve `proof_kind` by checking whether ANY of their own tenant's
    bindings for that exact repo recorded proof.
  normalize_owner_repo — public `owner/repo` normalizer shared by both shell
    resolvers below (existing private copies elsewhere are untouched by this
    module; see each copy's own docstring).

Shell functions (injected httpx):
  resolve_clone_token — PAT short-circuit (zero GitHub I/O) -> App
    installation-token mint (only for a binding with recorded proof) ->
    operator fallback PAT (only for a binding with a recorded
    verified-public proof) -> raise. Never returns an empty string.
  resolve_skill_sync_token — PAT short-circuit (zero GitHub I/O) -> App
    installation-token mint (only when an injected installation lookup is
    supplied AND the caller-resolved `proof_kind` is not None) -> operator
    fallback PAT -> None (anonymous fetch). Never returns an empty string.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
import structlog
from daimon.core.errors import DaimonError
from daimon.core.github_app_auth import (
    build_app_jwt,
    get_installation_id_for_repo,
    mint_installation_token,
)
from daimon.core.stores.domain import AgentRepoBindingRow, RepoProofKind
from pydantic import SecretStr

log = structlog.get_logger()

__all__ = [
    "select_clone_auth",
    "resolve_clone_token",
    "select_skill_sync_auth",
    "resolve_skill_sync_token",
]

type InstallationLookup = Callable[[str, str], Awaitable[int | None]]
"""Injected `(owner, repo) -> installation_id | None` lookup.

Lets the caller choose freshness: an interactive caller injects a live
GitHub lookup (`get_installation_id_for_repo`); an unattended batch caller
injects a cheap read of the cached `github_app_installations` table. That
choice belongs to the caller, not to `resolve_skill_sync_token`.
"""


def normalize_owner_repo(url: str) -> str:
    """Extract `owner/repo` from a URL or short-form path.

    Accepts: 'https://github.com/owner/repo', 'github.com/owner/repo',
    'owner/repo', any with a trailing '/' or '.git'.

    Public so the skill-sync credential seam can take a raw repo URL from
    either caller (the interactive MCP tool or the webhook resync batch)
    without either owning its own copy. Two private copies of this same body
    exist elsewhere in the codebase today
    (`daimon.core.skill_sync.fetcher._normalize_owner_repo` and
    `daimon.core.stores.agent_repo_binding._normalize_owner_repo`) — this
    plan does not repoint or delete either; that is out of scope here.
    """
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


def select_clone_auth(
    *,
    has_per_agent_pat: bool,
    app_installed: bool,
    proof_kind: RepoProofKind | None,
    has_fallback_pat: bool,
) -> Literal["pat", "app", "public", "none"]:
    """Decide the clone-auth mode per the precedence table.

    Order:
      1. A per-agent PAT always wins and needs no proof — it IS the
         credential, and the caller supplied it directly. GitHub itself
         enforces what it can read.
      2. An App installation token requires that the binding recorded proof
         the binder could read the repo (``proof_kind is not None``). App
         installation coverage on its own says nothing about who bound the
         repo: the deployment's App is installed by repo owners for their
         own use, not by the tenant binding the repo, so coverage alone
         cannot stand in for a demonstrated access check.
      3. The operator fallback token requires specifically a verified-public
         proof kind. That token is public-read-only; serving it for anything
         else (including a PAT-kind proof, which only demonstrates the
         binder could read the repo with a token, not that the repo is
         public) produces a clone that cannot work.
      4. Otherwise none — caller must raise; never emit an empty
         ``authorization_token``.

    ``proof_kind`` is the RECORDED proof read off the binding row, never a
    fresh probe — proof is established once at bind time and is not
    re-verified here or by any caller of this function.

    Pure — no I/O, no clock. Callers resolve ``app_installed`` (an
    installation lookup) and ``has_fallback_pat`` before calling this.

    See also ``select_skill_sync_auth``, the skill-sync sibling of this
    function: it shares this exact tier ORDERING on purpose (a test asserts
    the two agree), but takes no ``proof_kind`` at all, because a skill-sync
    URL may have no binding row to record proof against. This function is
    NOT widened to accept an optional binding for that case — doing so would
    thread an implicit "no proof required" branch through a decision whose
    whole point is that proof is required, so the two stay separate
    functions differing only in their inputs.
    """
    if has_per_agent_pat:
        return "pat"
    if app_installed and proof_kind is not None:
        return "app"
    if proof_kind == "public" and has_fallback_pat:
        return "public"
    return "none"


def select_skill_sync_auth(
    *,
    has_per_agent_pat: bool,
    app_installed: bool,
    proof_kind: RepoProofKind | None,
    has_fallback_pat: bool,
) -> Literal["pat", "app", "public", "none"]:
    """Decide the skill-sync credential mode for a bare repo URL.

    Scope: credential selection for a skill-repo fetch — the interactive
    ``sync_skills`` MCP tool and the per-agent webhook skill-resync path
    both select a credential for a repo URL that may have no
    ``agent_repo_binding`` row at all.

    Order, identical to ``select_clone_auth``: a per-agent token wins; else
    an App installation covering the repo AND a recorded proof of access
    for THIS caller's tenant; else the operator fallback token; else
    ``"none"``. This function shares ``select_clone_auth``'s tier ORDERING
    deliberately — a test
    (``test_skill_sync_and_clone_selectors_agree_on_tier_ordering``) asserts
    the two agree on the App tier for every combination of the shared
    inputs, so neither can silently drift from the other again.

    ``proof_kind`` gates the App tier exactly like ``select_clone_auth``:
    App installation coverage is installed by repo owners for their own
    use, not by the tenant requesting the sync, so coverage alone cannot
    authorize reading a repo this tenant never demonstrated access to.
    Unlike the clone path, ``proof_kind`` here is NOT read off a single
    bound row — a skill-sync URL may have no binding at all — callers
    resolve it by checking whether ANY of the calling tenant's own bindings
    for this exact repo recorded proof (see
    ``daimon.core.stores.agent_repo_binding.get_tenant_repo_proof_kind``).
    A repo this tenant has never bound (with proof) is never eligible for
    the App tier, closing the cross-tenant read this function used to
    allow: any tenant admin could otherwise name a repo the deployment's
    App happens to cover — installed by an unrelated tenant for their own
    use — and receive a minted installation token for it.

    The operator fallback tier is intentionally NOT gated on ``proof_kind``
    (unlike the clone path's ``proof_kind == "public"`` requirement): a
    skill-sync URL legitimately has no binding at all, and the fallback
    token is the deployment-wide public-read credential regardless of
    whether this specific repo was ever bound. This is the one remaining
    behavioral difference from ``select_clone_auth``.

    ``"none"`` here means the legitimate anonymous public fetch, not a
    refusal — unlike the clone path, which raises when its decision reaches
    ``"none"``. The shell counterpart, ``resolve_skill_sync_token``,
    converts ``"none"`` to ``None``, which the skill fetchers already accept
    as "no credential" for a public repo. Public skill repos legitimately
    sync with no credential on a deployment that never configured an
    operator fallback token, so anonymous is not an error here the way it
    is for a bound clone.

    Pure — no I/O, no clock. Callers resolve ``app_installed`` (an
    installation lookup), ``proof_kind`` (a bindings-table read), and
    ``has_fallback_pat`` before calling this.
    """
    if has_per_agent_pat:
        return "pat"
    if app_installed and proof_kind is not None:
        return "app"
    if has_fallback_pat:
        return "public"
    return "none"


async def resolve_clone_token(
    http_client: httpx.AsyncClient,
    *,
    binding: AgentRepoBindingRow,
    per_agent_pat: str | None,
    fallback_pat: str | None,
    app_id: str | None,
    app_private_key: SecretStr | None,
    now: int,
) -> str:
    """Resolve the clone token for a bound repo.

    Short-circuits on ``per_agent_pat`` before any GitHub HTTP call (per-agent PAT
    wins; Pitfall 2 — never mint a JWT / do an installation lookup when a PAT
    is already available). Otherwise attempts the App installation-token
    path (on-demand ``GET /repos/{owner}/{repo}/installation`` ->
    ``mint_installation_token``) when the binding recorded proof of access,
    falls back to the operator fallback PAT on a binding that recorded a
    verified-public proof, and raises ``DaimonError`` when none of those
    apply. Never returns an empty string (MA rejects an empty
    ``authorization_token`` with a 400).

    Args:
        http_client: Injected async HTTP client. Caller owns lifecycle.
        binding: The agent's repo binding (repo_url is canonical owner/repo).
        per_agent_pat: Already-resolved per-agent PAT overlay, or None.
        fallback_pat: Operator-wide fallback PAT, or None/empty (treated the
            same — an empty string is "no token").
        app_id: GitHub App id, or None if the App is not configured.
        app_private_key: GitHub App private key, or None if not configured.
        now: Current Unix timestamp (int) — caller provides this so the App
            JWT mint stays pure (no clock read inside this module).

    Returns:
        The resolved clone token (PAT or minted installation token).

    Raises:
        DaimonError: When no PAT, App coverage, or fallback PAT resolves —
            the fail-loud branch.
    """
    # Empty string is "no token" (same as fallback_pat's bool() handling below);
    # returning it verbatim would emit an empty authorization_token (MA 400s).
    if per_agent_pat:
        return per_agent_pat

    owner, repo = binding.repo_url.split("/", 1)
    # The legacy no-token string tag means "no PAT was supplied at bind
    # time" — it is not evidence the repo is public. The binding's recorded
    # proof is; select_clone_auth reads it directly, below.
    has_fallback_pat = bool(fallback_pat)

    # Best-effort App path: a transient GitHub failure on the installation
    # lookup / token mint (e.g. a 403 secondary-rate-limit, common once the App
    # has many installs) must not take down a clone that a public+fallback-PAT
    # binding could still serve. On any HTTP error, degrade to "App unavailable"
    # and fall through to the fallback/none decision. A malformed App private
    # key still raises from build_app_jwt (operator misconfig — fail loud so it
    # is fixed, not silently masked). A private binding with no App and no PAT
    # still fails loudly at the raise below.
    app_token: str | None = None
    if app_id is not None and app_private_key is not None:
        app_jwt = build_app_jwt(app_private_key.get_secret_value(), app_id, now=now)
        try:
            installation_id = await get_installation_id_for_repo(
                http_client, jwt=app_jwt, owner=owner, repo=repo
            )
            if installation_id is not None:
                app_token = await mint_installation_token(
                    http_client, jwt=app_jwt, installation_id=installation_id
                )
        except httpx.HTTPError as err:
            log.warning(
                "github_repo_auth.app_path_failed",
                repo_url=binding.repo_url,
                error=str(err),
            )

    mode = select_clone_auth(
        has_per_agent_pat=False,
        app_installed=app_token is not None,
        proof_kind=binding.proof_kind,
        has_fallback_pat=has_fallback_pat,
    )

    if mode == "app":
        assert app_token is not None  # narrows: app_installed implies a minted token
        return app_token
    if mode == "public":
        assert fallback_pat  # narrows: has_fallback_pat implies this is truthy
        return fallback_pat

    app_configured = app_id is not None and app_private_key is not None
    # The operator branch is checked first because the public+no-fallback case
    # is the one failure the user cannot fix: the binding is correct, the
    # deployment just has no public-clone credential configured.
    if binding.proof_kind == "public" and not has_fallback_pat:
        log.error(
            "github_repo_auth.operator_credential_missing",
            repo_url=binding.repo_url,
            proof_kind=binding.proof_kind,
            app_configured=app_configured,
        )
        raise DaimonError(
            f"This deployment has no credential configured for cloning public "
            f"repositories, so {binding.repo_url} cannot be cloned. The repo binding "
            "itself is correct — an operator must configure the deployment's GitHub "
            "credentials."
        )
    if binding.proof_kind is not None and not app_configured:
        log.error(
            "github_repo_auth.app_not_configured",
            repo_url=binding.repo_url,
            proof_kind=binding.proof_kind,
        )
    raise DaimonError(
        f"No credential is authorized to clone {binding.repo_url}. Re-bind this repo "
        "with a GitHub token that can read it, using request_repo_binding."
    )


async def resolve_skill_sync_token(
    http_client: httpx.AsyncClient,
    *,
    repo_url: str,
    per_agent_pat: str | None,
    proof_kind: RepoProofKind | None,
    fallback_pat: str | None,
    app_id: str | None,
    app_private_key: SecretStr | None,
    installation_lookup: InstallationLookup | None,
    now: int,
) -> str | None:
    """Resolve the skill-sync token for a bare repo URL.

    Structurally parallel to ``resolve_clone_token`` — same ordering, same
    short-circuit discipline — differing only in its inputs (``proof_kind``
    is a caller-resolved bindings-table read rather than a single bound
    row's field, and the installation lookup is injected instead of
    always-live) and in returning ``None`` for the anonymous case where the
    clone path raises.

    Short-circuits on ``per_agent_pat`` before any GitHub HTTP call, before
    building an App JWT, and before invoking ``installation_lookup`` — the
    same reasoning ``resolve_clone_token`` records: never mint a JWT or do an
    installation lookup when a per-agent credential is already available. An
    empty string is "no token", exactly as the clone path treats it.

    The App tier is only ATTEMPTED (installation lookup + mint) when
    ``installation_lookup`` is not ``None``, ``proof_kind`` is not ``None``,
    AND both App settings (``app_id``, ``app_private_key``) are present.
    ``proof_kind`` gates the attempt itself (not just the final decision) so
    a repo this tenant never demonstrated access to never triggers a live
    GitHub installation lookup or token mint at all — closing both the
    cross-tenant credential read AND the App-installation-existence probe
    this endpoint would otherwise expose to any caller who can name a repo
    URL. Passing ``installation_lookup=None`` (or ``proof_kind=None``) skips
    the App tier entirely, including the JWT build, with zero outbound
    requests.

    ``installation_lookup`` is injected precisely so an interactive caller
    can pass a live GitHub lookup (``get_installation_id_for_repo``) while an
    unattended batch caller passes a cheap read of the cached
    ``github_app_installations`` table — that freshness choice belongs to
    the caller, not to this function. ``proof_kind`` is likewise injected
    rather than looked up here: this function has no DB access; callers
    resolve it (e.g. via
    ``daimon.core.stores.agent_repo_binding.get_tenant_repo_proof_kind``)
    before calling.

    The App path is best-effort: a lookup-or-mint failure raising
    ``httpx.HTTPError`` degrades to "App unavailable" (logged, repo + error
    only — never a token) and falls through to the fallback/anonymous
    decision, mirroring ``resolve_clone_token``'s degradation so a transient
    GitHub failure never fails a sync a fallback token could still serve. A
    malformed App private key still raises from ``build_app_jwt`` (operator
    misconfiguration must fail loudly, not be silently masked).

    ``None`` means an unauthenticated (anonymous) fetch, which the skill
    fetchers already accept — it is the only way public skill sync works on
    a deployment that never configured an operator fallback token. This is
    the one behavioral difference from ``resolve_clone_token``, which raises
    instead of returning ``None`` for its equivalent "none" tier: anonymous
    is a legitimate outcome here, not a refusal.

    See also ``resolve_clone_token``, the clone-path sibling of this
    function: the two share this tier ordering on purpose and differ only in
    their inputs and in this anonymous-versus-raise tail.

    Args:
        http_client: Injected async HTTP client. Caller owns lifecycle.
        repo_url: A raw or `owner/repo` GitHub repo reference (normalized
            internally via ``normalize_owner_repo``).
        per_agent_pat: Already-resolved per-agent PAT overlay, or None.
        proof_kind: The recorded proof THIS caller's tenant established for
            repo_url (from any of its own bindings), or None if it has never
            demonstrated access to this repo. Gates the App tier — see
            ``select_skill_sync_auth``.
        fallback_pat: Operator-wide fallback PAT, or None/empty (treated the
            same — an empty string is "no token").
        app_id: GitHub App id, or None if the App is not configured.
        app_private_key: GitHub App private key, or None if not configured.
        installation_lookup: Injected `(owner, repo) -> installation_id |
            None` callable, or None to skip the App tier entirely.
        now: Current Unix timestamp (int) — caller provides this so the App
            JWT mint stays pure (no clock read inside this module).

    Returns:
        The resolved token (PAT or minted installation token or fallback
        PAT), or None for the legitimate anonymous case.

    Raises:
        DaimonError: When ``repo_url`` does not normalize to `owner/repo` —
            a malformed reference must not silently fall through to an
            anonymous fetch of a different (or no) repo.
    """
    # Empty string is "no token" (same as fallback_pat's bool() handling below);
    # returning it verbatim would emit an empty authorization_token (MA 400s).
    if per_agent_pat:
        return per_agent_pat

    normalized = normalize_owner_repo(repo_url)
    if "/" not in normalized:
        raise DaimonError(
            f"'{repo_url}' does not resolve to a usable owner/repo reference for skill sync."
        )
    owner, repo = normalized.split("/", 1)
    has_fallback_pat = bool(fallback_pat)

    # Best-effort App path: a transient GitHub failure on the installation
    # lookup / token mint must not fail a sync a fallback token could still
    # serve. On any HTTP error, degrade to "App unavailable" and fall through
    # to the fallback/none decision. A malformed App private key still raises
    # from build_app_jwt (operator misconfig — fail loud so it is fixed, not
    # silently masked). Gated on proof_kind is not None: a repo this tenant
    # never demonstrated access to must not trigger even the lookup — see the
    # docstring's cross-tenant-probe rationale.
    app_token: str | None = None
    if (
        installation_lookup is not None
        and proof_kind is not None
        and app_id is not None
        and app_private_key is not None
    ):
        app_jwt = build_app_jwt(app_private_key.get_secret_value(), app_id, now=now)
        try:
            installation_id = await installation_lookup(owner, repo)
            if installation_id is not None:
                app_token = await mint_installation_token(
                    http_client, jwt=app_jwt, installation_id=installation_id
                )
        except httpx.HTTPError as err:
            log.warning(
                "github_repo_auth.skill_sync_app_path_failed",
                repo_url=normalized,
                error=str(err),
            )

    mode = select_skill_sync_auth(
        has_per_agent_pat=False,
        app_installed=app_token is not None,
        proof_kind=proof_kind,
        has_fallback_pat=has_fallback_pat,
    )

    if mode == "app":
        assert app_token is not None  # narrows: app_installed implies a minted token
        return app_token
    if mode == "public":
        assert fallback_pat  # narrows: has_fallback_pat implies this is truthy
        return fallback_pat
    return None
