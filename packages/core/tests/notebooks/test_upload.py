"""Tests for the pure upload-URL minters."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime

import pytest
from daimon.core.config import NotebookSettings
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.notebooks.attach import InvalidAttachmentError
from daimon.core.notebooks.publish import HostNotConfiguredError, NotebookRateLimitError
from daimon.core.notebooks.slug import sanitize_slug
from daimon.core.notebooks.upload import (
    create_attachment_upload,
    create_notebook_upload,
)
from pydantic import HttpUrl, SecretStr

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _settings(*, allow_editable: bool = False) -> NotebookSettings:
    return NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=SecretStr("test-secret"),
        allow_editable=allow_editable,
    )


def _payload(upload_url: str) -> dict[str, object]:
    token = upload_url.rsplit("/upload/", 1)[1]
    payload_b64 = token.split(".", 1)[0]
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    return json.loads(raw)


def test_create_notebook_upload_permanent_namespaces_slug_and_builds_url() -> None:
    out = create_notebook_upload(
        slug="radar-plots",
        permanent=True,
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
    assert payload["op"] == "blog", "permanent=True signs the run-mode blog op into the token"
    assert payload["slug"] == out["slug"], "token slug matches the namespaced slug returned"
    assert payload["max_bytes"] == _settings().max_source_bytes, "source budget signed in"


@pytest.mark.parametrize(
    ("token", "expected_slug"),
    [
        ("A" * 22, "A" * 22),
        ("-" + "A" * 21, "x-" + "A" * 21),
    ],
    ids=["ordinary-token", "leading-dash-token"],
)
def test_create_notebook_upload_mints_random_slug_when_none(
    monkeypatch: pytest.MonkeyPatch, token: str, expected_slug: str
) -> None:
    requested_bytes: list[int] = []

    def token_urlsafe(nbytes: int) -> str:
        requested_bytes.append(nbytes)
        return token if nbytes == 16 else "fixed-nonce"

    monkeypatch.setattr("daimon.core.notebooks.publish.secrets.token_urlsafe", token_urlsafe)
    out = create_notebook_upload(
        slug=None, notebook_settings=_settings(), principal_key=None, now=_NOW
    )
    payload = _payload(out["upload_url"])

    assert 16 in requested_bytes, "the slug keeps its 128-bit random source"
    assert payload["op"] == "notebook", "notebook op signed in"
    assert out["slug"] == expected_slug, "slug uses the expected normalization"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", out["slug"]), "slug is URL-safe"
    assert payload["slug"] == out["slug"], "upload URL signs the returned slug"


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


def test_create_notebook_upload_raises_when_host_unset() -> None:
    with pytest.raises(HostNotConfiguredError):
        create_notebook_upload(
            slug="x",
            permanent=True,
            notebook_settings=NotebookSettings(host_url=None, admin_secret=None),
            principal_key="acct-1",
            now=_NOW,
        )


def test_create_notebook_upload_permanent_rate_limited() -> None:
    limiter = RateLimiter(max_requests=1)
    s = _settings()
    create_notebook_upload(
        slug="x",
        permanent=True,
        notebook_settings=s,
        principal_key="acct-1",
        now=_NOW,
        rate_limiter=limiter,
    )
    with pytest.raises(NotebookRateLimitError):
        create_notebook_upload(
            slug="y",
            permanent=True,
            notebook_settings=s,
            principal_key="acct-1",
            now=_NOW,
            rate_limiter=limiter,
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


def test_create_notebook_upload_defaults_to_the_ephemeral_op() -> None:
    """Permanence is opt-in: the cheap, reapable shape is what you get by default."""
    out = create_notebook_upload(
        slug="scratch", notebook_settings=_settings(), principal_key="acct-1", now=_NOW
    )
    assert _payload(out["upload_url"])["op"] == "notebook", (
        "omitting permanent must not mint a blog nobody asked to keep forever"
    )


def test_create_notebook_upload_editable_mints_the_editor_op() -> None:
    out = create_notebook_upload(
        slug="scratch",
        editable=True,
        notebook_settings=_settings(allow_editable=True),
        principal_key="a",
        now=_NOW,
    )
    assert _payload(out["upload_url"])["op"] == "notebook_edit", (
        "only an explicit editable=True asks the host for the code editor"
    )


def test_create_notebook_upload_scratch_default_is_not_the_editor() -> None:
    out = create_notebook_upload(
        slug="scratch", notebook_settings=_settings(), principal_key="a", now=_NOW
    )
    assert _payload(out["upload_url"])["op"] == "notebook", "the default is the read-only app"


def test_create_notebook_upload_rejects_an_editable_blog() -> None:
    with pytest.raises(ValueError, match="editable"):
        create_notebook_upload(
            slug="b",
            permanent=True,
            editable=True,
            notebook_settings=_settings(),
            principal_key="a",
            now=_NOW,
        )


def test_create_notebook_upload_editable_needs_the_operator_switch() -> None:
    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"), admin_secret=SecretStr("s")
    )
    with pytest.raises(ValueError, match="allow_editable"):
        create_notebook_upload(
            slug="x", editable=True, notebook_settings=settings, principal_key="a", now=_NOW
        )


def test_upload_tokens_carry_the_tenant() -> None:
    out = create_notebook_upload(
        slug="x", notebook_settings=_settings(), principal_key="a", now=_NOW, tenant="t-1"
    )
    assert _payload(out["upload_url"])["tenant"] == "t-1", (
        "a shared-origin notebook host admits one tenant, so it must know whose upload it is"
    )
    att = create_attachment_upload(
        slug="x",
        name="d.csv",
        notebook_settings=_settings(),
        principal_key="a",
        now=_NOW,
        tenant="t-1",
    )
    assert _payload(att["upload_url"])["tenant"] == "t-1"


def test_a_scratch_notebook_is_kept_one_day_by_default() -> None:
    out = create_notebook_upload(notebook_settings=_settings(), now=_NOW)
    assert _payload(out["upload_url"])["notebook_ttl_seconds"] == 86400, (
        "the agent's default lifetime is one day, whatever the host's own TTL"
    )


def test_a_scratch_notebook_is_kept_for_the_days_asked() -> None:
    out = create_notebook_upload(notebook_settings=_settings(), now=_NOW, ttl_days=365)
    assert _payload(out["upload_url"])["notebook_ttl_seconds"] == 365 * 86400


@pytest.mark.parametrize("ttl_days", [0, -1, 366])
def test_a_lifetime_outside_one_to_365_days_is_refused(ttl_days: int) -> None:
    with pytest.raises(ValueError, match="ttl_days"):
        create_notebook_upload(notebook_settings=_settings(), now=_NOW, ttl_days=ttl_days)


def test_a_blog_takes_no_lifetime() -> None:
    out = create_notebook_upload(
        permanent=True, notebook_settings=_settings(), principal_key="acct-1", now=_NOW
    )
    assert "notebook_ttl_seconds" not in _payload(out["upload_url"]), "a blog never expires"
    with pytest.raises(ValueError, match="ttl_days"):
        create_notebook_upload(
            permanent=True,
            notebook_settings=_settings(),
            principal_key="acct-1",
            now=_NOW,
            ttl_days=7,
        )


def test_the_editor_takes_no_lifetime() -> None:
    out = create_notebook_upload(
        editable=True, notebook_settings=_settings(allow_editable=True), now=_NOW
    )
    assert "notebook_ttl_seconds" not in _payload(out["upload_url"]), (
        "the editor runs code for whoever holds the link, so the host's TTL bounds it"
    )
    with pytest.raises(ValueError, match="ttl_days"):
        create_notebook_upload(
            editable=True, notebook_settings=_settings(allow_editable=True), now=_NOW, ttl_days=7
        )
