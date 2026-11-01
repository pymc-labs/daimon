"""Security headers and the cross-site write check."""

from __future__ import annotations

import pytest
from daimon.core.config import EmbeddedAppSettings

from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}


async def test_every_response_carries_the_security_headers(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        headers = (await client.get("/healthz")).headers
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert "max-age" in headers["strict-transport-security"]
    csp = headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
    assert "frame-src 'none'" in csp, "no app configured means no frames"


async def test_only_configured_app_origins_may_be_framed(web_client: WebClientFactory) -> None:
    app = EmbeddedAppSettings(
        key="notes", name="Notes", icon="book", origin="https://notes.example.com"
    )
    async with web_client(web_settings(apps=(app,))) as client:
        csp = (await client.get("/healthz")).headers["content-security-policy"]
    assert "frame-src https://notes.example.com;" in csp


async def test_api_and_auth_responses_are_not_cached(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        api = await client.get("/api/anything")
        page = await client.get("/healthz")
    assert api.headers["cache-control"] == "no-store"
    assert "cache-control" not in page.headers


@pytest.mark.parametrize("path", ["/api/anything", "/auth/anything"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": PUBLIC_URL},
        {"X-Daimon-Web": "1"},
        {"X-Daimon-Web": "1", "Origin": "https://evil.example.net"},
        {"X-Daimon-Web": "1", "Origin": "http://web.example.com"},
        {"X-Daimon-Web": "1", "Origin": "null"},
        {"X-Daimon-Web": "yes", "Origin": PUBLIC_URL},
    ],
    ids=["bare", "no-header", "no-origin", "foreign", "wrong-scheme", "null-origin", "bad-value"],
)
async def test_writes_without_the_header_and_our_origin_are_refused(
    web_client: WebClientFactory, method: str, path: str, headers: dict[str, str]
) -> None:
    async with web_client() as client:
        res = await client.request(method, path, headers=headers)
    assert res.status_code == 403
    assert res.headers["x-frame-options"] == "DENY", "a refusal still carries the headers"


async def test_a_write_with_both_passes_the_check(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        res = await client.post("/api/anything", headers=_OWN)
    assert res.status_code != 403, "the request reached routing"


async def test_reads_and_pages_need_no_header(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        assert (await client.get("/api/anything")).status_code != 403
        assert (await client.get("/healthz")).status_code != 403


async def test_a_write_outside_api_and_auth_is_not_checked(web_client: WebClientFactory) -> None:
    async with web_client() as client:
        res = await client.post("/somewhere")
    assert res.status_code == 404, (
        "no origin check ran, and the catch-all serves pages to reads only"
    )
