"""A page's HTML as served: runtime injected first, sandbox policy around it.

Page code runs in a sandboxed frame on an origin other than the app's: a content host per
page (`ContentHost`), else an opaque one. Either way it can never use the web
session cookie, call the web API or read other pages. Its only way out is the runtime's
postMessage bridge to the shell, which the server checks on every call.
`connect-src 'none'` keeps data from leaving; libraries, fonts and images load from
`ASSET_HOSTS` and the operator's own list only. What browsers still let out is in
docs/artifacts.md ("What a page cannot do").
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from daimon.core.artifact_pages.model import ArtifactError, page_url
from pydantic import JsonValue

# No host that fetches or redirects to a URL it is given (an image CDN's fetch mode):
# page code could send data out through it.
ASSET_HOSTS: Final = (
    "https://fonts.googleapis.com",
    "https://fonts.gstatic.com",
    "https://cdn.jsdelivr.net",
    "https://cdnjs.cloudflare.com",
    "https://unpkg.com",
)
# Video and document players, which need a real origin: only with a content host. Paths
# only, since a whole host may carry a fetch proxy (Drive's /viewerng/viewer?url=).
PLAYER_FRAMES: Final = (
    "https://www.youtube.com/embed/",
    "https://www.youtube-nocookie.com/embed/",
    "https://drive.google.com/file/",
)
# Never allow-top-navigation, and no popups: links and window.open go to the shell,
# which asks before opening another site. allow-same-origin only on a content host.
SANDBOX: Final = "allow-scripts allow-forms allow-modals allow-downloads"
_CONTENT_PATH: Final = re.compile(r"/a/([a-z2-7]{10})/_content")
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
HostRoute = Literal["serve", "redirect", "not_found", "legacy"]
_LEGACY_PATH: Final = re.compile(r"/a/([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)")


@dataclass(frozen=True)
class ContentHost:
    """`DAIMON_WEB__ARTIFACT_CONTENT_URL`: `https://{id}.pages.example.net`, one host per
    page, so no two pages share an origin."""

    scheme: str
    netloc: str  # lowercase host[:port], without a default port or the `{id}.` label

    @classmethod
    def parse(cls, url: str) -> ContentHost:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().removeprefix("{id}.")
        port = parts.port
        suffix = "" if port is None or port == _DEFAULT_PORTS.get(parts.scheme) else f":{port}"
        return cls(parts.scheme, host + suffix)

    def netloc_of(self, public_id: str) -> str:
        return f"{public_id}.{self.netloc}"

    def origin(self, public_id: str) -> str:
        return f"{self.scheme}://{self.netloc_of(public_id)}"

    def page_of(self, netloc: str) -> str | None:
        """For a request's host: the page's id, None when the host is not a page's."""
        label, dot, rest = netloc.partition(".")
        ok = dot and rest == self.netloc and re.fullmatch(r"[a-z2-7]{10}", label)
        return label if ok else None


def legacy_slug(path: str) -> str | None:
    """The page name in `/a/<name>`, the only path a legacy host answers."""
    found = _LEGACY_PATH.fullmatch(path)
    return found[1] if found else None


def legacy_location(public_url: str, public_id: str, slug: str, query: str) -> str:
    """Where a legacy address moves: the page's address with the old query, `key` renamed
    to `k`. A fragment needs no care: a browser keeps the one it had across a redirect."""
    pairs = [("k" if k == "key" else k, v) for k, v in parse_qsl(query, keep_blank_values=True)]
    return page_url(public_url, public_id, slug) + (f"?{urlencode(pairs)}" if pairs else "")


def route_host(
    netloc: str,
    path: str,
    *,
    public_netloc: str,
    content: ContentHost | None,
    legacy_hosts: Collection[str] = (),
) -> HostRoute:
    """Which host answers which path. The content host serves page HTML and nothing else:
    any other route there would share the page's origin. Pages linked on another of the
    app's hosts move to the public one. A legacy host answers `/a/<name>` (`legacy`) and
    nothing else. Hosts are lowercase host[:port]."""
    netloc = netloc.lower()
    if netloc in legacy_hosts:
        return "legacy" if legacy_slug(path) else "not_found"
    page = None if content is None else content.page_of(netloc)
    if page is not None:
        found = _CONTENT_PATH.fullmatch(path)
        return "serve" if found and page == found[1] else "not_found"
    if not path.startswith("/a/"):
        return "serve"
    if netloc != public_netloc:
        return "redirect"
    # With a content host, page code must never run on the app's own origin.
    return "not_found" if content is not None and _CONTENT_PATH.fullmatch(path) else "serve"


