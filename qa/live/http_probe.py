"""Bounded unauthenticated GET evidence for public staging endpoints."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request

from qa.live.schema import Assertion
from qa.live.types import Pending


def check_http(assertion: Assertion) -> tuple[bool, str]:
    url = assertion.url or ""
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise Pending("HTTP checks require an unauthenticated HTTP(S) URL")
    request = urllib.request.Request(url, method="GET")
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = response.code
        content_type = response.headers.get_content_type()
        body = response.read(65537)
    if assertion.body_absent and len(body) > 65536:
        raise Pending("HTTP response exceeded bounded body observation")
    passed = (
        (assertion.expect_status is None or status == assertion.expect_status)
        and (assertion.expect_content_type is None or content_type == assertion.expect_content_type)
        and (
            assertion.body_absent is None
            or not re.search(
                assertion.body_absent, body.decode("utf-8", errors="replace"), re.MULTILINE
            )
        )
    )
    return passed, f"GET {url}; status={status}; content_type={content_type}; bytes={len(body)}"
