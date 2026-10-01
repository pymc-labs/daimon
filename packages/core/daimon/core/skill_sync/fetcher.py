"""GitHub tarball fetcher, authenticated with an optional bearer credential.

The credential may be a personal access token or a GitHub App installation
token — both are opaque bearer strings from this module's perspective; the
caller (the orchestrator) resolves which kind to hand in.

Architectural rule: this module does NOT catch exceptions. Errors raise; the
orchestrator (named boundary) handles them. The four error CLASSES below are
the typed shapes the orchestrator pattern-matches.
"""

from __future__ import annotations

import io

import httpx
import structlog
from daimon.core.github_rate_limit import (
    GitHubRateLimitError,
    is_rate_limit_response,
    rate_limit_retry_after,
)
from daimon.core.github_repo_auth import normalize_owner_repo

_log = structlog.get_logger(__name__)


class PATMissingError(Exception):
    """No credential was available for the principal (or per-agent overlay)."""


class GitHubAuthError(Exception):
    """401/403 from GitHub — bad credential or insufficient scopes."""


class GitHubUnreachable(Exception):
    """404 from GitHub — repo or branch does not exist (or the credential cannot see it)."""


class TarballTooLarge(Exception):
    """Tarball download exceeded the configured raw byte cap (Content-Length or
    cumulative streamed body). Distinct from GitHubUnreachable (404) so the
    orchestrator's skipped_repos entries carry an accurate reason (RESEARCH
    Open Question 3)."""


class GitHubTarballFetcher:
    """Fetch a GitHub repo tarball authenticated with an optional bearer credential.

    DI: takes an injected `httpx.AsyncClient`. Does NOT construct one per call
    (per architecture rule §"Prefer Pure Functions and Dependency Injection").
    Caller owns the client's lifecycle and timeout configuration.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        max_tarball_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._http = http_client
        self._max_tarball_bytes = max_tarball_bytes

    async def fetch_tarball(self, *, credential: str | None, url: str, branch: str) -> bytes:
        """Download the tarball for ``url`` at ``branch``.

        ``credential`` is the resolved bearer token (personal access token or
        GitHub App installation token) to send as ``Authorization: token
        <credential>``. ``None`` means an unauthenticated fetch — how public
        repos sync when no credential resolves.
        """
        owner_repo = normalize_owner_repo(url)
        api = f"https://api.github.com/repos/{owner_repo}/tarball/{branch}"
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if credential is not None:
            headers["Authorization"] = f"token {credential}"
        cap = self._max_tarball_bytes
        async with self._http.stream(
            "GET",
            api,
            headers=headers,
            follow_redirects=True,
        ) as resp:
            if await is_rate_limit_response(resp):
                retry_after = rate_limit_retry_after(resp)
                _log.warning(
                    "skill_sync.fetcher.rate_limited",
                    url=url,
                    status=resp.status_code,
                    retry_after=retry_after.isoformat(),
                )
                raise GitHubRateLimitError(retry_after)
            if resp.status_code in (401, 403):
                _log.warning("skill_sync.fetcher.auth_error", url=url, status=resp.status_code)
                raise GitHubAuthError(url)
            if resp.status_code == 404:
                _log.warning("skill_sync.fetcher.unreachable", url=url, branch=branch)
                raise GitHubUnreachable(url)
            resp.raise_for_status()

            if cap > 0:
                content_length = resp.headers.get("content-length")
                if content_length is not None and int(content_length) > cap:
                    _log.warning(
                        "skill_sync.fetcher.tarball_too_large",
                        url=url,
                        cap=cap,
                        content_length=content_length,
                    )
                    raise TarballTooLarge(
                        f"tarball for {url} exceeds max_tarball_bytes cap of {cap} bytes "
                        f"(Content-Length={content_length})"
                    )

            buf = io.BytesIO()
            async for chunk in resp.aiter_bytes():
                if cap > 0 and buf.tell() + len(chunk) > cap:
                    _log.warning("skill_sync.fetcher.tarball_too_large", url=url, cap=cap)
                    raise TarballTooLarge(
                        f"tarball for {url} exceeds max_tarball_bytes cap of {cap} bytes "
                        "(streamed body)"
                    )
                buf.write(chunk)
            return buf.getvalue()
