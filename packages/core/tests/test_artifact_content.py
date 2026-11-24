"""Which host serves page code, and what a stored thumbnail may be."""

from __future__ import annotations

import struct
import zlib

import pytest
from daimon.core.artifact_pages.content import (
    ContentHost,
    check_thumbnail,
    content_policy,
    legacy_location,
    route_host,
)
from daimon.core.artifact_pages.model import ArtifactError

PAGE = "/a/abcdefgh23/_content"
APP = "web.example.com"


@pytest.mark.parametrize(
    ("netloc", "path", "route"),
    [
        ("abcdefgh23.pages.example.net", PAGE, "serve"),
        ("abcdefgh23.pages.example.net", "/a/zzzzzzzzzz/_content", "not_found"),
        ("abcdefgh23.pages.example.net", "/api/me", "not_found"),
        ("abcdefgh23.pages.example.net", "/a/abcdefgh23/poll", "not_found"),
        ("pages.example.net", PAGE, "redirect"),
        (APP, PAGE, "not_found"),
        (APP, "/a/abcdefgh23/poll", "serve"),
        ("old.example.org", "/a/abcdefgh23/poll", "redirect"),
        ("old.example.org", "/api/me", "serve"),
    ],
)
def test_a_per_page_content_host_serves_only_its_own_pages_content(
    netloc: str, path: str, route: str
) -> None:
    host = ContentHost.parse("https://{id}.pages.example.net")
    assert route_host(netloc, path, public_netloc=APP, content=host) == route


@pytest.mark.parametrize(
    ("netloc", "path", "route"),
    [
        ("pages.example.com", PAGE, "serve"),
        ("pages.example.com", "/a/zzzzzzzzzz/_content", "serve"),
        ("pages.example.com", "/api/me", "not_found"),
        ("pages.example.com", "/a/abcdefgh23/poll", "not_found"),
        ("abcdefgh23.pages.example.com", PAGE, "redirect"),
        (APP, PAGE, "not_found"),
    ],
)
def test_a_shared_content_host_serves_only_page_content(netloc: str, path: str, route: str) -> None:
    """Every page runs on the one host, which answers page content and nothing else."""
    host = ContentHost.parse("https://pages.example.com")
    assert host.origin("abcdefgh23") == "https://pages.example.com", "one origin for all pages"
    assert route_host(netloc, path, public_netloc=APP, content=host) == route


@pytest.mark.parametrize(
    ("netloc", "path", "route"),
    [
        ("old.example.org", "/a/q3-plan", "legacy"),
        ("old.example.org", "/a/q3-plan/", "not_found"),
        ("old.example.org", "/a/q3-plan/_content", "not_found"),
        ("old.example.org", "/api/me", "not_found"),
        ("old.example.org", "/", "not_found"),
        (APP, "/a/q3-plan", "serve"),
    ],
)
def test_a_legacy_host_answers_only_a_page_name(netloc: str, path: str, route: str) -> None:
    assert (
        route_host(netloc, path, public_netloc=APP, content=None, legacy_hosts={"old.example.org"})
        == route
    )


@pytest.mark.parametrize(
    ("query", "location"),
    [
        ("", "https://web.example.com/a/abcdefgh23/q3-plan"),
        ("key=s3cret&x=1", "https://web.example.com/a/abcdefgh23/q3-plan?k=s3cret&x=1"),
        ("a=&key=a%26b", "https://web.example.com/a/abcdefgh23/q3-plan?a=&k=a%26b"),
    ],
)
def test_a_legacy_address_moves_with_its_query_and_key_renamed(query: str, location: str) -> None:
    assert legacy_location("https://web.example.com", "abcdefgh23", "q3-plan", query) == location


def test_without_a_content_host_content_stays_on_the_app() -> None:
    assert route_host(APP, PAGE, public_netloc=APP, content=None) == "serve"
    host = ContentHost.parse("https://{id}.pages.example.net:443/")
    assert host.origin("abcdefgh23") == "https://abcdefgh23.pages.example.net"


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def test_a_thumbnail_is_a_whole_png_of_a_thumbnails_size() -> None:
    check_thumbnail(_png(640, 400))
    for bad in (b"GIF89a", _png(640, 400)[:-4], _png(4000, 400), _png(8, 8)):
        with pytest.raises(ArtifactError):
            check_thumbnail(bad)
    with pytest.raises(ArtifactError, match="200 KB"):
        check_thumbnail(_png(640, 400) + b"\0" * 200 * 1024)


def test_a_path_source_reaches_the_policy_as_the_operator_wrote_it() -> None:
    """The browser matches the prefix, so a page loads that path of the host and no other."""
    source = "https://img.example.org/acct/image/upload/"
    policy = content_policy(ancestor="https://web.example.com", asset_hosts=(source,))
    img = next(part for part in policy.split("; ") if part.startswith("img-src"))
    assert source in img.split(" "), img
    assert "https://img.example.org " not in policy + " ", "never the whole host"
