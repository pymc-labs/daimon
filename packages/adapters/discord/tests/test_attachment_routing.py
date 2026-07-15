"""Tests for ``build_attachment_url_prefix`` (data-attachment URL surfacing)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import discord
from daimon.adapters.discord.attachments import build_attachment_url_prefix


@dataclass
class FakeAttachment:
    """Minimal Discord Attachment double — the fields the prefix builder reads."""

    filename: str
    size: int
    url: str


def _as_attachment(fake: object) -> discord.Attachment:
    return cast(discord.Attachment, fake)


def test_empty_attachments_returns_empty_string() -> None:
    assert build_attachment_url_prefix([]) == "", "no attachments → no prefix"


def test_single_attachment_surfaces_name_size_and_url() -> None:
    att = FakeAttachment(filename="report.csv", size=2048, url="https://cdn.discord/r.csv?hm=ab")
    line = build_attachment_url_prefix([_as_attachment(att)])
    assert "`report.csv`" in line, "filename must appear"
    assert "2048 bytes" in line, "size must appear"
    assert "https://cdn.discord/r.csv?hm=ab" in line, "the signed CDN URL must appear verbatim"
    assert "create_attachment_upload_url" in line, "agent must be told the on-demand upload path"


def test_multiple_attachments_one_line_each() -> None:
    atts = [
        _as_attachment(FakeAttachment(filename="a.csv", size=1, url="https://cdn/a")),
        _as_attachment(FakeAttachment(filename="b.pdf", size=2, url="https://cdn/b")),
    ]
    prefix = build_attachment_url_prefix(atts)
    assert prefix.count("\n") == 1, "two attachments → two lines joined by one newline"
    assert "`a.csv`" in prefix and "`b.pdf`" in prefix, "every attachment must be surfaced"
