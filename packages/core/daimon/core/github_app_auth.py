"""GitHub App authentication primitives.

Owns App-auth crypto (pure) and the installation-token exchange (shell).
No DB access. No module-level singletons.

Pure functions:
  build_app_jwt  — RS256 JWT for App-to-GitHub auth (iss/iat/exp per GitHub docs)
  verify_signature — constant-time HMAC-SHA256 check on inbound webhook bodies

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

import httpx
import jwt


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
) -> str:
    """Exchange an App JWT for an installation access token (1h TTL).

    POSTs to https://api.github.com/app/installations/{installation_id}/access_tokens
    with the required GitHub headers. Raises httpx.HTTPStatusError on non-2xx —
    never returns a sentinel (architecture rule: no exception-to-sentinel conversion).

    Args:
        http_client: Injected async HTTP client. Caller owns lifecycle.
        jwt: Signed App JWT from build_app_jwt.
        installation_id: Numeric GitHub installation ID.

    Returns:
        The installation access token string from the JSON response.

    Raises:
        httpx.HTTPStatusError: On any non-2xx response from GitHub.
    """
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    resp = await http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    resp.raise_for_status()
    body: dict[str, object] = resp.json()
    if not isinstance(body, dict) or "token" not in body:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(
            f"GitHub installation-token response missing 'token' key "
            f"(installation_id={installation_id})"
        )
    return str(body["token"])


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
