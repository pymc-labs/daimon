"""`/api/github/connect/*`: the connect flow's outcomes as JSON, cookies as `Set-Cookie`."""

from __future__ import annotations

from cryptography.fernet import Fernet
from daimon.adapters.web.github_connect import respond
from daimon.adapters.web.security import CSRF_HEADER
from daimon.core.config import Settings
from daimon.core.github_connect_views import (
    MAX_CONFIRM_BODY_BYTES,
    ConnectCookie,
    ConnectRedirect,
)

from .conftest import PUBLIC_URL, WebClientFactory, web_settings


def test_respond_sends_cookies_only_as_set_cookie() -> None:
    """The outcome is the body; its cookies are HttpOnly, SameSite=Lax headers, not JSON."""
    cookie = ConnectCookie(name="daimon_gh_connect", value="secret", max_age=900, secure=True)
    response = respond(ConnectRedirect(url="https://github.com/login", cookies=(cookie,)))
    assert response.status_code == 200, "every outcome is a 200"
    assert b"secret" not in response.body, "a cookie value must never reach the body"
    assert response.body == b'{"kind":"redirect","url":"https://github.com/login"}', response.body
    header = response.headers["set-cookie"].lower()
    for part in ("daimon_gh_connect=secret", "httponly", "samesite=lax", "secure", "max-age=900"):
        assert part in header, f"Set-Cookie should carry {part}: {header}"


async def test_save_refuses_an_oversized_body_as_a_page(web_client: WebClientFactory) -> None:
    """The save route is mounted without sign-in and caps its body before parsing it."""
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        github_app={  # pyright: ignore[reportArgumentType]
            "app_id": "1",
            "app_slug": "app",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
            "client_id": "c",
            "client_secret": "s",
        },
        crypto={"keys": [Fernet.generate_key().decode()]},  # pyright: ignore[reportArgumentType]
        web=web_settings(),
    )
    async with web_client(settings=settings) as client:
        response = await client.post(
            "/api/github/connect/save",
            content=b"{" + b" " * MAX_CONFIRM_BODY_BYTES + b"}",
            headers={CSRF_HEADER: "1", "origin": PUBLIC_URL, "content-type": "application/json"},
        )
    assert response.status_code == 200, response.text
    view = response.json()["view"]
    assert (view["kind"], view["status"]) == ("error", 413), view
