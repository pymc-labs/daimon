"""Tests for notebook_host.lifecycle.safe_attachment_name and the
max_attachment_bytes_ceiling setting.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException


@pytest.mark.parametrize(
    "name",
    [
        "hello.csv",
        "_x",
        "a.b-c_d",
        "x" * 64,  # boundary — 64 chars allowed
        "data.txt",
        "A1_b2.c3-d4",
    ],
)
def test_safe_attachment_name_accepts_valid(name: str) -> None:
    """safe_attachment_name returns the name unchanged for valid inputs."""
    from notebook_host.lifecycle import safe_attachment_name

    assert safe_attachment_name(name) == name, (
        f"valid attachment name {name!r} should pass through unchanged"
    )


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "-leading",  # leading hyphen forbidden (argv-style attacks)
        ".leading",  # leading dot forbidden (hidden files)
        "has space",  # whitespace forbidden
        "has/slash",  # path separator forbidden
        "../etc/passwd",  # traversal
        "x" * 65,  # >64 chars
        "name\x00null",  # NUL byte
        "name\nnewline",  # newline
    ],
)
def test_safe_attachment_name_rejects_invalid(name: str) -> None:
    """safe_attachment_name raises HTTPException(400) for invalid inputs."""
    from notebook_host.lifecycle import safe_attachment_name

    with pytest.raises(HTTPException) as exc_info:
        safe_attachment_name(name)
    assert exc_info.value.status_code == 400, (
        f"invalid attachment name {name!r} should yield HTTP 400"
    )


def test_settings_max_attachment_bytes_ceiling_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_attachment_bytes_ceiling defaults to 100 MiB."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "x")
    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", "/tmp/x")
    from notebook_host.config import Settings

    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.max_attachment_bytes_ceiling == 100 * 1024 * 1024, (
        "max_attachment_bytes_ceiling default should be 100 MiB"
    )


def test_settings_max_attachment_bytes_ceiling_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES_CEILING overrides the default."""
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "x")
    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", "/tmp/x")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES_CEILING", "1024")
    from notebook_host.config import Settings

    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.max_attachment_bytes_ceiling == 1024, (
        "env DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES_CEILING should override the default"
    )
