"""Transport tests for installation repository pagination and payload validation."""

from __future__ import annotations

import httpx
import pytest
from daimon.core.github_app_auth import list_installation_repositories


async def test_list_installation_repositories_fetches_every_page() -> None:
    first_page = [f"owner/repo-{index}" for index in range(100)]
    second_page = ["owner/repo-100", "owner/repo-101"]
    requested_pages: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        requested_pages.append(page)
        assert request.url.params["per_page"] == "100", "the endpoint should use the max page size"
        if page == "1":
            return httpx.Response(
                200,
                headers={
                    "Link": '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'
                },
                json={
                    "total_count": 102,
                    "repositories": [{"full_name": name} for name in first_page],
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 102,
                "repositories": [{"full_name": name} for name in second_page],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repositories = await list_installation_repositories(client, token="test-token")

    assert requested_pages == ["1", "2"], "a next-page link must cause a second request"
    assert repositories == first_page + second_page, (
        "all repository names should be retained in order"
    )


async def test_list_installation_repositories_does_not_return_partial_pages() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                headers={
                    "Link": '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'
                },
                json={
                    "total_count": 101,
                    "repositories": [{"full_name": f"owner/repo-{i}"} for i in range(100)],
                },
            )
        return httpx.Response(503, json={"message": "temporarily unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError, match="503"):
            await list_installation_repositories(client, token="test-token")
