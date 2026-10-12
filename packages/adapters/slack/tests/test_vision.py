import asyncio
import base64

import httpx
import pytest
from daimon.adapters.slack.vision import (
    MAX_VISION_IMAGE_BYTES,
    MAX_VISION_IMAGES,
    SlackFile,
    download_as_image_blocks,
    is_vision_image,
)


def _file(**over: object) -> SlackFile:
    base: SlackFile = {
        "id": "F1",
        "mimetype": "image/png",
        "name": "shot.png",
        "size": 1024,
        "url_private": "https://files.slack.com/f/F1",
        "url_private_download": "https://files.slack.com/f/F1/download",
    }
    base.update(over)  # type: ignore[typeddict-item]  # test-only partial override
    return base


def test_is_vision_image_true_for_supported_png_within_caps():
    assert is_vision_image(_file()) is True, "small supported png is a vision image"


def test_is_vision_image_false_for_unsupported_mimetype():
    assert is_vision_image(_file(mimetype="image/svg+xml")) is False, "svg is not API-supported"


def test_is_vision_image_false_when_oversized():
    assert is_vision_image(_file(size=MAX_VISION_IMAGE_BYTES + 1)) is False, "over the byte cap"


def test_is_vision_image_false_when_dimension_exceeds_cap():
    assert is_vision_image(_file(original_w=9000, original_h=10)) is False, "over the pixel cap"


@pytest.mark.asyncio
async def test_download_as_image_blocks_sends_bearer_and_encodes_base64():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, content=b"PNGBYTES")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    blocks, skipped = await download_as_image_blocks(
        [_file()], token="xoxb-abc", http_client=client
    )
    await client.aclose()

    assert captured["auth"] == "Bearer xoxb-abc", "download authenticates with the bot token"
    assert skipped == [], "the supported image is not skipped"
    assert len(blocks) == 1, "one image block produced"
    assert blocks[0]["source"]["data"] == base64.standard_b64encode(b"PNGBYTES").decode(), (
        "bytes are base64-encoded into the block"
    )


@pytest.mark.asyncio
async def test_download_as_image_blocks_skips_on_fetch_error_and_continues():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    blocks, skipped = await download_as_image_blocks(
        [_file()], token="xoxb-abc", http_client=client
    )
    await client.aclose()

    assert blocks == [], "no block for a failed fetch"
    assert len(skipped) == 1 and skipped[0][0]["id"] == "F1", "failed image is reported skipped"


@pytest.mark.asyncio
async def test_download_as_image_blocks_skips_on_transport_error_and_continues():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    blocks, skipped = await download_as_image_blocks(
        [_file()], token="xoxb-abc", http_client=client
    )
    await client.aclose()
    assert blocks == [], "no block when the transport fails"
    assert len(skipped) == 1 and skipped[0][0]["id"] == "F1", (
        "transport error skips the image, not the whole batch"
    )


def _numbered(count: int) -> list[SlackFile]:
    return [
        _file(id=f"F{i}", name=f"img{i}.png", url_private=f"https://files.slack.com/f/F{i}")
        for i in range(count)
    ]


def _overlap_handler(
    tracker: dict[str, int], *, failing: frozenset[str] = frozenset()
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        tracker["in_flight"] += 1
        tracker["peak"] = max(tracker["peak"], tracker["in_flight"])
        await asyncio.sleep(0.01)
        tracker["in_flight"] -= 1
        file_id = request.url.path.rsplit("/", 1)[-1]
        if file_id in failing:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, content=file_id.encode())

    return httpx.MockTransport(handler)


async def test_download_as_image_blocks_overlaps_downloads_in_file_order():
    """Several images download side by side, a few at a time, in message order."""
    tracker = {"in_flight": 0, "peak": 0}
    files = _numbered(8)
    async with httpx.AsyncClient(transport=_overlap_handler(tracker)) as client:
        blocks, skipped = await download_as_image_blocks(files, token="xoxb", http_client=client)

    decoded = [base64.standard_b64decode(b["source"]["data"]).decode() for b in blocks]
    assert decoded == [f["id"] for f in files], "blocks keep the files' order"
    assert skipped == [], "every image fits under the cap"
    assert 1 < tracker["peak"] <= 4, f"fetches should overlap, at most four at once: {tracker}"


async def test_download_as_image_blocks_failed_fetch_frees_its_slot():
    """With more images than the cap, a failed fetch lets the next image in,
    exactly as fetching one at a time did."""
    tracker = {"in_flight": 0, "peak": 0}
    files = _numbered(MAX_VISION_IMAGES + 2)
    transport = _overlap_handler(tracker, failing=frozenset({"F1"}))
    async with httpx.AsyncClient(transport=transport) as client:
        blocks, skipped = await download_as_image_blocks(files, token="xoxb", http_client=client)

    kept = [f["id"] for f in files if f["id"] != "F1"][:MAX_VISION_IMAGES]
    decoded = [base64.standard_b64decode(b["source"]["data"]).decode() for b in blocks]
    assert decoded == kept, "the cap fills in order past the failure"
    assert [(f["id"], "fetch" in reason) for f, reason in skipped] == [
        ("F1", True),
        (f"F{MAX_VISION_IMAGES + 1}", False),
    ], "the failed image and the one past the cap are skipped, in message order"
