"""Inbound attachments: parsing, host allow-lists, size caps and the agent's lines."""

from __future__ import annotations

import io

import httpx
import pytest
from daimon.adapters.teams.attachments import (
    MAX_ATTACHMENTS,
    ChannelMedia,
    InboundFile,
    parse_attachments,
    prepare_attachments,
)
from daimon.core.media.vision import MAX_VISION_IMAGE_BYTES, MAX_VISION_IMAGE_DIMENSION
from microsoft_teams.api import FILE_DOWNLOAD_INFO_CONTENT_TYPE, Attachment
from PIL import Image

from .conftest import SERVICE_URL

PASTED_URL = f"{SERVICE_URL}/v3/attachments/0-img/views/original"
DOWNLOAD_URL = "https://contoso-my.sharepoint.com/personal/u/_layouts/15/download.aspx?tempauth=t"


def _png(width: int = 4, height: int = 4) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (width, height)).save(buffer, "PNG")
    return buffer.getvalue()


async def _bot_token() -> str:
    return "bot-token"


def test_parse_keeps_pasted_images_and_one_to_one_files_and_skips_cards() -> None:
    files = parse_attachments(
        [
            Attachment(content_type="image/*", content_url=PASTED_URL),
            Attachment(
                content_type=FILE_DOWNLOAD_INFO_CONTENT_TYPE,
                name="q3 report.pdf",
                content={"downloadUrl": DOWNLOAD_URL, "fileType": "pdf"},
            ),
            Attachment(content_type="text/html", content="<p>hi</p>"),
            Attachment(content_type="application/vnd.microsoft.card.adaptive", content={}),
        ],
        personal=True,
    )
    assert files == (
        InboundFile("pasted_image", "image", PASTED_URL),
        InboundFile("shared_file", "q3_report.pdf", DOWNLOAD_URL),
    ), "images and files are kept, names sanitized, cards and HTML bodies ignored"


@pytest.mark.parametrize(
    ("url", "personal"),
    [
        (DOWNLOAD_URL, False),  # A channel share needs Microsoft Graph.
        ("https://files.example.com/report.pdf", True),
        ("http://contoso-my.sharepoint.com/report.pdf", True),
        ("https://contoso-my.sharepoint.com.example.com/report.pdf", True),
    ],
)
def test_parse_marks_files_off_sharepoint_or_in_channels_unreachable(
    url: str, personal: bool
) -> None:
    attachment = Attachment(
        content_type=FILE_DOWNLOAD_INFO_CONTENT_TYPE, name="r.pdf", content={"downloadUrl": url}
    )
    assert parse_attachments([attachment], personal=personal) == (
        InboundFile("unreachable", "r.pdf"),
    ), "only https SharePoint URLs in a 1:1 chat are fetchable"


def test_parse_caps_the_attachment_count() -> None:
    many = [Attachment(content_type="image/*", content_url=PASTED_URL)] * (MAX_ATTACHMENTS + 5)
    assert len(parse_attachments(many, personal=True)) == MAX_ATTACHMENTS


async def test_pasted_image_is_fetched_with_the_bot_token_and_inlined() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_png())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await prepare_attachments(
            http,
            [InboundFile("pasted_image", "image", PASTED_URL)],
            bot_token=_bot_token,
            service_url=SERVICE_URL,
        )

    assert len(prepared.image_blocks) == 1, "the image becomes a vision block"
    assert prepared.image_blocks[0]["source"]["media_type"] == "image/png"  # type: ignore[index]
    assert requests[0].headers["authorization"] == "Bearer bot-token"
    assert "is attached as an image" in prepared.prefix, "the agent knows it was shared"


@pytest.mark.parametrize(
    "url", ["https://images.example.com/cat.png", f"{SERVICE_URL}/redirects-away"]
)
async def test_bot_token_never_reaches_a_host_outside_bot_framework(url: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://images.example.com/cat.png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await prepare_attachments(
            http,
            [InboundFile("pasted_image", "image", url)],
            bot_token=_bot_token,
            service_url=SERVICE_URL,
        )

    assert all(r.url.host != "images.example.com" for r in requests), "no request off-host"
    assert prepared.image_blocks == []
    assert "images.example.com is not an allowed file host" in prepared.prefix


async def test_shared_file_is_linked_for_the_agent_without_a_download() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a non-image file must not be downloaded")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await prepare_attachments(
            http,
            [InboundFile("shared_file", "report.pdf", DOWNLOAD_URL)],
            bot_token=_bot_token,
            service_url=SERVICE_URL,
        )

    assert DOWNLOAD_URL in prepared.prefix and "`report.pdf`" in prepared.prefix


async def test_oversize_shared_image_falls_back_to_a_link_without_the_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * MAX_VISION_IMAGE_BYTES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await prepare_attachments(
            http,
            [InboundFile("shared_file", "big.png", DOWNLOAD_URL)],
            bot_token=_bot_token,
            service_url=SERVICE_URL,
        )

    assert "authorization" not in requests[0].headers, "a download URL carries its own auth"
    assert prepared.image_blocks == []
    assert "larger than 5 MiB" in prepared.prefix and DOWNLOAD_URL in prepared.prefix


async def test_image_past_the_pixel_cap_and_channel_shares_are_explained() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_png(width=MAX_VISION_IMAGE_DIMENSION + 1, height=1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await prepare_attachments(
            http,
            [InboundFile("pasted_image", "image", PASTED_URL), InboundFile("unreachable", "r.pdf")],
            bot_token=_bot_token,
            service_url=SERVICE_URL,
        )

    assert prepared.image_blocks == [], "an image past the pixel cap would end the session"
    assert f"`image` was not inlined (larger than {MAX_VISION_IMAGE_DIMENSION}px)" in (
        prepared.prefix
    )
    assert "`r.pdf` was shared but can't be opened: daimon could not fetch it" in prepared.prefix


async def test_graph_token_only_goes_to_graph_and_a_refused_image_is_explained() -> None:
    """A hosted image is fetched with the Graph token from Graph alone."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_png())

    async def graph_token() -> str:
        return "graph-token"

    hosted = (
        "https://graph.microsoft.com/v1.0/teams/g/channels/c/messages/1/hostedContents/h/$value"
    )
    media = ChannelMedia(image_urls=(hosted, "https://evil.example/hostedContents/h/$value"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await prepare_attachments(
            http,
            [InboundFile("embedded_image", "image")],
            bot_token=_bot_token,
            service_url=SERVICE_URL,
            channel_media=media,
            graph_token=graph_token,
        )
    assert len(prepared.image_blocks) == 1, "the Graph-hosted image is inlined"
    assert [(r.url.host, r.headers["authorization"]) for r in requests] == [
        ("graph.microsoft.com", "Bearer graph-token")
    ], "the off-Graph URL is refused before any request"
    assert "`image` was not inlined (evil.example is not an allowed file host)" in prepared.prefix


async def test_history_images_follow_the_messages_own_and_a_miss_is_admitted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_png())

    async def graph_token() -> str:
        return "graph-token"

    hosted = (
        "https://graph.microsoft.com/v1.0/teams/g/channels/c/messages/1/hostedContents/h/$value"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await prepare_attachments(
            http,
            [],
            bot_token=_bot_token,
            service_url=SERVICE_URL,
            channel_media=ChannelMedia(),
            graph_token=graph_token,
            history_images=(hosted, "https://evil.example/hostedContents/h/$value"),
        )
    assert len(prepared.image_blocks) == 1
    assert "1 image(s) from earlier messages are attached" in prepared.prefix
    assert "1 could not be fetched" in prepared.prefix
