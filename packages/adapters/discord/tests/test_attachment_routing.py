"""Tests for ``build_attachment_url_prefix`` (data-attachment URL surfacing)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.attachments import build_attachment_url_prefix, prepare_attachments
from daimon.adapters.discord.vision import MAX_VISION_IMAGE_BYTES


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
    assert line.startswith("[attachment]") and "*system:" not in line, (
        "line is adapter metadata in the user turn; impersonating a system message reads "
        "as prompt injection and gets refused"
    )


def test_multiple_attachments_one_line_each() -> None:
    atts = [
        _as_attachment(FakeAttachment(filename="a.csv", size=1, url="https://cdn/a")),
        _as_attachment(FakeAttachment(filename="b.pdf", size=2, url="https://cdn/b")),
    ]
    prefix = build_attachment_url_prefix(atts)
    assert prefix.count("\n") == 1, "two attachments → two lines joined by one newline"
    assert "`a.csv`" in prefix and "`b.pdf`" in prefix, "every attachment must be surfaced"


def _attachment(att_id: int, filename: str, content_type: str, size: int) -> discord.Attachment:
    att = MagicMock(spec=discord.Attachment)
    att.id = att_id
    att.filename = filename
    att.content_type = content_type
    att.size = size
    att.width = att.height = 10
    att.url = f"https://cdn.discord/{filename}?hm=ab"
    att.read = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")
    return att


async def test_prepare_attachments_inlines_images_and_links_data_files() -> None:
    image = _attachment(1, "photo.png", "image/png", 1024)
    data = _attachment(2, "sales.csv", "text/csv", 2048)

    prefix, blocks = await prepare_attachments([image, data])

    assert blocks is not None and len(blocks) == 1, "the image goes in as a vision block"
    assert "sales.csv" in prefix and "photo.png" in prefix, "every file is named in the prefix"


async def test_prepare_attachments_falls_back_to_a_url_for_an_oversized_image() -> None:
    big = _attachment(3, "huge.png", "image/png", MAX_VISION_IMAGE_BYTES + 1)

    prefix, blocks = await prepare_attachments([big])

    assert blocks is None, "an image over the byte cap is not downloaded"
    assert "https://cdn.discord/huge.png?hm=ab" in prefix, "the agent can still fetch it"
    big.read.assert_not_awaited()


async def test_prepare_attachments_without_files_adds_nothing() -> None:
    assert await prepare_attachments([]) == ("", None), "no files, no prefix, no blocks"
