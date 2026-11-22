"""Which host serves page code, and what a stored thumbnail may be."""

from __future__ import annotations

import struct
import zlib

import pytest
from daimon.core.artifact_pages.content import ContentHost, check_thumbnail, route_host
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


def test_without_a_content_host_content_stays_on_the_app() -> None:
    assert route_host(APP, PAGE, public_netloc=APP, content=None) == "serve"
    single = ContentHost.parse("https://pages.example.net:443/")
    assert single.origin("abcdefgh23") == "https://pages.example.net"
    assert route_host("pages.example.net", PAGE, public_netloc=APP, content=single) == "serve"


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
