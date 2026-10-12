"""Tests for vision-attachment routing (``daimon.adapters.discord.vision``)."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any, cast

import discord
import pytest
from daimon.adapters.discord.vision import (
    MAX_VISION_IMAGE_BYTES,
    MAX_VISION_IMAGE_DIMENSION,
    MAX_VISION_IMAGES,
    build_image_url_prefix,
    build_skipped_image_prefix,
    download_as_image_blocks,
    is_vision_image_attachment,
)


@dataclass
class FakeAttachment:
    """Minimal Discord Attachment double — mirrors the fields/methods the
    vision helpers actually use (filename, content_type, size, width, height,
    url, read()). Discord populates width/height for image attachments; they
    are None for non-image files, so they default to None here."""

    filename: str
    content_type: str | None
    size: int
    width: int | None = None
    height: int | None = None
    url: str = ""
    _content: bytes = b""

    async def read(self) -> bytes:
        return self._content


def _as_attachment(fake: object) -> discord.Attachment:
    return cast(discord.Attachment, fake)


class TestIsVisionImageAttachment:
    def test_supported_media_type_under_cap_is_vision(self) -> None:
        att = FakeAttachment(
            filename="photo.png", content_type="image/png", size=1024, width=100, height=100
        )
        assert is_vision_image_attachment(_as_attachment(att)) is True, (
            "png under the size cap should route to vision"
        )

    @pytest.mark.parametrize("media_type", ["image/svg+xml", "image/avif", "application/pdf"])
    def test_unsupported_media_type_is_not_vision(self, media_type: str) -> None:
        att = FakeAttachment(filename="file.bin", content_type=media_type, size=1024)
        assert is_vision_image_attachment(_as_attachment(att)) is False, (
            f"{media_type} is not API-consumable and should route to the notebook path"
        )

    def test_none_content_type_is_not_vision(self) -> None:
        att = FakeAttachment(filename="mystery", content_type=None, size=1024)
        assert is_vision_image_attachment(_as_attachment(att)) is False, (
            "missing content_type should route to the notebook path"
        )

    def test_oversized_image_is_not_vision(self) -> None:
        att = FakeAttachment(
            filename="huge.png", content_type="image/png", size=MAX_VISION_IMAGE_BYTES + 1
        )
        assert is_vision_image_attachment(_as_attachment(att)) is False, (
            "image over the API size cap should route to the notebook path"
        )

    def test_image_at_exact_cap_is_vision(self) -> None:
        att = FakeAttachment(
            filename="edge.png",
            content_type="image/png",
            size=MAX_VISION_IMAGE_BYTES,
            width=100,
            height=100,
        )
        assert is_vision_image_attachment(_as_attachment(att)) is True, (
            "image exactly at the cap should still route to vision"
        )

    def test_oversized_width_is_not_vision(self) -> None:
        att = FakeAttachment(
            filename="wide.png",
            content_type="image/png",
            size=1024,
            width=MAX_VISION_IMAGE_DIMENSION + 1,
            height=100,
        )
        assert is_vision_image_attachment(_as_attachment(att)) is False, (
            "image wider than the API dimension cap should route to the notebook path"
        )

    def test_oversized_height_is_not_vision(self) -> None:
        att = FakeAttachment(
            filename="tall.png",
            content_type="image/png",
            size=1024,
            width=100,
            height=MAX_VISION_IMAGE_DIMENSION + 1,
        )
        assert is_vision_image_attachment(_as_attachment(att)) is False, (
            "image taller than the API dimension cap should route to the notebook path"
        )

    def test_dimension_at_exact_cap_is_vision(self) -> None:
        att = FakeAttachment(
            filename="edge.png",
            content_type="image/png",
            size=1024,
            width=MAX_VISION_IMAGE_DIMENSION,
            height=MAX_VISION_IMAGE_DIMENSION,
        )
        assert is_vision_image_attachment(_as_attachment(att)) is True, (
            "image exactly at the dimension cap should still route to vision"
        )

    def test_unknown_dimensions_is_not_vision(self) -> None:
        att = FakeAttachment(
            filename="photo.png", content_type="image/png", size=1024, width=None, height=None
        )
        assert is_vision_image_attachment(_as_attachment(att)) is False, (
            "unknown dimensions must fail closed — an over-cap block poisons the "
            "session on replay, so the image falls back to the URL path"
        )


class TestDownloadAsImageBlocks:
    @pytest.mark.asyncio
    async def test_downloads_and_encodes_base64_block(self) -> None:
        webp_bytes = b"RIFF\x24\x00\x00\x00WEBPVP8 payload"
        att = FakeAttachment(
            filename="photo.webp",
            content_type="image/webp",
            size=len(webp_bytes),
            width=100,
            height=100,
            _content=webp_bytes,
        )

        blocks, skipped = await download_as_image_blocks([_as_attachment(att)])

        assert skipped == [], "successful download should not produce a skipped entry"
        assert len(blocks) == 1, "one attachment should produce one block"
        assert blocks[0]["type"] == "image"
        source = blocks[0]["source"]
        assert source["type"] == "base64"
        assert source["media_type"] == "image/webp", "block should carry the real media type"
        assert base64.standard_b64decode(source["data"]) == webp_bytes, (
            "block data should round-trip the downloaded bytes"
        )

    @pytest.mark.asyncio
    async def test_mislabeled_content_type_corrected_from_bytes(self) -> None:
        # Discord labeled the attachment image/webp but the bytes are PNG —
        # a declared-vs-actual mismatch is rejected by the API and, once MA
        # replays the block, permanently terminates the session.
        png_bytes = b"\x89PNG\r\n\x1a\npayload"
        att = FakeAttachment(
            filename="screenshot.webp",
            content_type="image/webp",
            size=len(png_bytes),
            width=100,
            height=100,
            _content=png_bytes,
        )

        blocks, skipped = await download_as_image_blocks([_as_attachment(att)])

        assert skipped == [], "a sniffable image should not be skipped over a bad label"
        assert len(blocks) == 1
        source = blocks[0]["source"]
        assert source["type"] == "base64"
        assert source["media_type"] == "image/png", (
            "block media type must come from the bytes, not Discord's content_type"
        )

    @pytest.mark.asyncio
    async def test_unrecognized_bytes_skipped_with_reason(self) -> None:
        att = FakeAttachment(
            filename="fake.png",
            content_type="image/png",
            size=8,
            width=100,
            height=100,
            _content=b"not-img!",
        )

        blocks, skipped = await download_as_image_blocks([_as_attachment(att)])

        assert blocks == [], "bytes matching no supported image format must not become a block"
        assert len(skipped) == 1
        assert skipped[0][0].filename == "fake.png"
        assert "not a supported image format" in skipped[0][1]
        assert "image/png" in skipped[0][1], "skip reason should name the declared media type"

    @pytest.mark.asyncio
    async def test_cdn_fetch_failure_skips_item_other_items_proceed(self) -> None:
        @dataclass
        class _FakeResponse:
            status: int = 503
            reason: str = "CDN flake"

        @dataclass
        class FlakyAttachment:
            filename: str
            content_type: str
            size: int
            width: int | None = None
            height: int | None = None

            async def read(self) -> bytes:
                raise discord.HTTPException(
                    response=cast(Any, _FakeResponse()),
                    message="Service Unavailable",
                )

        png_bytes = b"\x89PNG\r\n\x1a\nok"
        good = FakeAttachment(
            filename="ok.png",
            content_type="image/png",
            size=len(png_bytes),
            width=100,
            height=100,
            _content=png_bytes,
        )
        flaky = FlakyAttachment(
            filename="flake.png", content_type="image/png", size=2, width=100, height=100
        )

        blocks, skipped = await download_as_image_blocks(
            [_as_attachment(flaky), _as_attachment(good)]
        )

        assert len(blocks) == 1, "sibling attachment must still download"
        source = blocks[0]["source"]
        assert source["type"] == "base64"
        assert base64.standard_b64decode(source["data"]) == png_bytes
        assert len(skipped) == 1, "fetch-failed attachment should be reported, not silent"
        assert skipped[0][0].filename == "flake.png", "skip entry should carry the attachment"
        assert "fetch" in skipped[0][1].lower()

    @pytest.mark.asyncio
    async def test_unsupported_media_type_skipped_with_reason(self) -> None:
        att = FakeAttachment(
            filename="diagram.svg",
            content_type="image/svg+xml",
            size=10,
            width=100,
            height=100,
            _content=b"<svg/>",
        )

        blocks, skipped = await download_as_image_blocks([_as_attachment(att)])

        assert blocks == [], "unsupported media type must not become a block"
        assert len(skipped) == 1
        assert skipped[0][0].filename == "diagram.svg", "skip entry should carry the attachment"
        assert skipped[0][1] == "unsupported image media type: image/svg+xml"

    @pytest.mark.asyncio
    async def test_oversized_image_skipped_without_download(self) -> None:
        fetched: list[str] = []

        @dataclass
        class TrackingAttachment:
            filename: str
            content_type: str
            size: int
            width: int | None = None
            height: int | None = None

            async def read(self) -> bytes:
                fetched.append(self.filename)
                return b""

        big = TrackingAttachment(
            filename="huge.png", content_type="image/png", size=MAX_VISION_IMAGE_BYTES + 1
        )

        blocks, skipped = await download_as_image_blocks([_as_attachment(big)])

        assert blocks == [], "oversized image must not become a block"
        assert len(skipped) == 1
        assert skipped[0][0].filename == "huge.png"
        assert "size cap" in skipped[0][1]
        assert fetched == [], "oversized image should be skipped before the CDN fetch"

    @pytest.mark.asyncio
    async def test_oversized_dimension_skipped_without_download(self) -> None:
        fetched: list[str] = []

        @dataclass
        class TrackingAttachment:
            filename: str
            content_type: str
            size: int
            width: int | None = None
            height: int | None = None

            async def read(self) -> bytes:
                fetched.append(self.filename)
                return b""

        wide = TrackingAttachment(
            filename="panorama.png",
            content_type="image/png",
            size=1024,
            width=MAX_VISION_IMAGE_DIMENSION + 1,
            height=1024,
        )

        blocks, skipped = await download_as_image_blocks([_as_attachment(wide)])

        assert blocks == [], "image over the dimension cap must not become a block"
        assert len(skipped) == 1
        assert skipped[0][0].filename == "panorama.png"
        assert "dimension" in skipped[0][1].lower()
        assert fetched == [], "oversized-by-dimension image should be skipped before the CDN fetch"

    @pytest.mark.asyncio
    async def test_count_cap_limits_blocks_and_reports_overflow(self) -> None:
        # MAX_VISION_IMAGES + 1 small, valid images: the cap keeps the first
        # MAX_VISION_IMAGES (the API tightens the dimension limit to 2000px once
        # a request carries more than 20, so we never send that many).
        png_bytes = b"\x89PNG\r\n\x1a\nok"
        atts = [
            FakeAttachment(
                filename=f"img{i}.png",
                content_type="image/png",
                size=len(png_bytes),
                width=100,
                height=100,
                _content=png_bytes,
            )
            for i in range(MAX_VISION_IMAGES + 1)
        ]

        blocks, skipped = await download_as_image_blocks([_as_attachment(a) for a in atts])

        assert len(blocks) == MAX_VISION_IMAGES, (
            f"no more than {MAX_VISION_IMAGES} image blocks should be sent in one request"
        )
        assert len(skipped) == 1, "the overflow image should be reported, not silently dropped"
        assert skipped[0][0].filename == f"img{MAX_VISION_IMAGES}.png", (
            "the first MAX_VISION_IMAGES images are kept; the overflow tail is skipped"
        )
        assert str(MAX_VISION_IMAGES) in skipped[0][1]

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self) -> None:
        blocks, skipped = await download_as_image_blocks([])
        assert blocks == [], "no attachments should produce no blocks"
        assert skipped == [], "no attachments should produce no skips"


class TestBuildImageUrlPrefix:
    def test_empty_input_returns_empty_string(self) -> None:
        assert build_image_url_prefix([]) == "", "no images should produce no prefix"

    def test_line_carries_filename_size_and_signed_url(self) -> None:
        att = FakeAttachment(
            filename="chart.png",
            content_type="image/png",
            size=412331,
            url="https://cdn.discordapp.com/attachments/1/2/chart.png?ex=abc&is=def&hm=0f0f",
        )

        prefix = build_image_url_prefix([_as_attachment(att)])

        assert "`chart.png`" in prefix, "line should name the attached file"
        assert "412331 bytes" in prefix, "line should carry the file size"
        assert (
            "https://cdn.discordapp.com/attachments/1/2/chart.png?ex=abc&is=def&hm=0f0f" in prefix
        ), "line should carry the full signed CDN URL including signature params"
        assert prefix.startswith("[attachment]"), (
            "line must be labelled adapter metadata, never impersonate a system message"
        )
        assert "*system:" not in prefix, (
            "a spoofed system line in user-turn text reads as prompt injection"
        )

    def test_multiple_images_one_line_each_in_order(self) -> None:
        first = FakeAttachment(
            filename="chart.png",
            content_type="image/png",
            size=100,
            url="https://cdn.discordapp.com/attachments/1/2/chart.png?ex=a",
        )
        second = FakeAttachment(
            filename="mascot.jpg",
            content_type="image/jpeg",
            size=200,
            url="https://cdn.discordapp.com/attachments/1/3/mascot.jpg?ex=b",
        )

        prefix = build_image_url_prefix([_as_attachment(first), _as_attachment(second)])

        lines = prefix.split("\n")
        assert len(lines) == 2, "each image should contribute exactly one line"
        assert "chart.png" in lines[0] and "mascot.jpg" in lines[1], (
            "lines should preserve attachment order"
        )


class TestBuildSkippedImagePrefix:
    def test_empty_input_returns_empty_string(self) -> None:
        assert build_skipped_image_prefix([]) == "", "no skipped images should produce no prefix"

    def test_line_states_not_inlined_with_reason_and_fetch_guidance(self) -> None:
        att = FakeAttachment(
            filename="panorama.png",
            content_type="image/png",
            size=1024,
            url="https://cdn.discordapp.com/attachments/1/2/panorama.png?ex=a&is=b&hm=c",
        )

        prefix = build_skipped_image_prefix(
            [(_as_attachment(att), "image dimensions exceed 8000px")]
        )

        assert "`panorama.png`" in prefix, "line should name the skipped image"
        assert "image dimensions exceed 8000px" in prefix, "line should carry the skip reason"
        assert "NOT inlined" in prefix, "line should make clear there is no vision block"
        assert "read" in prefix, "line should tell the agent to curl + read to view it"
        assert "https://cdn.discordapp.com/attachments/1/2/panorama.png?ex=a&is=b&hm=c" in prefix, (
            "line should carry the full signed CDN URL so the agent can fetch the image"
        )
        assert prefix.startswith("[attachment]"), (
            "line must be labelled adapter metadata, never impersonate a system message"
        )
        assert "*system:" not in prefix, (
            "a spoofed system line in user-turn text reads as prompt injection"
        )

    def test_multiple_skipped_images_one_line_each_in_order(self) -> None:
        first = FakeAttachment(
            filename="a.png", content_type="image/png", size=1, url="https://cdn/x/a.png?ex=a"
        )
        second = FakeAttachment(
            filename="b.png", content_type="image/png", size=1, url="https://cdn/x/b.png?ex=b"
        )

        prefix = build_skipped_image_prefix(
            [(_as_attachment(first), "too many"), (_as_attachment(second), "too big")]
        )

        lines = prefix.split("\n")
        assert len(lines) == 2, "each skipped image should contribute exactly one line"
        assert "a.png" in lines[0] and "b.png" in lines[1], "lines should preserve order"


def test_dimension_skipped_image_is_warned_not_told_to_read_it() -> None:
    """The pixel cap must not hand the agent a URL and say "curl it then read it".

    Regression: staging session sesn_01TBcsjhyD4KMEc6wasC3vyg. Four images were
    refused inline here for exceeding 8000px, the note told the agent to fetch
    and ``read`` them, ``read`` inlined them at full size, and the resulting
    terminal rejection killed the session and bricked the whole thread.
    """
    att = FakeAttachment(
        filename="huge.jpg",
        content_type="image/jpeg",
        size=1024,
        width=12000,
        height=4000,
        url="https://cdn.discordapp.com/attachments/1/2/huge.jpg?ex=a",
    )

    text = build_skipped_image_prefix(
        [(att, f"image dimensions unknown or exceed {MAX_VISION_IMAGE_DIMENSION}px")]
    )

    assert "Do NOT `read` it at full size" in text
    assert "Downscale it first" in text
    assert "kills" in text, "the consequence must be stated, not just the instruction"
    assert att.url in text, "the URL is still surfaced — downscaling needs the bytes"


def test_non_dimension_skip_keeps_the_plain_fetch_and_read_instruction() -> None:
    """Only the pixel cap is dangerous to ``read``; other skips keep the old advice."""
    att = FakeAttachment(
        filename="big.png",
        content_type="image/png",
        size=9_000_000,
        width=800,
        height=600,
        url="https://cdn.discordapp.com/attachments/1/2/big.png?ex=a",
    )

    text = build_skipped_image_prefix(
        [(att, f"exceeds vision size cap (9000000 > {MAX_VISION_IMAGE_BYTES})")]
    )

    assert "`read` tool to view it" in text
    assert "Do NOT `read`" not in text


@dataclass
class _FlakeResponse:
    status: int = 503
    reason: str = "CDN flake"


@dataclass
class _TrackedAttachment:
    """An attachment whose read can fail and records how many reads overlap."""

    filename: str
    tracker: dict[str, int]
    fails: bool = False
    content_type: str = "image/png"
    width: int = 100
    height: int = 100
    url: str = ""

    @property
    def payload(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + self.filename.encode()

    @property
    def size(self) -> int:
        return len(self.payload)

    async def read(self) -> bytes:
        self.tracker["in_flight"] += 1
        self.tracker["peak"] = max(self.tracker["peak"], self.tracker["in_flight"])
        await asyncio.sleep(0.01)
        self.tracker["in_flight"] -= 1
        if self.fails:
            raise discord.HTTPException(
                response=cast(Any, _FlakeResponse()), message="Service Unavailable"
            )
        return self.payload


async def test_downloads_overlap_and_keep_attachment_order() -> None:
    """Several images download side by side, a few at a time, in message order."""
    tracker = {"in_flight": 0, "peak": 0}
    atts = [_TrackedAttachment(f"img{i}.png", tracker) for i in range(8)]

    blocks, skipped = await download_as_image_blocks([_as_attachment(a) for a in atts])

    decoded = [base64.standard_b64decode(b["source"]["data"]) for b in blocks]
    assert decoded == [a.payload for a in atts], "blocks keep the attachments' order"
    assert skipped == [], "every image fits under the cap"
    assert 1 < tracker["peak"] <= 4, f"reads should overlap, at most four at once: {tracker}"


async def test_a_failed_download_frees_its_slot_for_a_later_image() -> None:
    """With more images than the cap, a failed fetch lets the next image in,
    exactly as fetching one at a time did."""
    tracker = {"in_flight": 0, "peak": 0}
    atts = [
        _TrackedAttachment(f"img{i}.png", tracker, fails=i == 1)
        for i in range(MAX_VISION_IMAGES + 2)
    ]

    blocks, skipped = await download_as_image_blocks([_as_attachment(a) for a in atts])

    kept = [a for a in atts if not a.fails][:MAX_VISION_IMAGES]
    decoded = [base64.standard_b64decode(b["source"]["data"]) for b in blocks]
    assert decoded == [a.payload for a in kept], "the cap fills in order past the failure"
    assert [(a.filename, "fetch" in reason.lower()) for a, reason in skipped] == [
        ("img1.png", True),
        (f"img{MAX_VISION_IMAGES + 1}.png", False),
    ], "the failed image and the one past the cap are skipped, in message order"
