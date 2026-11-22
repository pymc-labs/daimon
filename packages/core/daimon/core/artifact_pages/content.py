"""A page's HTML as served: runtime injected first, sandbox policy around it.

Page code runs in a sandboxed frame without `allow-same-origin`, so its origin is
opaque: it can never use the web session cookie, call the web API or read other pages.
Its only way out is the runtime's postMessage bridge to the shell, which the server
checks on every call. `connect-src 'none'` keeps data from leaving; libraries, fonts and
images load from `ASSET_HOSTS` only.
"""

from __future__ import annotations

import json
import re
from typing import Final

from daimon.core.artifact_pages.model import ArtifactError, Limits
from pydantic import JsonValue

ASSET_HOSTS: Final = (
    "https://fonts.googleapis.com",
    "https://fonts.gstatic.com",
    "https://cdn.jsdelivr.net",
    "https://cdnjs.cloudflare.com",
    "https://unpkg.com",
    "https://res.cloudinary.com",
)
# Never allow-same-origin (it would hand page code the session) or allow-top-navigation.
SANDBOX: Final = (
    "allow-scripts allow-forms allow-modals allow-popups "
    "allow-popups-to-escape-sandbox allow-downloads"
)
_HEAD: Final = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE: Final = re.compile(r"\s*<!doctype[^>]*>", re.IGNORECASE)


def decode_html(raw: bytes) -> str:
    if len(raw) > Limits.HTML_BYTES:
        raise ArtifactError(
            "too_large",
            f"The page is {len(raw):,} bytes; the limit is {Limits.HTML_BYTES:,}. Move large "
            "data into the page's data or images to a CDN.",
        )
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("invalid", "The page is not UTF-8 text.") from exc
    if not html.strip():
        raise ArtifactError("invalid", "The page is empty.")
    return html


def content_policy() -> str:
    hosts = " ".join(ASSET_HOSTS)
    return "; ".join(
        (
            f"sandbox {SANDBOX}",
            "default-src 'none'",
            f"script-src 'unsafe-inline' 'unsafe-eval' {hosts}",
            f"style-src 'unsafe-inline' {hosts}",
            f"font-src data: {hosts}",
            f"img-src data: blob: {hosts}",
            f"media-src data: blob: {hosts}",
            "worker-src blob:",
            "connect-src 'none'",
            "form-action 'none'",
            "frame-src 'none'",
            "frame-ancestors 'self'",
            "base-uri 'none'",
        )
    )


def shell_policy(nonce: str, content_url: str) -> str:
    """`content_url` (no query) pins the frame to this page's content: it cannot navigate
    to another page, which would inherit this shell's bridge. The web app frames shells
    to preview them, hence `frame-ancestors 'self'`."""
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
