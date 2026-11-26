"""Download and extract a public GitHub repository tarball."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
from daimon.core.errors import DaimonError
from daimon.core.skill_sync.bundler import extract_tarball
from daimon.core.skill_sync.fetcher import read_capped

_log = structlog.get_logger(__name__)


class GitHubFetchError(DaimonError):
    """A non-2xx response from GitHub, carrying the status for the caller.

    A ``DaimonError`` subclass so every existing ``except DaimonError``
    handler keeps catching it unchanged — this adds a discriminator, it does
    not reroute anything.

    The status matters because 404 is ambiguous on an *anonymous* fetch:
    GitHub 404s a private repo rather than 403ing it, specifically so an
    unauthenticated caller cannot probe for existence. Callers that know
    whether they sent a credential can resolve that ambiguity; this layer
    cannot, so it reports the status instead of guessing at a message.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FetchResult:
    """Result of fetch_repo: ``path`` is the repo root, ``cleanup_dir`` is what to rmtree."""

    path: Path
    cleanup_dir: Path


def _parse_github_url(url: str) -> str:
    """Extract owner/repo from a GitHub URL. Raises DaimonError for non-GitHub URLs."""
    stripped = url.removesuffix(".git").rstrip("/")
    if "github.com/" not in stripped:
        raise DaimonError(f"not a GitHub URL: {url!r}")
    return stripped.split("github.com/", 1)[1]


async def fetch_repo(
    http_client: httpx.AsyncClient,
    url: str,
    *,
    branch: str = "main",
    token: str | None = None,
    max_tarball_bytes: int = 50 * 1024 * 1024,
    max_tarball_decompressed_bytes: int = 200 * 1024 * 1024,
) -> FetchResult:
    """Download a GitHub repo tarball, extract to a temp dir, and return paths.

        Applies smart strip: if the tarball contains a single top-level wrapper
        directory (GitHub's default ``owner-repo-sha/`` wrapper), descends into it
        so callers get the actual repo root.

        Returns a ``FetchResult`` with ``path`` (the working directory after smart
        strip) and ``cleanup_dir`` (the ``mkdtemp`` root — always use this for
        ``shutil.rmtree``).

        ``max_tarball_bytes`` bounds the raw (compressed) download via a streaming
        Content-Length fast-reject plus cumulative byte-count abort
    . ``max_tarball_decompressed_bytes`` bounds the sum of tar member
        sizes, checked before ``extractall``, guarding against zip bombs.
        Both default to the safe settings-mirrored constants; set to ``0`` to
        disable either guard.

        Raises:
            DaimonError: if ``url`` is not a GitHub URL, the response is not 2xx,
                or either size cap is exceeded.
    """
    owner_repo = _parse_github_url(url)
    headers: dict[str, str] = {}
    if token is not None:
        # Private repos: the anon `github.com/.../archive/...` URL 404s. The
        # API tarball endpoint honors `Authorization: token …` (same scheme as
        # skill_sync.fetcher, proven against live GitHub by the resync path).
        tarball_url = f"https://api.github.com/repos/{owner_repo}/tarball/{branch}"
        headers["Authorization"] = f"token {token}"
    else:
        tarball_url = f"https://github.com/{owner_repo}/archive/refs/heads/{branch}.tar.gz"

    # GitHub returns 302 from both tarball endpoints redirecting to
    # `codeload.github.com/...`. httpx does not follow redirects by default;
    # without `follow_redirects=True` every sync fails with HTTP 302 here.
    async with http_client.stream(
        "GET", tarball_url, follow_redirects=True, headers=headers
    ) as response:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubFetchError(
                f"failed to fetch {url!r} (branch {branch!r}): HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc

        # `url` is quoted: this caller's cap message has always named it with repr.
        body = await read_capped(response, url=repr(url), cap=max_tarball_bytes)

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        root = await asyncio.to_thread(
            extract_tarball,
            body,
            tmp_dir,
            label=url,
            max_tarball_decompressed_bytes=max_tarball_decompressed_bytes,
            max_tarball_members=0,
        )
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    _log.info("fetch.downloaded", url=url, branch=branch)
    return FetchResult(path=root, cleanup_dir=tmp_dir)
