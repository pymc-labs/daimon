"""Tests for the /slack/file/{token} proxy route and the Slack fetch helper.

Route tests use committing_sessionmaker so the seeded bot-token row (written on
its own connection) is visible to the route handler's own session — the route
opens `sessionmaker()` internally rather than sharing the test's connection.
The fetch helper is tested separately against httpx.MockTransport, with no DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.slack_file_proxy import build_slack_file_proxy_route
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.slack_file_token import mint_file_token
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

SECRET = "proxy-secret"
_FERNET_KEY = "JNTqv3cXWJ69AQIq7kf8ism3DX9JVG_Qn0xCLGY7nus="


def _app(route_handler: object) -> Starlette:
    return Starlette(routes=[Route("/slack/file/{token}", route_handler, methods=["GET"])])  # type: ignore[list-item]


@pytest.fixture
def fernet() -> MultiFernet:
    return build_multifernet((_FERNET_KEY,))


@pytest_asyncio.fixture
async def seeded_bot_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fernet: MultiFernet
) -> AsyncIterator[None]:
    """Insert an encrypted bot-token row for team "T1", committed and visible."""
    async with committing_sessionmaker() as session:
        await upsert_slack_bot_token(
            session, team_id="T1", encrypted_token=encrypt_token(fernet, "xoxb-1")
        )
        await session.commit()
    yield


@pytest.mark.asyncio
async def test_proxy_streams_bytes_for_valid_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    seeded_bot_token: None,
    fernet: MultiFernet,
) -> None:
    async def fetch_file(bot_token: str, file_id: str) -> tuple[bytes, str, str]:
        assert file_id == "F1", "route passes the token's file_id to the fetcher"
        return (b"CSVDATA", "text/csv", "data.csv")

    handler = build_slack_file_proxy_route(
        sessionmaker=committing_sessionmaker,
        fernet=fernet,
        secret=SECRET,
        fetch_file=fetch_file,
        now=lambda: 1000,
    )
    token = mint_file_token(team_id="T1", file_id="F1", exp=2000, secret=SECRET)
    transport = httpx.ASGITransport(app=_app(handler))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get(f"/slack/file/{token}")
    assert resp.status_code == 200, "valid token streams the file"
    assert resp.content == b"CSVDATA", "bytes are streamed through"
    assert resp.headers["content-type"].startswith("text/csv"), "content-type preserved"
    assert resp.headers["content-disposition"].startswith("attachment"), (
        "must force download, not inline"
    )
    assert resp.headers["x-content-type-options"] == "nosniff", "must prevent MIME sniffing"


@pytest.mark.asyncio
async def test_proxy_safe_filename_prevents_header_injection(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    seeded_bot_token: None,
    fernet: MultiFernet,
) -> None:
    """Dangerous filename with quotes and newlines cannot break the response header."""

    async def fetch_file(bot_token: str, file_id: str) -> tuple[bytes, str, str]:
        return (b"<html>evil</html>", "text/html", "'ev\"il\n.html")

    handler = build_slack_file_proxy_route(
        sessionmaker=committing_sessionmaker,
        fernet=fernet,
        secret=SECRET,
        fetch_file=fetch_file,
        now=lambda: 1000,
    )
    token = mint_file_token(team_id="T1", file_id="F1", exp=2000, secret=SECRET)
    transport = httpx.ASGITransport(app=_app(handler))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get(f"/slack/file/{token}")
    assert resp.status_code == 200, "dangerous filename does not cause an error"
    assert '"' not in resp.headers["content-disposition"], "quotes must not appear in header"
    assert resp.headers["content-disposition"].startswith("attachment"), "must force download"


@pytest.mark.asyncio
async def test_proxy_403_for_expired_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fernet: MultiFernet
) -> None:
    async def fetch_file(bot_token: str, file_id: str) -> tuple[bytes, str, str]:
        raise AssertionError("must not fetch for an invalid token")

    handler = build_slack_file_proxy_route(
        sessionmaker=committing_sessionmaker,
        fernet=fernet,
        secret=SECRET,
        fetch_file=fetch_file,
        now=lambda: 9999,
    )
    token = mint_file_token(team_id="T1", file_id="F1", exp=1000, secret=SECRET)
    transport = httpx.ASGITransport(app=_app(handler))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get(f"/slack/file/{token}")
    assert resp.status_code == 403, "expired token is rejected before any fetch"


@pytest.mark.asyncio
async def test_fetch_slack_file_authenticates_files_info_and_download() -> None:
    from daimon.adapters.mcp.slack_file_proxy import fetch_slack_file

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
    from daimon.adapters.mcp.slack_file_proxy import fetch_slack_file

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"<html>Service Unavailable</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        await fetch_slack_file(client, bot_token="xoxb-1", file_id="F1")
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_slack_file_raises_httperror_when_download_url_missing() -> None:
    """An ``ok:true`` files.info lacking url_private_download (external files) → httpx.HTTPError."""
    from daimon.adapters.mcp.slack_file_proxy import fetch_slack_file

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "file": {"mimetype": "text/csv", "name": "data.csv"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPError):
        await fetch_slack_file(client, bot_token="xoxb-1", file_id="F1")
    await client.aclose()
