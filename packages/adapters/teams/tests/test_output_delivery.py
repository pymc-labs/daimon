"""Output delivery: consent cards in 1:1 chats, channel Files links, the delete contract."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import re
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import structlog
from anthropic.types.beta import FileMetadata
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.output_delivery import (
    FILE_CONSENT_CONTENT_TYPE,
    FILE_INFO_CONTENT_TYPE,
    TeamsOutputDelivery,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.file_uploads import create_upload, store_upload_content
from daimon.core.teams_file_offers import UploadOffer, sign_offer
from daimon.core.teams_graph import GraphClient, TeamGroups
from daimon.core.teams_sharepoint import SharePoint
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from microsoft_teams.api import Attachment, FileConsentCard, FileConsentInvokeActivity
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    TEAM_GROUP_ID,
    THREAD_ID,
    FakeSender,
    build_teams_runtime,
    make_inbound,
    make_invoke,
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")
NOW = datetime(2026, 9, 1, tzinfo=UTC)
UPLOAD_URL = "https://contoso-my.sharepoint.com/personal/u/_api/v2.0/uploadSession?guid=1"
CONTENT_URL = "https://contoso-my.sharepoint.com/personal/u/Documents/data.csv"


@dataclasses.dataclass
class _Harness:
    """A delivery over fake MA files and a fake SharePoint, recording what each did."""

    delivery: TeamsOutputDelivery
    sender: FakeSender
    deletes: list[str]
    uploads: list[httpx.Request]
    tasks: list[asyncio.Task[None]]

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks)


def _harness(
    db_factory: async_sessionmaker[AsyncSession],
    listing: list[FileMetadata] | None = None,
    channel_files: ChannelFiles | None = None,
    contents: dict[str, bytes] | None = None,
) -> _Harness:
    """MA serves `listing` (a small CSV by default) minus deleted entries;
    a file's bytes come from `contents` by id, else a small CSV."""
    files = listing if listing is not None else [_csv()]
    deletes: list[str] = []
    uploads: list[httpx.Request] = []
    tasks: list[asyncio.Task[None]] = []

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response([f.model_dump(mode="json") for f in files if f.id not in deletes])

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    def on_upload(request: httpx.Request) -> httpx.Response:
        uploads.append(request)
        return httpx.Response(201, json={"id": "item-1"})

    def spawn(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    async def no_sleep(_: float) -> None:
        return None

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    bodies = contents or {}
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda r, m: httpx.Response(200, content=bodies.get(m.group(1), b"a,b\n")),
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    runtime = build_teams_runtime(
        db_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(on_upload)),
    )
    sender = FakeSender()
    delivery = TeamsOutputDelivery(
        runtime=runtime, sender=sender, spawn=spawn, files=channel_files, sleep=no_sleep
    )
    return _Harness(delivery, sender, deletes, uploads, tasks)


def _consent(
    action: str,
    token: str,
    *,
    user: str = AAD_OBJECT_ID,
    upload_url: str = UPLOAD_URL,
    key: str = "offer",
) -> Any:
    """A Teams `fileConsent/invoke` as the SDK hands it to the handler."""
    upload = {
        "name": "data.csv",
        "uploadUrl": upload_url,
        "contentUrl": CONTENT_URL,
        "uniqueId": "unique-1",
        "fileType": "csv",
    }
    value = {
        "type": "fileUpload",
        "action": action,
        "context": {key: token},
        "uploadInfo": upload,
    }
    activity = FileConsentInvokeActivity.model_validate(
        make_invoke("fileConsent/invoke", value, user=user)
    )
    return SimpleNamespace(
        activity=activity, conversation_ref=SimpleNamespace(service_url=SERVICE_URL)
    )


def _csv(file_id: str = "file_csv", size: int = 4, filename: str = "data.csv") -> FileMetadata:
    return FileMetadata(
        id=file_id,
        created_at=NOW,
        filename=filename,
        mime_type="text/csv",
        size_bytes=size,
        type="file",
        downloadable=True,
    )


def _png(file_id: str, filename: str = "chart.png") -> tuple[FileMetadata, bytes]:
    """A small chart output and its bytes."""
    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), "navy").save(buffer, format="PNG")
    content = buffer.getvalue()
    meta = _csv(file_id, size=len(content), filename=filename)
    return meta.model_copy(update={"mime_type": "image/png"}), content


