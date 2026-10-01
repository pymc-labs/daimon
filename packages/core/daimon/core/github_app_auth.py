"""GitHub App authentication primitives.

Owns App-auth crypto (pure) and the installation-token exchange (shell).
No DB access. No module-level singletons.

Pure functions:
  build_app_jwt  — RS256 JWT for App-to-GitHub auth (iss/iat/exp per GitHub docs)
  verify_signature — constant-time HMAC-SHA256 check on inbound webhook bodies
  build_app_install_url — the App's install-page URL for a configured slug;
    the single place this URL is constructed, imported by both the chat tool
    and the setup panel so the two surfaces cannot state different URLs. The
    slug is validated at settings load, so this function does not re-validate.

Shell functions (injected httpx):
  mint_installation_token — POST to GitHub to exchange an App JWT for an
    installation access token; raises on non-2xx (never swallows).
  get_installation_id_for_repo — GET the App installation id for a repo;
    returns None on 404 (App not installed — a routing signal), raises on
    any other non-2xx (never swallows a real error).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import cast

import httpx
import jwt
from daimon.core.github_rate_limit import (
    GitHubRateLimitError,
    is_rate_limit_response,
    rate_limit_retry_after,
)

_APP_INSTALL_URL_TEMPLATE = "https://github.com/apps/{slug}/installations/new"


def build_app_install_url(slug: str) -> str:
    """Build the GitHub App's install-page URL for a configured slug.

    This is the single place the install URL is constructed. Both the chat
    tool and the setup panel import this function rather than each
    formatting their own copy of the URL, so the two surfaces cannot state
    different install links. The slug is validated at settings load (see
    GithubSettings.app_slug); this function does not re-validate it.

    Args:
        slug: The GitHub App's URL name (e.g. from GithubSettings.app_slug).
            Nothing else is accepted — no host, no full URL, no
            caller-supplied string — so tool input can never reach the URL.

    Returns:
        The full install-page URL for the App.
    """
    return _APP_INSTALL_URL_TEMPLATE.format(slug=slug)


def build_app_jwt(private_key_pem: str, app_id: str, *, now: int) -> str:
    """Mint an RS256 App JWT for authenticating to GitHub as the App.

    Claims follow GitHub's requirements:
      iss = app_id (the numeric App ID as a string)
      iat = now - 60  (60s back-dated for clock drift tolerance)
      exp = now + 540 (9 minutes from now; max is 10 minutes)

    Args:
        private_key_pem: PEM-encoded RSA private key (PKCS8 or traditional).
        app_id: The GitHub App's numeric ID as a string.
        now: Current Unix timestamp (int). Caller provides this so the function
            stays pure (no time.time() inside).

    Returns:
        Signed JWT string suitable for use as a Bearer token.
    """
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Verify a GitHub webhook X-Hub-Signature-256 header (constant-time).

    Uses hmac.compare_digest to avoid timing oracles (T-56-06).

    Args:
        secret: The webhook secret configured in the GitHub App settings.
        body: Raw request body bytes (before any JSON parsing).
        header: Value of the X-Hub-Signature-256 header from the request.

    Returns:
        True if the signature is valid; False otherwise.
    """
    if not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def mint_installation_token(
    http_client: httpx.AsyncClient,
    *,
    jwt: str,
    installation_id: int,
    repository: str,
    permissions: Mapping[str, str] | None = None,
) -> str:
    """Exchange an App JWT for an installation access token (1h TTL).

    POSTs to https://api.github.com/app/installations/{installation_id}/access_tokens
    with the required GitHub headers. Raises httpx.HTTPStatusError on non-2xx —
    never returns a sentinel (architecture rule: no exception-to-sentinel conversion).

    The token is always narrowed to the one ``repository`` the caller is
    authorized for. An installation typically covers many repositories and is
    created by the repo owner for their own use, while every caller here acts
    on behalf of a single binding whose recorded proof covers one repository;
    an un-narrowed token would carry access to every repository in the
    installation. ``repository`` is required so no caller can mint an
    installation-wide token by omission.

    Args:
        http_client: Injected async HTTP client. Caller owns lifecycle.
        jwt: Signed App JWT from build_app_jwt.
        installation_id: Numeric GitHub installation ID.
        repository: Repository name (no owner prefix) the token is scoped to.
        permissions: Optional permission subset (e.g. ``{"contents": "read"}``);
            None keeps the installation's granted permissions.

    Returns:
        The installation access token string from the JSON response.

    Raises:
        GitHubRateLimitError: On an explicit rate-limit response.
        httpx.HTTPStatusError: On other non-2xx responses from GitHub.
    """
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    if not repository or "/" in repository:
        raise ValueError(
            f"installation tokens must be scoped to one repository name, got {repository!r}"
        )
    request_body: dict[str, object] = {"repositories": [repository]}
    if permissions is not None:
        request_body["permissions"] = dict(permissions)
    resp = await http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=request_body,
    )
    if await is_rate_limit_response(resp):
        raise GitHubRateLimitError(rate_limit_retry_after(resp))
    resp.raise_for_status()
    body: dict[str, object] = resp.json()
    if not isinstance(body, dict) or "token" not in body:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(
            f"GitHub installation-token response missing 'token' key "
            f"(installation_id={installation_id})"
        )
    return str(body["token"])


