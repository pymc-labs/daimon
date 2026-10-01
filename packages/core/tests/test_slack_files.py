"""Tests for `daimon.core.slack_files.fetch_slack_file` (HTTP-only, no DB)."""

from __future__ import annotations

import httpx
import pytest
from daimon.core.slack_files import fetch_slack_file


@pytest.mark.asyncio
async def test_fetch_slack_file_authenticates_files_info_and_download() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer xoxb-1", "bot token on every call"
        if request.url.path.endswith("/files.info"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "file": {
                        "url_private_download": "https://files.slack.com/F1/dl",
                        "mimetype": "text/csv",
                        "name": "data.csv",
                    },
                },
            )
        return httpx.Response(200, content=b"CSVDATA")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    body, ctype, name = await fetch_slack_file(client, bot_token="xoxb-1", file_id="F1")
    await client.aclose()
    assert body == b"CSVDATA" and ctype == "text/csv" and name == "data.csv", (
        "fetcher returns bytes, content-type, and filename"
    )


@pytest.mark.asyncio
async def test_fetch_slack_file_raises_httperror_on_non_json_body() -> None:
    """A Slack gateway page (5xx, non-JSON) surfaces as httpx.HTTPError, not JSONDecodeError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"<html>Service Unavailable</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        await fetch_slack_file(client, bot_token="xoxb-1", file_id="F1")
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_slack_file_raises_httperror_when_download_url_missing() -> None:
    """An ``ok:true`` files.info lacking url_private_download (external files) → httpx.HTTPError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "file": {"mimetype": "text/csv", "name": "data.csv"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        await fetch_slack_file(client, bot_token="xoxb-1", file_id="F1")
    await client.aclose()
