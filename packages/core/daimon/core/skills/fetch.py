"""Download and extract a public GitHub repository tarball."""

from __future__ import annotations

import io
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
from daimon.core.errors import DaimonError

_log = structlog.get_logger(__name__)


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


def _smart_strip(extract_root: Path) -> Path:
    """If extract_root has exactly one subdir and no root SKILL.md, descend into it."""
    entries = list(extract_root.iterdir())
    dirs = [e for e in entries if e.is_dir()]
    has_root_skill = (extract_root / "SKILL.md").exists()
    if len(dirs) == 1 and not has_root_skill:
        return dirs[0]
    return extract_root


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
            raise DaimonError(
                f"failed to fetch {url!r} (branch {branch!r}): HTTP {exc.response.status_code}"
            ) from exc

        if max_tarball_bytes > 0:
            content_length = response.headers.get("content-length")
            if content_length is not None and int(content_length) > max_tarball_bytes:
                raise DaimonError(
                    f"tarball for {url!r} exceeds max_tarball_bytes cap of "
                    f"{max_tarball_bytes} bytes (Content-Length={content_length})"
                )

        body = io.BytesIO()
        async for chunk in response.aiter_bytes():
            if max_tarball_bytes > 0 and body.tell() + len(chunk) > max_tarball_bytes:
                raise DaimonError(
                    f"tarball for {url!r} exceeds max_tarball_bytes cap of "
                    f"{max_tarball_bytes} bytes (streamed body)"
                )
            body.write(chunk)

    body_bytes = body.getvalue()

    with tarfile.open(fileobj=io.BytesIO(body_bytes), mode="r:gz") as tf:
        members = tf.getmembers()
        total = sum(m.size for m in members)
        if max_tarball_decompressed_bytes > 0 and total > max_tarball_decompressed_bytes:
            raise DaimonError(
                f"tarball for {url!r} decompresses to {total} bytes, exceeding "
                f"max_tarball_decompressed_bytes cap of {max_tarball_decompressed_bytes} bytes"
            )
        tmp_dir = Path(tempfile.mkdtemp())
        tf.extractall(tmp_dir, filter="data")

    _log.info("fetch.downloaded", url=url, branch=branch)

    return FetchResult(path=_smart_strip(tmp_dir), cleanup_dir=tmp_dir)