async def mint_installation_listing_token(
    http_client: httpx.AsyncClient,
    *,
    jwt: str,
    installation_id: int,
) -> str:
    """Mint a metadata-only installation token for listing its repositories.

    Unlike ``mint_installation_token``, this deliberately does not restrict the
    token to one repository: the installation repositories endpoint must return
    the full set. The token has only metadata read permission and is used only
    by the reconciliation worker.
    """
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    response = await http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"permissions": {"metadata": "read"}},
    )
    response.raise_for_status()
    body: object = response.json()
    if not isinstance(body, dict):
        raise ValueError(
            f"GitHub installation-token response is not an object "
            f"(installation_id={installation_id})"
        )
    body_object = cast(dict[str, object], body)
    token = body_object.get("token")
    if not isinstance(token, str):
        raise ValueError(
            f"GitHub installation-token response missing string 'token' "
            f"(installation_id={installation_id})"
        )
    return token


async def get_app_installation_account(
    http_client: httpx.AsyncClient,
    *,
    jwt: str,
    installation_id: int,
) -> str | None:
    """Return the account login, or None when GitHub confirms uninstall."""
    url = f"https://api.github.com/app/installations/{installation_id}"
    response = await http_client.get(
        url,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body: object = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"GitHub installation response is not an object ({installation_id})")
    body_object = cast(dict[str, object], body)
    account = body_object.get("account")
    if not isinstance(account, dict):
        raise ValueError(
            f"GitHub installation response is missing account.login ({installation_id})"
        )
    account_object = cast(dict[str, object], account)
    login = account_object.get("login")
    if not isinstance(login, str):
        raise ValueError(
            f"GitHub installation response is missing account.login ({installation_id})"
        )
    return login


async def list_installation_repositories(
    http_client: httpx.AsyncClient,
    *,
    token: str,
) -> list[str]:
    """Fetch and validate every page of the installation repository listing."""
    url = "https://api.github.com/installation/repositories"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repos: list[str] = []
    seen: set[str] = set()
    seen_next_links: set[str] = set()
    total_count: int | None = None
    page = 1
    while True:
        response = await http_client.get(
            url, headers=headers, params={"per_page": 100, "page": page}
        )
        response.raise_for_status()
        body: object = response.json()
        if not isinstance(body, dict):
            raise ValueError("GitHub installation-repositories response is malformed")
        body_object = cast(dict[str, object], body)
        page_total = body_object.get("total_count")
        if not isinstance(page_total, int) or isinstance(page_total, bool) or page_total < 0:
            raise ValueError("GitHub installation-repositories response has invalid total_count")
        if total_count is None:
            total_count = page_total
        elif page_total != total_count:
            raise ValueError("GitHub installation-repositories total_count changed between pages")
        repository_list = body_object.get("repositories")
        if not isinstance(repository_list, list):
            raise ValueError("GitHub installation-repositories response is malformed")
        seen_before_page = len(seen)
        for repository in cast(list[object], repository_list):
            if not isinstance(repository, dict):
                raise ValueError("GitHub installation-repositories page has a malformed repository")
            repository_object = cast(dict[str, object], repository)
            full_name = repository_object.get("full_name")
            if not isinstance(full_name, str):
                raise ValueError("GitHub installation-repositories page has a malformed repository")
            if not full_name or full_name.count("/") != 1:
                raise ValueError("GitHub installation-repositories page has an invalid full_name")
            if full_name not in seen:
                repos.append(full_name)
                seen.add(full_name)
        if len(seen) > total_count:
            raise ValueError(
                "GitHub installation-repositories returned more repositories than total_count"
            )
        if "next" not in response.links:
            if len(seen) != total_count:
                raise ValueError(
                    "GitHub installation-repositories page count does not match total_count"
                )
            return repos
        if not repository_list:
            raise ValueError(
                "GitHub installation-repositories returned an empty page with a next link"
            )
        if len(seen) == seen_before_page or len(seen) >= total_count:
            raise ValueError(
                "GitHub installation-repositories page cannot make progress toward total_count"
            )
        next_url = response.links["next"].get("url")
        if not isinstance(next_url, str) or next_url in seen_next_links:
            raise ValueError("GitHub installation-repositories repeated or malformed next link")
        seen_next_links.add(next_url)
        page += 1


async def get_installation_id_for_repo(
    http_client: httpx.AsyncClient,
    *,
    jwt: str,
    owner: str,
    repo: str,
) -> int | None:
    """Resolve the App installation id for a repo (on-demand lookup).

    GETs https://api.github.com/repos/{owner}/{repo}/installation with the
    App JWT (not an installation token) as bearer auth.

    Returns None when the App is not installed on the repo (404) — a
    routing signal, not an error. Raises httpx.HTTPStatusError on any other
    non-2xx response — never returns a sentinel for a real error
    (architecture rule: no exception-to-sentinel conversion).

    Args:
        http_client: Injected async HTTP client. Caller owns lifecycle.
        jwt: Signed App JWT from build_app_jwt.
        owner: Repository owner (org or user login).
        repo: Repository name (no owner prefix).

    Returns:
        The installation id, or None if the App is not installed on the repo.

    Raises:
        httpx.HTTPStatusError: On any non-2xx response other than 404.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/installation"
    resp = await http_client.get(
        url,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    body: dict[str, object] = resp.json()
    return int(body["id"])  # pyright: ignore[reportArgumentType]
