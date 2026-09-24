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


async def test_list_installation_repositories_rejects_truncated_last_page() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"total_count": 2, "repositories": [{"full_name": "owner/repo"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="does not match total_count"):
            await list_installation_repositories(client, token="test-token")


async def test_list_installation_repositories_rejects_count_change_between_pages() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(
                200,
                headers={
                    "Link": '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'
                },
                json={"total_count": 2, "repositories": [{"full_name": "owner/repo"}]},
            )
        return httpx.Response(
            200,
            json={"total_count": 3, "repositories": [{"full_name": "owner/repo-2"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="total_count changed"):
            await list_installation_repositories(client, token="test-token")


async def test_list_installation_repositories_rejects_empty_page_with_next_link() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Link": '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'
            },
            json={"total_count": 0, "repositories": []},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="empty page with a next link"):
            await list_installation_repositories(client, token="test-token")


async def test_list_installation_repositories_rejects_repeated_next_page() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        repositories = [{"full_name": "owner/repo" if page == "1" else "owner/repo-2"}]
        return httpx.Response(
            200,
            headers={
                "Link": '<https://api.github.com/installation/repositories?per_page=100&page=2>; rel="next"'
            },
            json={"total_count": 3, "repositories": repositories},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="repeated or malformed next link"):
            await list_installation_repositories(client, token="test-token")
