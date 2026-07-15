"""PAT-authenticated GitHub tarball fetcher.

Architectural rule: this module does NOT catch exceptions. Errors raise; the
orchestrator (named boundary) handles them. The four error CLASSES below are
the typed shapes the orchestrator pattern-matches.
"""

from __future__ import annotations

import io

import httpx
import structlog

_log = structlog.get_logger(__name__)


class PATMissingError(Exception):
    """No PAT available for the principal (or per-agent overlay)."""


class GitHubAuthError(Exception):
    """401/403 from GitHub — bad PAT or insufficient scopes."""


class GitHubUnreachable(Exception):
    """404 from GitHub — repo or branch does not exist (or PAT cannot see it)."""


class TarballTooLarge(Exception):
    """Tarball download exceeded the configured raw byte cap (Content-Length or
    cumulative streamed body). Distinct from GitHubUnreachable (404) so the
    orchestrator's skipped_repos entries carry an accurate reason (RESEARCH
    Open Question 3)."""


class RepoCollisionError(Exception):
    """Two repos in the same agent produced the same sanitized skill name."""


def _normalize_owner_repo(url: str) -> str:
    """Extract `owner/repo` from a URL or short-form path.

    Accepts: 'https://github.com/owner/repo', 'github.com/owner/repo',
    'owner/repo', any with a trailing '/' or '.git'.
    """
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


class GitHubTarballFetcher:
    """Fetch a GitHub repo tarball authenticated with a PAT.

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

    async def fetch_tarball(self, *, pat: str | None, url: str, branch: str) -> bytes:
        owner_repo = _normalize_owner_repo(url)
        api = f"https://api.github.com/repos/{owner_repo}/tarball/{branch}"
        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if pat is not None:
            headers["Authorization"] = f"token {pat}"
        cap = self._max_tarball_bytes
        async with self._http.stream(
            "GET",
            api,
            headers=headers,
            follow_redirects=True,
        ) as resp:
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
