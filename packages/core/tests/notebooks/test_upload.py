"""Tests for the pure upload-URL minters."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from daimon.core.config import NotebookSettings
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.notebooks.attach import InvalidAttachmentError
from daimon.core.notebooks.publish import HostNotConfiguredError, NotebookRateLimitError
from daimon.core.notebooks.slug import sanitize_slug
from daimon.core.notebooks.upload import (
    create_attachment_upload,
    create_blog_upload,
    create_notebook_upload,
)
from pydantic import HttpUrl, SecretStr

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _settings() -> NotebookSettings:
    return NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=SecretStr("test-secret"),
    )


def _payload(upload_url: str) -> dict[str, object]:
    token = upload_url.rsplit("/upload/", 1)[1]
    payload_b64 = token.split(".", 1)[0]
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    return json.loads(raw)


def test_create_blog_upload_namespaces_slug_and_builds_url() -> None:
    out = create_blog_upload(
        slug="radar-plots",
        notebook_settings=_settings(),
        principal_key="acct-1",
        now=_NOW,
    )
    assert out["upload_url"].startswith("http://notebook-host:8001/upload/"), (
        "URL points at the host upload route"
    )
    assert out["slug"].endswith(f"-{sanitize_slug('radar-plots')}"), "slug is principal-prefixed"
    assert out["upload_expires_at"] == "2026-06-09T12:05:00+00:00", "expiry is now + 300s, ISO-8601"
    payload = _payload(out["upload_url"])
    assert payload["op"] == "blog", "blog op signed into the token"
    assert payload["slug"] == out["slug"], "token slug matches the namespaced slug returned"
    assert payload["max_bytes"] == _settings().max_source_bytes, "source budget signed in"


def test_create_notebook_upload_mints_random_slug_when_none() -> None:
    out = create_notebook_upload(
        slug=None, notebook_settings=_settings(), principal_key=None, now=_NOW
    )
    payload = _payload(out["upload_url"])
    assert payload["op"] == "notebook", "notebook op signed in"
    import re

    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", out["slug"]), (
        "slug is a fresh 128-bit url-safe token (16 bytes → 22 chars)"
    )


def test_create_attachment_upload_signs_name_and_data_op() -> None:
    out = create_attachment_upload(
        slug="my-blog",
        name="posterior.nc",
        notebook_settings=_settings(),
        principal_key="acct-1",
        now=_NOW,
    )
    payload = _payload(out["upload_url"])
    assert payload["op"] == "data", "data op signed in"
    assert payload["name"] == "posterior.nc", "attachment name signed in"
    assert payload["max_bytes"] == _settings().max_attachment_bytes, "attachment budget signed in"


def test_create_attachment_upload_rejects_unsafe_name() -> None:
    with pytest.raises(InvalidAttachmentError):
        create_attachment_upload(
            slug="my-blog",
            name="../etc/passwd",
            notebook_settings=_settings(),
            principal_key="acct-1",
            now=_NOW,
        )


def test_create_blog_upload_raises_when_host_unset() -> None:
    with pytest.raises(HostNotConfiguredError):
        create_blog_upload(
            slug="x",
            notebook_settings=NotebookSettings(host_url=None, admin_secret=None),
            principal_key="acct-1",
            now=_NOW,
        )


def test_create_blog_upload_rate_limited() -> None:
    limiter = RateLimiter(max_requests=1)
    s = _settings()
    create_blog_upload(
        slug="x", notebook_settings=s, principal_key="acct-1", now=_NOW, rate_limiter=limiter
    )
    with pytest.raises(NotebookRateLimitError):
        create_blog_upload(
            slug="y", notebook_settings=s, principal_key="acct-1", now=_NOW, rate_limiter=limiter
        )


def test_create_notebook_upload_namespaces_slug_when_provided() -> None:
    out = create_notebook_upload(
        slug="scratch", notebook_settings=_settings(), principal_key="acct-1", now=_NOW
    )
    assert out["slug"].endswith(f"-{sanitize_slug('scratch')}"), (
        "provided slug is principal-prefixed"
    )
    assert _payload(out["upload_url"])["op"] == "notebook", "op stays notebook"


def test_create_attachment_upload_rate_limited() -> None:
    limiter = RateLimiter(max_requests=1)
    s = _settings()
    create_attachment_upload(
        slug="b",
        name="a.nc",
        notebook_settings=s,
        principal_key="acct-1",
        now=_NOW,
        rate_limiter=limiter,
    )
    with pytest.raises(NotebookRateLimitError):
        create_attachment_upload(
            slug="b",
            name="b.nc",
            notebook_settings=s,
            principal_key="acct-1",
            now=_NOW,
            rate_limiter=limiter,
        )
