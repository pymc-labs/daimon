"""Filename derivation for chat attachments."""

from __future__ import annotations

import pytest
from daimon.core.media.filenames import display_filename_for, extension_for_mime, sanitize_title


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("application/pdf", ".pdf"),
        ("application/octet-stream", ".bin"),
    ],
)
def test_extension_for_mime_maps_known_types(mime_type: str, expected: str) -> None:
    assert extension_for_mime(mime_type) == expected, f"{mime_type} should resolve to {expected}"


def test_sanitize_title_strips_path_separators() -> None:
    assert sanitize_title("../../etc/passwd") == "etc_passwd", (
        "a title must not be able to carry a path separator"
    )


def test_display_filename_appends_extension_when_title_has_none() -> None:
    assert display_filename_for("chart", "image/png") == "chart.png", (
        "a bare title should gain the extension its mime implies"
    )


def test_display_filename_does_not_double_a_matching_extension() -> None:
    assert display_filename_for("chart.png", "image/png") == "chart.png", (
        "an agent that already supplied the right extension must not get chart.png.png"
    )


@pytest.mark.parametrize(
    ("title", "mime_type"),
    [
        # The data-analysis skills' own artifact formats. mimetypes knows neither
        # .jsonl nor .parquet, and an agent has no better mime to declare.
        ("changelog.jsonl", "application/json"),
        ("findings.jsonl", "application/octet-stream"),
        ("orders.parquet", "application/octet-stream"),
        ("clean.parquet", "binary/octet-stream"),
        ("notes.md", "text/plain"),
        ("clean.sql", "application/octet-stream"),
        ("changelog.jsonl", "application/json; charset=utf-8"),
        # Types mimetypes has never heard of. Agents invent these -- every one
        # below was observed on staging or is its obvious sibling -- and an
        # invented type is no more informative than octet-stream.
        ("changelog.jsonl", "application/jsonl"),
        ("findings.jsonl", "application/x-ndjson"),
        ("orders.parquet", "application/vnd.apache.parquet"),
    ],
)
def test_display_filename_keeps_the_title_extension_when_the_mime_is_uninformative(
    title: str, mime_type: str
) -> None:
    assert display_filename_for(title, mime_type) == title, (
        "a generic mime carries no format information, so the title's own "
        "extension is the more specific fact and must survive"
    )


def test_display_filename_lets_a_specific_mime_override_a_lying_title() -> None:
    assert display_filename_for("report.pdf", "image/png") == "report.pdf.png", (
        "deriving from a SPECIFIC mime is the whole point: a PNG must not "
        "preview as a PDF because the agent titled it one"
    )


def test_display_filename_still_falls_back_for_a_generic_mime_and_no_extension() -> None:
    assert display_filename_for("dump", "application/octet-stream") == "dump.bin", (
        "with neither a usable mime nor a title extension there is nothing to "
        "preserve, so the opaque fallback still applies"
    )
