"""Where sign-in may send the browser."""

from __future__ import annotations

import pytest
from daimon.adapters.web.redirects import safe_next, signin_error_url, workspaces_url


@pytest.mark.parametrize("path", ["/chat", "/chat/abc?x=1#top", "/agents/new", "/"])
def test_same_site_paths_pass_through(path: str) -> None:
    assert safe_next(path) == path


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "chat",
        "https://evil.example.net/",
        "http://evil.example.net",
        "//evil.example.net",
        "/\\evil.example.net",
        "/\\/evil.example.net",
        "javascript:alert(1)",
        "/chat\\..\\evil",
        "/chat\r\nSet-Cookie: x=1",
        "/" + "a" * 600,
    ],
)
def test_anything_that_could_leave_the_site_becomes_the_default(value: str | None) -> None:
    assert safe_next(value) == "/chat"


def test_urls_are_encoded() -> None:
    assert signin_error_url("Try & again") == "/signin?error=Try%20%26%20again"
    assert workspaces_url("/chat?a=1") == "/workspaces?next=%2Fchat%3Fa%3D1"
