"""Clone-credential resolution for App-or-PAT repo auth.

Owns the mode decision (pure) and the token-resolution orchestration + panel
coverage probe (shell, injected httpx). No DB access, no module-level
singletons — callers inject the `httpx.AsyncClient` and already-resolved
per-agent PAT.

Pure function:
  select_clone_auth — deterministic pat/app/public/none decision table.

Shell functions (injected httpx):
  resolve_clone_token — PAT short-circuit (zero GitHub I/O) -> App
    installation-token mint -> operator fallback PAT -> raise. Never returns
    an empty string.
  is_app_installed_for_repo — bind-time App-coverage probe for setup panels;
    returns False (never raises) when App creds are unset.
"""

from __future__ import annotations

from typing import Literal

import httpx
import structlog
from daimon.core.errors import DaimonError
from daimon.core.github_app_auth import (
    build_app_jwt,
    get_installation_id_for_repo,
    mint_installation_token,
)
from daimon.core.stores.domain import AgentRepoBindingRow
from pydantic import SecretStr

log = structlog.get_logger()

__all__ = ["select_clone_auth", "resolve_clone_token", "is_app_installed_for_repo"]


def select_clone_auth(
    *,
    has_per_agent_pat: bool,
    app_installed: bool,
    binding_is_public: bool,
    has_fallback_pat: bool,
) -> Literal["pat", "app", "public", "none"]:
    """Decide the clone-auth mode per the precedence table.

    Order: per-agent PAT (always wins) -> App installed on the repo
    owner -> operator fallback PAT on a verified-public (``anon:``) binding
    -> none (caller must raise; never emit an empty ``authorization_token``).

    Pure — no I/O. Callers resolve ``app_installed`` (an installation lookup)
    and ``has_fallback_pat`` before calling this.
    """
    if has_per_agent_pat:
        return "pat"
    if app_installed:
        return "app"
    if binding_is_public and has_fallback_pat:
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
    ``mint_installation_token``), falls back to the operator fallback PAT on
    a verified-public (``anon:``) binding, and raises ``DaimonError`` when
    none of those apply. Never returns an empty string (MA rejects an empty
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
    binding_is_public = binding.ma_secret_ref == "anon:"
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
        binding_is_public=binding_is_public,
        has_fallback_pat=has_fallback_pat,
    )

    if mode == "app":
        assert app_token is not None  # narrows: app_installed implies a minted token
        return app_token
    if mode == "public":
        assert fallback_pat  # narrows: has_fallback_pat implies this is truthy
        return fallback_pat
    raise DaimonError(
        f"No clone credential available for {binding.repo_url}: no per-agent PAT, "
        "the GitHub App is not installed on the repo owner (or its lookup failed), "
        "and no operator fallback PAT for a public binding."
    )


async def is_app_installed_for_repo(
    http_client: httpx.AsyncClient,
    *,
    app_id: str | None,
    app_private_key: SecretStr | None,
    owner: str,
    repo: str,
    now: int,
) -> bool:
    """Bind-time App-coverage probe for setup panels.

    Returns False (never raises) when App creds are unset, so panels on
    non-App deployments just show the PAT path.

    Args:
        http_client: Injected async HTTP client. Caller owns lifecycle.
        app_id: GitHub App id, or None if the App is not configured.
        app_private_key: GitHub App private key, or None if not configured.
        owner: Repository owner (org or user login).
        repo: Repository name (no owner prefix).
        now: Current Unix timestamp (int).

    Returns:
        True if the App is installed on the repo, False otherwise (including
        when App creds are unset).
    """
    if app_id is None or app_private_key is None:
        return False
    jwt = build_app_jwt(app_private_key.get_secret_value(), app_id, now=now)
    installation_id = await get_installation_id_for_repo(
        http_client, jwt=jwt, owner=owner, repo=repo
    )
    return installation_id is not None
