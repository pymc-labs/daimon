"""Transport-level tests for daimon.core.skill_sync.fetcher.

All tests use httpx.MockTransport — no real network, no method-level mocks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from daimon.core.skill_sync.fetcher import (
    GitHubAuthError,
    GitHubTarballFetcher,
    GitHubUnreachable,
    TarballTooLarge,
)


async def test_fetch_tarball_returns_bytes_on_200() -> None:
    """200 response yields the raw response body bytes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<tarball>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    result = await fetcher.fetch_tarball(pat="t", url="https://github.com/o/r", branch="main")
    assert result == b"<tarball>", "fetcher should return raw response bytes on 200"


async def test_fetch_tarball_sends_authorization_header() -> None:
    """Request carries `Authorization: token <pat>` and GitHub Accept header."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    await fetcher.fetch_tarball(pat="my-pat", url="o/r", branch="main")
    assert len(captured) == 1, "exactly one request should have been sent"
    assert captured[0].headers["Authorization"] == "token my-pat", (
        "Authorization header must use `token <pat>` scheme"
    )
    assert captured[0].headers["Accept"] == "application/vnd.github+json", (
        "Accept header must request the GitHub JSON media type"
    )


async def test_fetch_tarball_uses_correct_url() -> None:
    """URL is the GitHub REST tarball endpoint with owner/repo and branch."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    await fetcher.fetch_tarball(pat="t", url="https://github.com/o/r", branch="main")
    assert str(captured[0].url) == "https://api.github.com/repos/o/r/tarball/main", (
        "URL must target api.github.com/repos/{owner}/{repo}/tarball/{branch}"
    )


async def test_fetch_tarball_omits_authorization_when_pat_is_none() -> None:
    """When pat is None, send the request unauthenticated — public repos work without a PAT.

    This is the panel-path fix for #40 follow-up: the chat path uses unauthenticated fetch
    via daimon.core.skills.fetch (no PAT required for public repos). The panel path was
    PAT-only and silently failed for any user without a PAT row in github_credentials.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"<public-tarball>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    result = await fetcher.fetch_tarball(pat=None, url="o/r", branch="main")
    assert result == b"<public-tarball>", "unauthenticated fetch should succeed on public repos"
    assert len(captured) == 1, "exactly one request"
    assert "Authorization" not in captured[0].headers, (
        "no PAT means no Authorization header — sending an empty token would 401"
    )
    assert captured[0].headers["Accept"] == "application/vnd.github+json", (
        "Accept header still required even without auth"
    )


async def test_fetch_tarball_raises_unreachable_on_404_unauth() -> None:
    """Without a PAT, GitHub returns 404 for private or non-existent repos.

    Caller can interpret this as 'public repo not found OR private repo requires PAT';
    either way the fetcher's contract is to raise GitHubUnreachable for 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    with pytest.raises(GitHubUnreachable):
        await fetcher.fetch_tarball(pat=None, url="o/private-repo", branch="main")


async def test_fetch_tarball_normalizes_url_with_https_prefix() -> None:
    """`https://github.com/o/r` is normalized to `o/r` in the API URL."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    await fetcher.fetch_tarball(pat="t", url="https://github.com/o/r", branch="main")
    assert "/repos/o/r/tarball/" in str(captured[0].url), (
        "https:// prefix must be stripped before constructing API URL"
    )


async def test_fetch_tarball_normalizes_url_with_git_suffix() -> None:
    """`https://github.com/o/r.git` is normalized — `.git` suffix is removed."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    await fetcher.fetch_tarball(pat="t", url="https://github.com/o/r.git", branch="main")
    assert "/repos/o/r/tarball/" in str(captured[0].url), (
        ".git suffix must be stripped before constructing API URL"
    )


async def test_fetch_tarball_normalizes_url_with_short_form() -> None:
    """Short-form `o/r` (no scheme/host) is accepted as-is."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")
    assert "/repos/o/r/tarball/" in str(captured[0].url), (
        "short-form owner/repo must produce a valid API URL"
    )


async def test_fetch_tarball_raises_GitHubAuthError_on_401() -> None:
    """401 response maps to GitHubAuthError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    with pytest.raises(GitHubAuthError):
        await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")


async def test_fetch_tarball_raises_GitHubAuthError_on_403() -> None:
    """403 response maps to GitHubAuthError (insufficient scopes)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    with pytest.raises(GitHubAuthError):
        await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")


async def test_fetch_tarball_raises_GitHubUnreachable_on_404() -> None:
    """404 response maps to GitHubUnreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    with pytest.raises(GitHubUnreachable):
        await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")


async def test_fetch_tarball_propagates_5xx_as_HTTPStatusError() -> None:
    """5xx responses propagate raw — orchestrator handles at the boundary."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    with pytest.raises(httpx.HTTPStatusError):
        await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")


async def test_fetch_tarball_follows_redirect() -> None:
    """A 302 redirect is followed and the final 200 body is returned."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if "tarball/main" in str(request.url):
            return httpx.Response(
                302,
                headers={"Location": "https://codeload.github.com/o/r/legacy.tar.gz/main"},
            )
        return httpx.Response(200, content=b"<final>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client)
    result = await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")
    assert result == b"<final>", "fetcher must follow the redirect and return final bytes"
    assert call_count["n"] == 2, "exactly two requests should have been made (initial + redirect)"


# --- streaming size guard tests ---


async def test_fetch_tarball_raises_tarball_too_large_on_content_length_header() -> None:
    """A Content-Length header exceeding the cap is rejected before the body is read."""
    consumed: list[bool] = []

    async def body_gen() -> AsyncIterator[bytes]:
        consumed.append(True)
        yield b"x" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "1000000"},
            content=body_gen(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client, max_tarball_bytes=100)
    with pytest.raises(TarballTooLarge):
        await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")
    assert consumed == [], (
        "body iterator must never be consumed when Content-Length alone exceeds the cap"
    )


async def test_fetch_tarball_raises_tarball_too_large_mid_stream_without_header() -> None:
    """No (or lying) Content-Length: the cumulative streamed byte count still rejects."""

    def handler(request: httpx.Request) -> httpx.Response:
        # No content-length header at all — codeload.github.com sends chunked bodies.
        return httpx.Response(200, content=b"x" * 1000)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client, max_tarball_bytes=100)
    with pytest.raises(TarballTooLarge):
        await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")


async def test_fetch_tarball_under_cap_returns_bytes_unchanged() -> None:
    """A tarball under the cap fetches and returns bytes exactly as before."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<small-tarball>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client, max_tarball_bytes=1024)
    result = await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")
    assert result == b"<small-tarball>", "under-cap tarball must return unchanged bytes"


async def test_fetch_tarball_max_tarball_bytes_zero_disables_guard() -> None:
    """max_tarball_bytes=0 fully disables the size guard — large bodies pass through."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "1000000"}, content=b"x" * 1000)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = GitHubTarballFetcher(client, max_tarball_bytes=0)
    result = await fetcher.fetch_tarball(pat="t", url="o/r", branch="main")
    assert result == b"x" * 1000, "max_tarball_bytes=0 must disable the guard entirely"
