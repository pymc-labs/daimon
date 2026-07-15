import base64

import httpx
import pytest
from daimon.adapters.slack.vision import (
    MAX_VISION_IMAGE_BYTES,
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
