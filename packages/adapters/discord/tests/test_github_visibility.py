"""Tests for the anon-bind public-visibility guard (quick task 260616-45k).

Boundary mock only: a real httpx.AsyncClient over httpx.MockTransport so the
request shape and status handling are exercised end-to-end.
"""

from __future__ import annotations

import httpx
import pytest
from daimon.adapters.discord.github_visibility import is_public_repo, pat_can_access_repo


@pytest.mark.asyncio
async def test_is_public_repo_true_for_public_repo() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo", "must GET the repos endpoint"
        return httpx.Response(200, json={"private": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await is_public_repo(client, owner_repo="owner/repo")

    assert result is True, "200 with private=false must be reported public"


@pytest.mark.asyncio
async def test_is_public_repo_false_for_private_repo() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"private": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await is_public_repo(client, owner_repo="owner/private")

    assert result is False, "200 with private=true must be reported not-public"


@pytest.mark.asyncio
async def test_is_public_repo_false_for_missing_repo() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await is_public_repo(client, owner_repo="owner/ghost")

    assert result is False, "404 must be treated as not-public"


@pytest.mark.asyncio
async def test_is_public_repo_raises_on_server_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await is_public_repo(client, owner_repo="owner/repo")


@pytest.mark.asyncio
async def test_pat_can_access_repo_true_when_token_authorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/private", "must GET the repos endpoint"
        assert request.headers.get("Authorization") == "Bearer ghp_good", (
            "the PAT must be sent as a Bearer token"
        )
        return httpx.Response(200, json={"private": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await pat_can_access_repo(client, owner_repo="owner/private", pat="ghp_good")

    assert result is True, "200 with the PAT means the token can read the repo"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_pat_can_access_repo_false_when_token_lacks_access(status: int) -> None:
    """A junk/foreign PAT on a repo it can't see (GitHub returns 404 to hide
    private repos) must be reported as no-access, so the bind is rejected."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "no"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await pat_can_access_repo(client, owner_repo="owner/secret", pat="ghp_junk")

    assert result is False, f"status {status} must be reported as no-access"


@pytest.mark.asyncio
async def test_pat_can_access_repo_raises_on_server_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await pat_can_access_repo(client, owner_repo="owner/repo", pat="ghp_x")
