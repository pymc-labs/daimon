"""Inbound attachments: parsing, host allow-lists, size caps and the agent's lines."""

from __future__ import annotations

import io
from functools import partial

import httpx
import pytest
from daimon.adapters.teams.attachments import (
    MAX_ATTACHMENTS,
    ChannelMedia,
    InboundFile,
    SharedFile,
    parse_attachments,
    prepare_attachments,
)
from daimon.core.media.vision import (
    MAX_DOWNSCALE_SOURCE_BYTES,
    MAX_VISION_IMAGE_DIMENSION,
    UNFIT_REASON,
)
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


_prepare_attachments = partial(prepare_attachments, bot_token=_bot_token, service_url=SERVICE_URL)


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
        prepared = await _prepare_attachments(
            http, [InboundFile("pasted_image", "image", PASTED_URL)]
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
        prepared = await _prepare_attachments(http, [InboundFile("pasted_image", "image", url)])

    assert all(r.url.host != "images.example.com" for r in requests), "no request off-host"
    assert prepared.image_blocks == []
    assert "images.example.com is not an allowed file host" in prepared.prefix


async def test_shared_file_is_linked_for_the_agent_without_a_download() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a non-image file must not be downloaded")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await _prepare_attachments(
            http, [InboundFile("shared_file", "report.pdf", DOWNLOAD_URL)]
        )

    assert DOWNLOAD_URL in prepared.prefix and "`report.pdf`" in prepared.prefix
    assert "replace_channel_file" not in prepared.prefix, (
        "a file without a link and version is never offered for replacing"
    )


async def test_a_channel_file_line_names_the_link_and_version_a_replace_takes() -> None:
    """A file in the channel's Files can be saved over, so its line says how."""
    link = "https://example.sharepoint.com/sites/team/Shared Documents/General/deck.pptx"
    media = ChannelMedia(
        files=(SharedFile("deck.pptx", link, DOWNLOAD_URL, version="{D},3"),), group_id="g"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as http:
        prepared = await _prepare_attachments(http, [], channel=True, channel_media=media)

    assert DOWNLOAD_URL in prepared.prefix, "the file is still fetched by its download URL"
    assert f"replace_channel_file link `{link}` and version `{{D}},3`." in prepared.prefix, (
        "the link and version that save a new version over it, each quoted unambiguously"
    )


async def test_oversize_shared_image_falls_back_to_a_link_without_the_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * MAX_DOWNSCALE_SOURCE_BYTES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await _prepare_attachments(
            http, [InboundFile("shared_file", "big.png", DOWNLOAD_URL)]
        )

    assert "authorization" not in requests[0].headers, "a download URL carries its own auth"
    assert prepared.image_blocks == []
    assert "larger than 25 MiB" in prepared.prefix and DOWNLOAD_URL in prepared.prefix


async def test_an_image_past_the_edge_cap_is_resized_and_one_that_cannot_be_read_is_explained() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        png = _png(width=MAX_VISION_IMAGE_DIMENSION + 1, height=200)
        return httpx.Response(200, content=png if request.url.path.endswith("wide") else png[:-30])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        resized = await _prepare_attachments(
            http, [InboundFile("pasted_image", "wide", PASTED_URL + "/wide")]
        )
        refused = await _prepare_attachments(
            http,
            [
                InboundFile("pasted_image", "image", PASTED_URL + "/bad"),
                InboundFile("unreachable", "r.pdf"),
            ],
        )

    assert len(resized.image_blocks) == 1, "a panorama under 25 MiB is shrunk, not linked"
    assert refused.image_blocks == [], "a cut-off image never becomes a block"
    assert f"`image` was not inlined ({UNFIT_REASON})" in refused.prefix, "the reason is plain"
    assert "`r.pdf` was shared but can't be opened: daimon could not fetch it" in refused.prefix


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
        prepared = await _prepare_attachments(
            http,
            [InboundFile("embedded_image", "image")],
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
        prepared = await _prepare_attachments(
            http,
            [],
            channel_media=ChannelMedia(),
            graph_token=graph_token,
            history_images=(hosted, "https://evil.example/hostedContents/h/$value"),
        )
    assert len(prepared.image_blocks) == 1
    assert "1 image(s) from earlier messages are attached" in prepared.prefix
    assert "1 could not be fetched" in prepared.prefix