_HEAD: Final = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE: Final = re.compile(r"\s*<!doctype[^>]*>", re.IGNORECASE)


def decode_html(raw: bytes, max_bytes: int) -> str:
    if len(raw) > max_bytes:
        raise ArtifactError(
            "too_large",
            f"The page is {len(raw):,} bytes; the limit is {max_bytes:,}. Move large "
            "data into the page's data or images to a CDN.",
        )
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("invalid", "The page is not UTF-8 text.") from exc
    if not html.strip():
        raise ArtifactError("invalid", "The page is empty.")
    return html


def content_policy(
    *, ancestor: str, asset_hosts: Sequence[str] = (), content_host: bool = False
) -> str:
    """`ancestor` is the app's origin, the only one that may frame page code. A content
    host gives the page a real origin (`allow-same-origin`), which the players need."""
    hosts = " ".join((*ASSET_HOSTS, *asset_hosts))
    sandbox = f"{SANDBOX} allow-same-origin" if content_host else SANDBOX
    frames = " ".join(PLAYER_FRAMES) if content_host else "'none'"
    return "; ".join(
        (
            f"sandbox {sandbox}",
            "default-src 'none'",
            f"script-src 'unsafe-inline' 'unsafe-eval' {hosts}",
            f"style-src 'unsafe-inline' {hosts}",
            f"font-src data: {hosts}",
            f"img-src data: blob: {hosts}",
            f"media-src data: blob: {hosts}",
            "worker-src blob:",
            "connect-src 'none'",
            "form-action 'none'",
            f"frame-src {frames}",
            f"child-src {frames}",
            "webrtc 'block'",
            f"frame-ancestors {ancestor}",
            "base-uri 'none'",
        )
    )


def shell_policy(nonce: str, content_url: str) -> str:
    """`content_url` (no query) pins the frame to this page's content: it cannot navigate
    itself to another page, which would inherit this shell's bridge. The web app frames
    shells to preview them, hence `frame-ancestors 'self'`."""
    return "; ".join(
        (
            "default-src 'none'",
            f"script-src 'nonce-{nonce}'",
            f"style-src 'nonce-{nonce}'",
            "img-src 'self' data:",
            f"frame-src {content_url}",
            "connect-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'self'",
            "base-uri 'none'",
        )
    )


def inject_runtime(html: str, runtime_js: str, boot: dict[str, JsonValue]) -> str:
    """Run the runtime before any page script: first in <head>, else after the doctype.
    `boot` becomes `window.__daimon_boot`, with `<` escaped so no value closes the script."""
    encoded = json.dumps(boot, ensure_ascii=False).replace("<", "\\u003c")
    tag = f"<script>window.__daimon_boot={encoded};\n{runtime_js}</script>"
    if head := _HEAD.search(html):
        return html[: head.end()] + tag + html[head.end() :]
    if doctype := _DOCTYPE.match(html):
        return html[: doctype.end()] + tag + html[doctype.end() :]
    return tag + html


MAX_THUMBNAIL_BYTES: Final = 200 * 1024
_PNG: Final = b"\x89PNG\r\n\x1a\n"
_IEND: Final = b"IEND\xaeB`\x82"


def check_thumbnail(png: bytes) -> None:
    """A whole PNG of a thumbnail's size: the list shows it to everyone who can open the
    page, so nothing else gets stored."""
    if len(png) > MAX_THUMBNAIL_BYTES:
        raise ArtifactError("too_large", "A thumbnail is at most 200 KB.")
    whole = png.startswith(_PNG) and png.endswith(_IEND) and len(png) > 33
    if not whole or png[12:16] != b"IHDR":
        raise ArtifactError("invalid", "A thumbnail is a PNG.")
    width, height = int.from_bytes(png[16:20]), int.from_bytes(png[20:24])
    if not (16 <= width <= 1280 and 16 <= height <= 1280):
        raise ArtifactError("invalid", "A thumbnail is 16 to 1280 pixels a side.")