def _card(sender: FakeSender, index: int) -> Attachment:
    attachments = sender.activities[index].attachments
    assert attachments
    return attachments[0]


def _offer_token(sender: FakeSender) -> str:
    card = _card(sender, 0).content
    assert isinstance(card, FileConsentCard)
    return cast("dict[str, str]", card.accept_context)["offer"]


async def test_accepted_offer_uploads_then_deletes_and_shows_the_file(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sweep offers a card and defers; Accept PUTs the bytes, deletes, shows a file card."""
    harness = _harness(db_session_factory)

    await harness.delivery.sweep(make_inbound(), "sesn_1")
    card = _card(harness.sender, 0)
    assert card.content_type == FILE_CONSENT_CONTENT_TYPE and card.name == "data.csv"
    assert harness.deletes == [], "an offered file stays listed until the person decides"

    await harness.delivery.handle_consent(_consent("accept", _offer_token(harness.sender)))
    await harness.settle()

    [upload] = harness.uploads
    assert (upload.method, str(upload.url)) == ("PUT", UPLOAD_URL)
    assert upload.headers["content-range"] == "bytes 0-3/4" and upload.content == b"a,b\n"
    assert harness.deletes == ["file_csv"], "the output is deleted only after the upload succeeded"
    info = _card(harness.sender, -1)
    assert (info.content_type, info.content_url) == (FILE_INFO_CONTENT_TYPE, CONTENT_URL)


@pytest.mark.parametrize(
    ("failure", "error"), [("fail_on", httpx.ConnectError), ("timeout_on", httpx.ReadTimeout)]
)
async def test_failed_file_card_send_keeps_dm_output_for_retry(
    db_session_factory: async_sessionmaker[AsyncSession], failure: str, error: type[Exception]
) -> None:
    harness = _harness(db_session_factory)
    await harness.delivery.sweep(make_inbound(), "sesn_1")
    getattr(harness.sender, failure).add(1)

    await harness.delivery.handle_consent(_consent("accept", _offer_token(harness.sender)))
    with pytest.raises(error):
        await harness.settle()

    assert len(harness.uploads) == 1
    assert harness.deletes == [], "an unconfirmed file card leaves the output retryable"


async def _staged(db_factory: async_sessionmaker[AsyncSession]) -> str:
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with db_factory.begin() as session:
        row, token = await create_upload(
            session,
            tenant_id=tenant_id,
            title="chart",
            display_filename="chart.png",
            content_type="image/png",
            now=NOW,
        )
        await store_upload_content(session, upload_token=token, data=b"png", now=NOW)
    return row.id


@pytest.mark.parametrize("user", [AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID])
async def test_a_staged_file_offer_uploads_for_its_own_person_only(
    db_session_factory: async_sessionmaker[AsyncSession], user: str
) -> None:
    """The MCP server's offer is signed: only its person in its chat gets the bytes."""
    harness = _harness(db_session_factory)
    offer = UploadOffer(await _staged(db_session_factory), AAD_OBJECT_ID, CONVERSATION_ID)
    token = sign_offer(offer, secret="test-secret", now=time.time())

    await harness.delivery.handle_consent(_consent("accept", token, user=user, key="upload"))
    await harness.settle()

    if user != AAD_OBJECT_ID:
        assert harness.uploads == []
        assert "expired" in (harness.sender.activities[-1].text or "")
        return
    [upload] = harness.uploads
    assert (str(upload.url), upload.content) == (UPLOAD_URL, b"png")
    assert _card(harness.sender, -1).content_type == FILE_INFO_CONTENT_TYPE


async def test_a_forged_staged_offer_uploads_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = _harness(db_session_factory)
    offer = UploadOffer(await _staged(db_session_factory), AAD_OBJECT_ID, CONVERSATION_ID)
    token = sign_offer(offer, secret="not-the-secret", now=time.time())
    await harness.delivery.handle_consent(_consent("accept", token, key="upload"))
    await harness.settle()
    assert harness.uploads == []


async def test_declined_offer_deletes_the_file_and_posts_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decline is explicit: the output is deleted, with no status message under the card."""
    harness = _harness(db_session_factory)

    await harness.delivery.sweep(make_inbound(), "sesn_1")
    await harness.delivery.handle_consent(_consent("decline", _offer_token(harness.sender)))
    await harness.settle()

    assert harness.uploads == [] and harness.deletes == ["file_csv"]
    assert len(harness.sender.activities) == 1, "only the consent card"


@pytest.mark.parametrize(
    ("token", "user"), [("forged", AAD_OBJECT_ID), (None, OTHER_AAD_OBJECT_ID)]
)
async def test_unknown_token_or_another_person_cannot_claim_an_offer(
    db_session_factory: async_sessionmaker[AsyncSession], token: str | None, user: str
) -> None:
    """The round-tripped context is untrusted: no upload, no delete, the offer survives."""
    harness = _harness(db_session_factory)
    await harness.delivery.sweep(make_inbound(), "sesn_1")
    real = _offer_token(harness.sender)

    await harness.delivery.handle_consent(_consent("accept", token or real, user=user))
    await harness.settle()
    assert harness.uploads == [] and harness.deletes == [], "nothing happens for a stranger's click"
    assert "expired" in (harness.sender.activities[-1].text or "")

    await harness.delivery.handle_consent(_consent("accept", real))
    await harness.settle()
    assert harness.deletes == ["file_csv"], "the owner can still accept afterwards"


async def test_upload_url_off_sharepoint_is_refused_and_the_file_stays_listed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bytes only go to SharePoint; a refused upload keeps the output for the next sweep."""
    harness = _harness(db_session_factory)
    await harness.delivery.sweep(make_inbound(), "sesn_1")

    upload_url = "https://harness.uploads.example.com/session"
    await harness.delivery.handle_consent(
        _consent("accept", _offer_token(harness.sender), upload_url=upload_url)
    )
    await harness.settle()

    assert harness.uploads == [] and harness.deletes == []
    assert (
        harness.sender.activities[-1].text == "I couldn't upload `data.csv`. Ask me again to retry."
    )


async def test_a_pending_offer_is_not_sent_twice(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A later sweep sees the file still listed, pending or uploading, and does not re-offer it."""
    harness = _harness(db_session_factory)

    await harness.delivery.sweep(make_inbound(), "sesn_1")
    await harness.delivery.sweep(make_inbound(), "sesn_1")
    assert len(harness.sender.activities) == 1 and harness.deletes == []

    await harness.delivery.handle_consent(_consent("accept", _offer_token(harness.sender)))
    await harness.delivery.sweep(make_inbound(), "sesn_1")
    await harness.settle()
    assert [_card(harness.sender, -1).content_type] == [FILE_INFO_CONTENT_TYPE]
    assert len(harness.sender.activities) == 2, "no second consent card"


async def test_channel_outputs_are_logged_not_posted_and_are_deleted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without channel files the agent says so itself: no note, a log, a delete."""
    harness = _harness(db_session_factory)

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(make_inbound(kind="channel", conversation=THREAD_ID), "sesn_1")

    assert harness.sender.sent == [], "a per-file note would clutter the channel thread"
    skipped = [e for e in logs if e["event"] == "teams.channel_output.skipped"]
    assert [e["file_id"] for e in skipped] == ["file_csv"], "each skipped file is logged"
    assert "filename" not in skipped[0], "the log line carries no file content or name"
    assert harness.deletes == ["file_csv"], "the listing entry goes, so later sweeps skip it"


def _channel_files(put: Callable[[httpx.Request], httpx.Response]) -> ChannelFiles:
    """A team whose channel folder Graph finds; `put` answers each simple upload."""
    folder = {"id": "01FOLDER", "parentReference": {"driveId": "b!drive-1"}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/filesFolder"):
            return httpx.Response(200, json=folder)
        if request.method == "PUT" and request.url.path.endswith(":/content"):
            return put(request)
        return httpx.Response(404)

    async def token() -> str:
        return "graph-token"

    async def no_lookup(_: str) -> Any:
        raise AssertionError("the activity's group id is used")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def nothing_stored(_: str) -> None:
        return None

    return ChannelFiles(
        SharePoint(GraphClient(http, token), http),
        TeamGroups(no_lookup),
        no_lookup,
        stored_site=nothing_stored,
    )


def _uploaded(request: httpx.Request) -> httpx.Response:
    name = request.url.path.rsplit(":/", 2)[-2].rsplit("/", 1)[-1]
    # A channel named `Q3 (plan)`: Graph leaves parentheses unencoded in webUrl.
    web_url = f"https://example.sharepoint.com/sites/team/Shared%20Documents/Q3%20(plan)/{name}"
    return httpx.Response(201, json={"id": f"item-{name}", "name": name, "webUrl": web_url})


def _in_channel() -> Any:
    inbound = make_inbound(kind="channel", conversation=THREAD_ID)
    return dataclasses.replace(inbound, channel_id=CHANNEL_ID, team_group_id=TEAM_GROUP_ID)


async def test_channel_outputs_are_uploaded_and_linked_below_the_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With the site granted, files go to the channel's Files and their links into the answer."""
    listing = [_csv(), _csv("file_b", filename="b.csv")]
    harness = _harness(db_session_factory, listing, channel_files=_channel_files(_uploaded))
    appended: list[str] = []

    async def append(text: str) -> bool:
        appended.append(text)
        return True

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    base = "https://example.sharepoint.com/sites/team/Shared%20Documents/Q3%20%28plan%29"
    assert appended == [
        f"Saved to this channel's files:\n- [data.csv]({base}/data.csv)\n- [b.csv]({base}/b.csv)"
    ], "one block of links, parentheses escaped so the markdown link holds"
    assert harness.sender.sent == [], "the links ride on the answer, not a new message"
    assert harness.deletes == ["file_csv", "file_b"], "delivered files leave the listing"


async def test_channel_links_go_in_one_message_when_the_answer_cannot_take_them(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An answer too long to edit gets one markdown message for every file, never one each."""
    listing = [_csv(), _csv("file_2", filename="b.csv")]
    harness = _harness(db_session_factory, listing, channel_files=_channel_files(_uploaded))

    async def full(_: str) -> bool:
        return False

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=full)

    [message] = harness.sender.activities
    assert message.text_format == "markdown"
    assert (message.text or "").count("](https://example.sharepoint.com/") == 2, "both files"


async def test_a_refused_channel_upload_is_only_logged_and_deleted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 403 (grant removed) after `files="available"`: logged, and nothing added to the thread."""

    def denied(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "accessDenied"}})

    harness = _harness(db_session_factory, channel_files=_channel_files(denied))
    appended: list[str] = []

    async def append(text: str) -> bool:
        appended.append(text)
        return True

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    assert appended == [], "no status line below the answer"
    assert harness.sender.sent == [], "nor a message of its own"
    failed = [e for e in logs if e["event"] == "teams.channel_output.upload_failed"]
    assert [(e["file_id"], e["status"]) for e in failed] == [("file_csv", 403)]
    assert harness.deletes == ["file_csv"], "the ledger entry goes, as on the skip path"


@pytest.mark.parametrize("status", [None, 408, 423, 429, 503])
async def test_transient_channel_upload_failure_keeps_output_for_retry(
    db_session_factory: async_sessionmaker[AsyncSession], status: int | None
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError("graph unavailable", request=request)
        return httpx.Response(status, json={"error": {"code": "serviceUnavailable"}})

    harness = _harness(db_session_factory, channel_files=_channel_files(unavailable))
    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1")

    assert harness.sender.sent == []
    assert harness.deletes == [], "a temporary Graph failure must leave the output retryable"
    assert any(e["event"] == "output_delivery.file_failed" for e in logs)


async def test_an_unprompted_turn_posts_no_links_message_when_the_answer_cannot_take_them(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nobody asked: an unprompted turn posts its answer and nothing beyond it."""
    harness = _harness(db_session_factory, channel_files=_channel_files(_uploaded))

    async def full(_: str) -> bool:
        return False

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(
            dataclasses.replace(_in_channel(), unprompted=True), "sesn_1", append=full
        )

    assert harness.sender.sent == [], "no standalone message after an unprompted answer"
    assert any(e["event"] == "teams.channel_output.links_withheld" for e in logs), "but logged"
    assert harness.deletes == ["file_csv"], "the file was still saved to the channel's Files"


async def test_oversize_output_in_a_dm_is_only_logged(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The per-file cap is core's: no consent card, no note, a log line; the file is kept."""
    harness = _harness(db_session_factory, [_csv(size=21 * 1024 * 1024)])
    appended: list[str] = []

    async def append(text: str) -> bool:
        appended.append(text)
        return True

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(make_inbound(), "sesn_1", append=append)

    assert appended == [] and harness.sender.activities == [], "nothing posted"
    assert any(e["event"] == "teams.output.oversize" for e in logs)
    assert harness.deletes == [], "an oversize file stays listed rather than being lost"


async def test_a_channel_without_file_access_keeps_an_oversize_output(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No channel could take the oversize file either, so it is kept; a file
    under the cap still goes down the log-and-delete path."""
    listing = [_csv("file_big", size=20 * 1024 * 1024 + 1, filename="render.mp4"), _csv()]
    harness = _harness(db_session_factory, listing)

    await harness.delivery.sweep(make_inbound(kind="channel", conversation=THREAD_ID), "sesn_1")

    assert harness.sender.sent == [] and harness.uploads == []
    assert harness.deletes == ["file_csv"], "only the file under the cap is removed"


async def test_a_channel_with_file_access_keeps_an_oversize_output(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing = [_csv("file_big", size=20 * 1024 * 1024 + 1, filename="render.mp4"), _csv()]
    harness = _harness(db_session_factory, listing, channel_files=_channel_files(_uploaded))

    async def append(text: str) -> bool:
        return True

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    assert harness.deletes == ["file_csv"], "the uploaded file leaves; the oversize one stays"


async def test_dm_image_output_is_previewed_right_before_its_consent_card(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A chart shows inline once, then its card; a deferred re-sweep sends neither again."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], contents={"file_png": content})

    await harness.delivery.sweep(make_inbound(), "sesn_1")
    await harness.delivery.sweep(make_inbound(), "sesn_1")

    preview, card = harness.sender.activities
    [image] = preview.attachments or []
    assert image.content_type == "image/png", "the preview is an inline image attachment"
    assert (image.content_url or "").startswith("data:image/png;base64,"), "as a data URI"
    assert card.attachments and card.attachments[0].content_type == FILE_CONSENT_CONTENT_TYPE
    assert harness.deletes == [], "the preview does not change the consent card's ledger"


async def test_failed_dm_preview_still_sends_the_consent_card(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A preview Teams refuses is logged; the file is still offered."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], contents={"file_png": content})
    harness.sender.fail_on.add(0)

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(make_inbound(), "sesn_1")

    assert _card(harness.sender, 1).content_type == FILE_CONSENT_CONTENT_TYPE, "card still sent"
    assert any(e["event"] == "teams.output_preview.send_failed" for e in logs), "logged"


async def test_channel_image_outputs_are_previewed_after_the_links(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Links ride on the answer; up to four charts follow in one message, in delivery order."""
    images = [_png(f"file_{n}", f"chart{n}.png") for n in range(5)]
    listing = [_csv(), *(meta for meta, _ in images)]
    contents = {meta.id: content for meta, content in images}
    harness = _harness(db_session_factory, listing, _channel_files(_uploaded), contents)
    appended: list[str] = []

    async def append(text: str) -> bool:
        appended.append(text)
        return True

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    assert len(appended) == 1 and appended[0].count("](https://") == 6, "every file is linked"
    [(conversation, message, _)] = harness.sender.sent
    assert conversation == THREAD_ID, "the previews go in the same thread"
    names = [a.name for a in message.attachments or []]
    assert names == ["chart0.png", "chart1.png", "chart2.png", "chart3.png"], "first four"
    assert len(harness.deletes) == 6, "every delivered file leaves the listing"


async def test_failed_channel_preview_leaves_the_delivery_alone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 413 on the preview message is logged; the files are saved and linked as before."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], _channel_files(_uploaded), {"file_png": content})
    harness.sender.fail_on.add(0)

    async def append(_: str) -> bool:
        return True

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    assert any(e["event"] == "teams.output_preview.send_failed" for e in logs), "logged"
    assert harness.deletes == ["file_png"], "the saved file still leaves the listing"


async def test_an_unprompted_channel_turn_posts_no_previews(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unprompted turn posts its answer and nothing else, previews included."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], _channel_files(_uploaded), {"file_png": content})

    async def append(_: str) -> bool:
        return True

    unprompted = dataclasses.replace(_in_channel(), unprompted=True)
    await harness.delivery.sweep(unprompted, "sesn_1", append=append)

    assert harness.sender.sent == [], "no preview message after an unprompted answer"
    assert harness.deletes == ["file_png"], "the file was still saved"
