"""Output delivery: consent cards in 1:1 chats, channel Files links, the delete contract."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import re
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import structlog
from anthropic.types.beta import FileMetadata
from daimon.adapters.teams import output_delivery
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.output_delivery import (
    FILE_CONSENT_CONTENT_TYPE,
    FILE_INFO_CONTENT_TYPE,
    OFFER_TTL_S,
    TeamsOutputDelivery,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.output_delivery import DeliverableFile
from daimon.core.stores.file_uploads import create_upload, store_upload_content
from daimon.core.teams_file_offers import UploadOffer, sign_offer
from daimon.core.teams_graph import GraphClient, TeamGroups
from daimon.core.teams_sharepoint import SharePoint
from microsoft_teams.api import Attachment, FileConsentCard, FileConsentInvokeActivity
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
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


def _notice_text(activity: Any) -> str:
    """The words on a notice card; the card is Daimon's red error container."""
    body = json.loads(activity.model_dump_json(by_alias=True))["attachments"][0]["content"]["body"]
    assert activity.text is None, "no plain text beside the card"
    (container,) = body
    assert container["type"] == "Container" and container["style"] == "attention"
    return container["items"][0]["text"]


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
    clock: Callable[[], float] = time.monotonic,
    served: dict[str, bytes | str] | None = None,
    unreadable: frozenset[str] = frozenset(),
) -> _Harness:
    """MA serves `listing` (a small CSV by default) minus deleted entries;
    a file's bytes come from `contents` by id, else a small CSV, and one in
    `unreadable` answers 404. Other plain HTTP is recorded in `uploads`; a URL
    in `served` answers with its bytes, or redirects to it when it is a URL."""
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
        if isinstance(body := (served or {}).get(str(request.url)), str):
            return httpx.Response(302, headers={"Location": body})
        if body is not None:
            return httpx.Response(200, content=body)
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

    def on_download(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        if match.group(1) in unreadable:
            return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error"}})
        return httpx.Response(200, content=bodies.get(match.group(1), b"a,b\n"))

    router.add("GET", r"/v1/files/([^/]+)/content", on_download)
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    runtime = build_teams_runtime(
        db_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(on_upload)),
    )
    sender = FakeSender()
    delivery = TeamsOutputDelivery(
        runtime=runtime,
        sender=sender,
        spawn=spawn,
        files=channel_files,
        clock=clock,
        sleep=no_sleep,
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


@dataclasses.dataclass
class _Answer:
    """The turn's `append_to_answer`: records each edit `takes` lets land on the answer."""

    takes: Callable[[str | None, Sequence[Attachment]], bool] = lambda *_: True
    edits: list[tuple[str | None, list[Attachment]]] = dataclasses.field(
        default_factory=list[tuple[str | None, list[Attachment]]]
    )

    async def __call__(self, text: str | None, attachments: Sequence[Attachment]) -> bool:
        if not self.takes(text, attachments):
            return False
        self.edits.append((text, list(attachments)))
        return True

    @property
    def previews(self) -> list[list[str | None]]:
        """The names of each edit's attachments, for the edits that carried any."""
        return [[a.name for a in attached] for _, attached in self.edits if attached]


def _links_only() -> _Answer:
    """An answer that takes text but refuses attachments, as an oversize edit would be."""
    return _Answer(lambda _, attachments: not attachments)


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
        assert "expired" in _notice_text(harness.sender.activities[-1])
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
    assert "expired" in _notice_text(harness.sender.activities[-1])

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
        _notice_text(harness.sender.activities[-1])
        == "I couldn't upload `data.csv`. Ask me again to retry."
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


async def test_a_channel_without_file_access_previews_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing is saved, so nothing shows: every file, images too, is logged and deleted."""
    images = [_png(f"file_{n}", f"chart{n}.png") for n in range(2)]
    listing = [_csv(), *(meta for meta, _ in images)]
    contents = {meta.id: content for meta, content in images}
    harness = _harness(db_session_factory, listing, contents=contents)
    answer = _Answer()

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(
            make_inbound(kind="channel", conversation=THREAD_ID), "sesn_1", append=answer
        )

    assert harness.sender.sent == [] and answer.edits == [], "no preview, in or after the answer"
    skipped = {e["file_id"] for e in logs if e["event"] == "teams.channel_output.skipped"}
    assert skipped == {meta.id for meta in listing}, "each file is logged as not saved"
    assert sorted(harness.deletes) == sorted(meta.id for meta in listing), "and leaves"
    assert harness.uploads == [], "nothing is saved anywhere"


def _channel_files(
    put: Callable[[httpx.Request], httpx.Response],
    thumbnails: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]] | None = None,
) -> ChannelFiles:
    """A team whose channel folder Graph finds; `put` answers each simple upload,
    `thumbnails` each thumbnail lookup (404 without it)."""
    folder = {"id": "01FOLDER", "parentReference": {"driveId": "b!drive-1"}}

    def handler(request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        if request.url.path.endswith("/filesFolder"):
            return httpx.Response(200, json=folder)
        if request.method == "PUT" and request.url.path.endswith(":/content"):
            return put(request)
        if thumbnails is not None and request.url.path.endswith("/thumbnails"):
            return thumbnails(request)
        return httpx.Response(404)

    async def token() -> str:
        return "graph-token"

    async def no_lookup(_: str) -> Any:
        raise AssertionError("the activity's group id is used")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ChannelFiles(SharePoint(GraphClient(http, token)), TeamGroups(no_lookup))


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
    """With the permission granted, files go to the channel's Files and links into the answer."""
    listing = [_csv(), _csv("file_b", filename="b.csv")]
    harness = _harness(db_session_factory, listing, channel_files=_channel_files(_uploaded))
    answer = _Answer()

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    base = "https://example.sharepoint.com/sites/team/Shared%20Documents/Q3%20%28plan%29"
    assert answer.edits == [
        (
            f"- [data.csv]({base}/data.csv)\n- [b.csv]({base}/b.csv)",
            [],
        )
    ], "one block of links, parentheses escaped so the markdown link holds"
    assert harness.sender.sent == [], "the links ride on the answer, not a new message"
    assert harness.deletes == ["file_csv", "file_b"], "delivered files leave the listing"


async def test_channel_links_go_in_one_message_when_the_answer_cannot_take_them(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An answer too long to edit gets one markdown message for every file, never one each."""
    listing = [_csv(), _csv("file_2", filename="b.csv")]
    harness = _harness(db_session_factory, listing, channel_files=_channel_files(_uploaded))

    async def full(*_: object) -> bool:
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

    async def append(text: str | None, *_: object) -> bool:
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

    async def full(*_: object) -> bool:
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

    async def append(text: str | None, *_: object) -> bool:
        appended.append(text)
        return True

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(make_inbound(), "sesn_1", append=append)

    assert appended == [] and harness.sender.activities == [], "nothing posted"
    assert any(e["event"] == "teams.output.oversize" for e in logs)
    assert harness.deletes == [], "an oversize file stays listed rather than being lost"


@pytest.mark.parametrize("action", ["accept", "decline"])
async def test_neutral_consent_uses_exact_bytes_and_never_anthropic(
    db_session_factory: async_sessionmaker[AsyncSession], action: str
) -> None:
    harness = _harness(db_session_factory, listing=[])
    content = b"\x89PNG\r\n\x1a\n\x00provider-image"
    assert await harness.delivery.deliver_provider_output(
        make_inbound(),
        "session-gemini",
        DeliverableFile("neutral:gemini:root:image", "chart", "image/png", len(content), content),
    )
    assert _card(harness.sender, 0).name == "chart.png"
    assert not harness.uploads
    await harness.delivery.handle_consent(_consent(action, _offer_token(harness.sender)))
    await harness.settle()
    assert not harness.deletes
    if action == "accept":
        [upload] = harness.uploads
        assert upload.content == content
        assert upload.headers["content-range"] == f"bytes 0-{len(content) - 1}/{len(content)}"
        assert _card(harness.sender, -1).content_type == FILE_INFO_CONTENT_TYPE
    else:
        assert not harness.uploads


async def test_neutral_offer_rejects_wrong_actor_then_allows_requester(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = _harness(db_session_factory, listing=[])
    assert await harness.delivery.deliver_provider_output(
        make_inbound(),
        "session-openai",
        DeliverableFile("neutral:openai:root:file", "data.csv", "text/csv", 4, b"a,b\n"),
    )
    token = _offer_token(harness.sender)
    await harness.delivery.handle_consent(_consent("accept", token, user=OTHER_AAD_OBJECT_ID))
    await harness.settle()
    assert not harness.uploads
    await harness.delivery.handle_consent(_consent("accept", token))
    await harness.settle()
    assert harness.uploads[0].content == b"a,b\n"
    assert not harness.deletes


async def test_neutral_graph_upload_preserves_bytes_and_appends_actual_link(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    puts: list[httpx.Request] = []

    def put(request: httpx.Request) -> httpx.Response:
        puts.append(request)
        return _uploaded(request)

    harness = _harness(db_session_factory, listing=[], channel_files=_channel_files(put))
    links: list[str] = []

    async def append(text: str | None, *_: object) -> bool:
        links.append(text)
        return True

    assert await harness.delivery.deliver_provider_output(
        _in_channel(),
        "session-openai",
        DeliverableFile(
            "opaque-artifact-ref", "report.pdf", "application/octet-stream", 4, b"pdf!"
        ),
        append=append,
    )
    assert len(puts) == 1 and puts[0].content == b"pdf!"
    assert links == [
        "- [report.pdf](https://example.sharepoint.com/sites/team/Shared%20Documents/Q3%20%28plan%29/report.pdf)"
    ]
    assert not harness.sender.sent and not harness.deletes


async def test_neutral_consent_replay_wrong_service_and_untrusted_upload_url(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = _harness(db_session_factory, listing=[])
    file = DeliverableFile("opaque", "data.csv", "text/csv", 4, b"a,b\n")
    assert await harness.delivery.deliver_provider_output(make_inbound(), "session-openai", file)
    assert await harness.delivery.deliver_provider_output(make_inbound(), "session-openai", file)
    assert len(harness.sender.activities) == 1
    token = _offer_token(harness.sender)
    ctx = _consent("accept", token)
    ctx.conversation_ref.service_url = "https://different.invalid"
    await harness.delivery.handle_consent(ctx)
    await harness.settle()
    assert not harness.uploads
    await harness.delivery.handle_consent(
        _consent("accept", token, upload_url="https://untrusted.invalid/upload")
    )
    await harness.settle()
    assert not harness.uploads and not harness.deletes
    assert "couldn't upload" in _notice_text(harness.sender.activities[-1])
    await harness.delivery.handle_consent(_consent("accept", token))
    await harness.settle()
    assert not harness.uploads


async def test_neutral_offer_memory_is_bounded_and_expired_bytes_are_released(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.output_delivery import MAX_BYTES_PER_FILE

    harness = _harness(db_session_factory, listing=[])
    now = [0.0]
    harness.delivery._clock = lambda: now[0]
    content = b"x" * MAX_BYTES_PER_FILE
    for i in range(4):
        assert await harness.delivery.deliver_provider_output(
            make_inbound(),
            "session-openai",
            DeliverableFile(str(i), "data.bin", "application/octet-stream", len(content), content),
        )
    extra = DeliverableFile("extra", "data.bin", "application/octet-stream", 1, b"x")
    assert not await harness.delivery.deliver_provider_output(
        make_inbound(), "session-openai", extra
    )
    now[0] = 3601.0
    assert await harness.delivery.deliver_provider_output(make_inbound(), "session-openai", extra)
    assert not harness.uploads and not harness.deletes


async def test_neutral_inflight_consent_bytes_still_count_toward_memory_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.output_delivery import MAX_BYTES_PER_FILE

    harness = _harness(db_session_factory, listing=[])
    started, release = asyncio.Event(), asyncio.Event()

    async def upload(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(201, json={"id": "actual-upload"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(upload))
    harness.delivery._runtime = dataclasses.replace(harness.delivery._runtime, http_client=http)
    content = b"x" * MAX_BYTES_PER_FILE
    try:
        for i in range(4):
            assert await harness.delivery.deliver_provider_output(
                make_inbound(),
                "session-openai",
                DeliverableFile(
                    str(i), "data.bin", "application/octet-stream", len(content), content
                ),
            )
        await harness.delivery.handle_consent(_consent("accept", _offer_token(harness.sender)))
        await started.wait()
        extra = DeliverableFile("extra", "data.bin", "application/octet-stream", 1, b"x")
        assert not await harness.delivery.deliver_provider_output(
            make_inbound(), "session-openai", extra
        )
        release.set()
        await harness.settle()
        assert await harness.delivery.deliver_provider_output(
            make_inbound(), "session-openai", extra
        )
        assert not harness.deletes
    finally:
        release.set()
        await harness.settle()
        await http.aclose()


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

    async def append(text: str | None, *_: object) -> bool:
        return True

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    assert harness.deletes == ["file_csv"], "the uploaded file leaves; the oversize one stays"


async def test_dm_image_output_is_previewed_in_the_answer_above_its_consent_card(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A chart shows inline once, in the answer, then its card; a deferred re-sweep sends neither."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], contents={"file_png": content})
    answer = _Answer()

    await harness.delivery.sweep(make_inbound(), "sesn_1", append=answer)
    await harness.delivery.sweep(make_inbound(), "sesn_1", append=answer)

    [(text, [image])] = answer.edits
    assert text is None, "the preview adds no text to the answer"
    assert image.content_type == "image/png", "the preview is an inline image attachment"
    assert (image.content_url or "").startswith("data:image/png;base64,"), "as a data URI"
    [card] = harness.sender.activities
    assert card.attachments and card.attachments[0].content_type == FILE_CONSENT_CONTENT_TYPE
    assert harness.deletes == [], "the preview does not change the consent card's ledger"


async def test_dm_previews_beyond_what_the_answer_takes_follow_one_per_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first four ride the answer, the fifth comes after the cards; refused, all do."""
    images = [_png(f"file_{n}", f"chart{n}.png") for n in range(5)]
    contents = {meta.id: content for meta, content in images}
    for answer, inline in ((_Answer(), 4), (_Answer(lambda *_: False), 0)):
        harness = _harness(db_session_factory, [meta for meta, _ in images], contents=contents)

        await harness.delivery.sweep(make_inbound(), "sesn_1", append=answer)

        assert answer.previews == ([[f"chart{n}.png" for n in range(4)]] if inline else [])
        kinds = [(a.attachments or [])[0].content_type for a in harness.sender.activities]
        assert kinds == [FILE_CONSENT_CONTENT_TYPE] * 5 + ["image/png"] * (5 - inline), (
            "every card is offered as before, then each preview left over in its own message"
        )


async def test_dm_image_reoffered_after_its_card_expires_is_not_previewed_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A re-offer repeats the consent card only: the chart is already on screen."""
    meta, content = _png("file_png")
    now = [0.0]
    harness = _harness(
        db_session_factory, [meta], contents={"file_png": content}, clock=lambda: now[0]
    )
    answer = _Answer()

    await harness.delivery.sweep(make_inbound(), "sesn_1", append=answer)
    now[0] += OFFER_TTL_S + 1
    await harness.delivery.sweep(make_inbound(), "sesn_1", append=answer)

    assert answer.previews == [["chart.png"]], "one preview"
    kinds = [(a.attachments or [])[0].content_type for a in harness.sender.activities]
    assert kinds == [FILE_CONSENT_CONTENT_TYPE, FILE_CONSENT_CONTENT_TYPE], "a card per offer"


async def test_dm_image_reoffered_after_a_failed_upload_is_previewed_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An answered offer is done with: the next offer is a fresh one, preview included."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], contents={"file_png": content})
    answer = _Answer()
    await harness.delivery.sweep(make_inbound(), "sesn_1", append=answer)
    card = _card(harness.sender, 0).content
    assert isinstance(card, FileConsentCard)
    harness.sender.fail_on.add(1)

    await harness.delivery.handle_consent(
        _consent("accept", cast("dict[str, str]", card.accept_context)["offer"])
    )
    with pytest.raises(httpx.ConnectError):
        await harness.settle()
    await harness.delivery.sweep(make_inbound(), "sesn_1", append=answer)

    kinds = [(a.attachments or [])[0].content_type for a in harness.sender.activities]
    assert kinds == [FILE_CONSENT_CONTENT_TYPE, FILE_INFO_CONTENT_TYPE, FILE_CONSENT_CONTENT_TYPE]
    assert answer.previews == [["chart.png"], ["chart.png"]], (
        "the output stays listed, and its next offer shows the chart again"
    )


async def test_failed_dm_preview_still_sends_the_consent_card(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A preview the answer and then Teams refuse is logged; the file is still offered."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], contents={"file_png": content})
    harness.sender.fail_on.add(1)

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(make_inbound(), "sesn_1", append=_Answer(lambda *_: False))

    assert _card(harness.sender, 0).content_type == FILE_CONSENT_CONTENT_TYPE, "card still sent"
    assert any(e["event"] == "teams.output_preview.send_failed" for e in logs), "logged"


async def test_channel_image_outputs_ride_the_links_edit(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Links and up to four charts, in delivery order, go into the answer in one edit."""
    images = [_png(f"file_{n}", f"chart{n}.png") for n in range(5)]
    listing = [_csv(), *(meta for meta, _ in images)]
    contents = {meta.id: content for meta, content in images}
    harness = _harness(db_session_factory, listing, _channel_files(_uploaded), contents)
    answer = _Answer()

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    [(text, _)] = answer.edits
    assert (text or "").count("](https://") == 6, "every file is linked"
    assert answer.previews == [[f"chart{n}.png" for n in range(4)]], "first four, same edit"
    assert harness.sender.sent == [], "nothing follows the answer"
    assert len(harness.deletes) == 6, "every delivered file leaves the listing"


async def test_failed_channel_preview_leaves_the_delivery_alone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The answer takes the links alone; a 413 on the preview message is only logged."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], _channel_files(_uploaded), {"file_png": content})
    harness.sender.refuse_on.add(0)
    answer = _links_only()

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    [(text, previews)] = answer.edits
    assert (text or "").count("](https://") == 1 and previews == [], "the links still ride"
    assert any(e["event"] == "teams.output_preview.send_failed" for e in logs), "logged"
    assert harness.deletes == ["file_png"], "the saved file still leaves the listing"


@pytest.mark.parametrize("status", [400, 413])
async def test_refused_channel_previews_are_retried_one_per_message(
    db_session_factory: async_sessionmaker[AsyncSession], status: int
) -> None:
    """Several previews may be too big together: each is retried alone, failures only logged."""
    images = [_png(f"file_{n}", f"chart{n}.png") for n in range(3)]
    contents = {meta.id: content for meta, content in images}
    harness = _harness(
        db_session_factory, [meta for meta, _ in images], _channel_files(_uploaded), contents
    )
    harness.sender.refuse_on.add(0)
    harness.sender.refuse_status = status
    harness.sender.fail_on.add(2)

    append = _links_only()

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    names = [[a.name for a in activity.attachments or []] for activity in harness.sender.activities]
    assert names == [
        ["chart0.png", "chart1.png", "chart2.png"],
        ["chart0.png"],
        ["chart1.png"],
        ["chart2.png"],
    ], "the refused combined message is retried one preview at a time, in order"
    failures = [e for e in logs if e["event"] == "teams.output_preview.send_failed"]
    assert len(failures) == 1, "only the preview refused on its own is logged"
    assert len(harness.deletes) == 3, "the files are delivered whatever the previews do"


@pytest.mark.parametrize("status", [403, 429])
async def test_channel_previews_refused_for_another_reason_are_not_resent(
    db_session_factory: async_sessionmaker[AsyncSession], status: int
) -> None:
    """Only 400 and 413 can mean the combined message was too large."""
    images = [_png(f"file_{n}", f"chart{n}.png") for n in range(2)]
    contents = {meta.id: content for meta, content in images}
    harness = _harness(
        db_session_factory, [meta for meta, _ in images], _channel_files(_uploaded), contents
    )
    harness.sender.refuse_on.add(0)
    harness.sender.refuse_status = status

    append = _links_only()

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    previews = [a for a in harness.sender.activities if a.attachments]
    assert len(previews) == 1, "the combined preview message is sent once, never split"


@pytest.mark.parametrize("timeout_index", [0, 1], ids=["asyncio", "httpx"])
async def test_timed_out_channel_previews_are_not_resent(
    db_session_factory: async_sessionmaker[AsyncSession], timeout_index: int
) -> None:
    """A timed-out send may have landed: resending one by one could show each twice."""
    images = [_png(f"file_{n}", f"chart{n}.png") for n in range(2)]
    contents = {meta.id: content for meta, content in images}
    harness = _harness(
        db_session_factory, [meta for meta, _ in images], _channel_files(_uploaded), contents
    )
    # The links message takes index 0 when the answer cannot carry them.
    harness.sender.timeout_on.add(timeout_index)

    append = _Answer(lambda _, attachments: timeout_index == 0 and not attachments)

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    previews = [a for a in harness.sender.activities if a.attachments]
    assert len(previews) == 1, "the combined preview message is sent once, never retried"
    assert any(e["event"] == "teams.output_preview.send_failed" for e in logs), "logged"


async def test_an_unprompted_channel_turn_posts_no_previews(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unprompted turn posts its answer and nothing else, previews included."""
    meta, content = _png("file_png")
    harness = _harness(db_session_factory, [meta], _channel_files(_uploaded), {"file_png": content})

    answer = _Answer()

    unprompted = dataclasses.replace(_in_channel(), unprompted=True)
    await harness.delivery.sweep(unprompted, "sesn_1", append=answer)

    assert harness.sender.sent == [], "no preview message after an unprompted answer"
    assert answer.previews == [], "nor one edited into it"
    assert harness.deletes == ["file_png"], "the file was still saved"


THUMBNAIL_URL = "https://eus1-mediap.svc.ms/transform/thumbnail?provider=spo&tempauth=x"


def _thumbnail_sets(
    *answers: object, asked: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Graph's thumbnail replies in turn, the last one repeated; a number is a status.
    Each lookup is recorded in `asked`."""
    replies = list(answers)

    def reply(request: httpx.Request) -> httpx.Response:
        if asked is not None:
            asked.append(request)
        answer = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": {"code": "itemNotFound"}})
        return httpx.Response(
            200, json={"value": [{"large": {"url": answer}}]} if answer else {"value": []}
        )

    return reply


def _deck_listing() -> tuple[list[FileMetadata], dict[str, bytes]]:
    chart, chart_bytes = _png("file_chart", "chart.png")
    deck = _csv("file_deck", filename="deck.pptx").model_copy(
        update={
            "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        }
    )
    return [deck, _csv(), chart], {"file_chart": chart_bytes}


async def test_an_uploaded_document_previews_its_first_page_after_the_images(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Graph renders the thumbnail late: a missing one is asked for again, then fetched tokenless."""
    listing, contents = _deck_listing()
    _, page = _png("page")
    thumbnails = _thumbnail_sets(404, None, THUMBNAIL_URL)
    files = _channel_files(_uploaded, thumbnails)
    harness = _harness(db_session_factory, listing, files, contents, served={THUMBNAIL_URL: page})
    answer = _Answer()

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    assert answer.previews == [["chart.png"], ["deck.pptx"]], "the image, then the deck's page"
    assert answer.edits[1][0] is None, "the second edit adds no text"
    assert harness.sender.sent == [], "both ride the answer"
    [fetch] = [r for r in harness.uploads if r.url.host.endswith(".svc.ms")]
    assert "Authorization" not in fetch.headers, "the pre-authorised URL gets no token"


async def test_a_thumbnail_the_answer_refuses_follows_in_a_message_of_its_own(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second edit refused (too large with the images, say): the page is sent after."""
    listing, contents = _deck_listing()
    _, page = _png("page")
    files = _channel_files(_uploaded, _thumbnail_sets(THUMBNAIL_URL))
    harness = _harness(db_session_factory, listing, files, contents, served={THUMBNAIL_URL: page})
    answer = _Answer(lambda text, _: text is not None)

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    assert answer.previews == [["chart.png"]], "the image rode the links' edit"
    sent = [[a.name for a in message.attachments or []] for _, message, _ in harness.sender.sent]
    assert sent == [["deck.pptx"]], "the deck's page comes on its own"


async def test_documents_never_take_an_images_preview_place(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Four documents ahead of a chart whose thumbnails never come: the chart still shows."""
    decks = [
        _csv(f"file_doc{n}", filename=name)
        for n, name in enumerate(["deck.pptx", "a.pptx", "b.pdf", "c.docx"])
    ]
    chart, chart_bytes = _png("file_chart", "chart.png")
    files = _channel_files(_uploaded, _thumbnail_sets(None))
    harness = _harness(db_session_factory, [*decks, chart], files, {"file_chart": chart_bytes})
    answer = _Answer()

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    assert answer.previews == [["chart.png"]], (
        "the image keeps its preview though no thumbnail came"
    )


@pytest.mark.parametrize(
    ("answers", "reason", "lookups"),
    [
        ((None,), "not_ready", 3),
        ((500,), "http error", 1),
        (("https://evil.example/thumbnail",), "FetchRefused", 1),
        (("https://svc.ms.example/thumbnail",), "FetchRefused", 1),
        (("https://evilsvc.ms/thumbnail",), "FetchRefused", 1),
    ],
    ids=["never-rendered", "graph-error", "other-host", "lookalike-suffix", "lookalike-domain"],
)
async def test_a_document_thumbnail_that_fails_is_logged_once_and_skipped(
    db_session_factory: async_sessionmaker[AsyncSession],
    answers: tuple[object, ...],
    reason: str,
    lookups: int,
) -> None:
    listing, contents = _deck_listing()
    asked: list[httpx.Request] = []
    files = _channel_files(_uploaded, _thumbnail_sets(*answers, asked=asked))
    harness = _harness(db_session_factory, listing, files, contents)
    answer = _Answer()

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    assert answer.previews == [["chart.png"]], "the chart still shows"
    skips = [e for e in logs if e["event"] == "teams.output_thumbnail.skipped"]
    assert [e["reason"] for e in skips] == [reason], "one line, with no name or URL"
    assert len(asked) == lookups, "only a thumbnail not rendered yet is asked for again"
    assert len(harness.deletes) == 3, "every file was still saved and leaves the listing"


async def test_a_thumbnail_redirected_off_the_allowed_hosts_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every hop is checked: the media service's URL may not send the fetch elsewhere."""
    listing, contents = _deck_listing()
    files = _channel_files(_uploaded, _thumbnail_sets(THUMBNAIL_URL))
    served: dict[str, bytes | str] = {THUMBNAIL_URL: "https://attacker.example/page.png"}
    harness = _harness(db_session_factory, listing, files, contents, served=served)
    answer = _Answer()

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    assert answer.previews == [["chart.png"]], "the chart shows, the redirected thumbnail does not"
    assert [str(r.url) for r in harness.uploads] == [THUMBNAIL_URL], "the redirect is not followed"
    skips = [e["reason"] for e in logs if e["event"] == "teams.output_thumbnail.skipped"]
    assert skips == ["FetchRefused"], "refused, and logged once"


async def test_thumbnails_wait_no_longer_than_their_budget(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out of time, the documents are left out; the images went before them regardless."""
    monkeypatch.setattr(output_delivery, "THUMBNAIL_BUDGET_S", 0.0)
    listing, contents = _deck_listing()

    async def stalled(_: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("the lookup is cancelled first")

    harness = _harness(db_session_factory, listing, _channel_files(_uploaded, stalled), contents)
    answer = _Answer()

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    assert answer.previews == [["chart.png"]], "only the image preview: the deck's ran out of time"
    skips = [e["reason"] for e in logs if e["event"] == "teams.output_thumbnail.skipped"]
    assert skips == ["timed_out"], "the timeout is logged once"


async def test_a_group_chat_previews_saves_or_offers_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No files there, even with channel files on: no preview, no consent card."""
    chat = "19:group-chat-1@thread.v2"
    image, content = _png("file_png")
    harness = _harness(
        db_session_factory,
        [_csv(), image],
        channel_files=_channel_files(_uploaded),
        contents={image.id: content},
    )
    answer = _Answer()
    await harness.delivery.sweep(
        make_inbound(kind="group", conversation=chat), "sesn_1", append=answer
    )

    assert harness.sender.sent == [] and answer.edits == [], "nothing is posted or edited in"
    assert sorted(harness.deletes) == ["file_csv", "file_png"] and harness.uploads == []


def _deck_with_preview() -> tuple[list[FileMetadata], dict[str, bytes]]:
    """A deck and the agent's own picture of its first slide."""
    deck = _deck_listing()[0][0]
    picture, picture_bytes = _png("file_pic", "deck.pptx.preview.png")
    return [deck, picture], {"file_pic": picture_bytes}


@pytest.mark.parametrize("reverse", [False, True], ids=["deck first", "picture first"])
async def test_dm_preview_output_is_shown_never_offered_and_deleted(
    db_session_factory: async_sessionmaker[AsyncSession], reverse: bool
) -> None:
    """In a 1:1 chat a `.preview.png` only shows inline, in the answer above the deck's card."""
    listing, contents = _deck_with_preview()
    harness = _harness(db_session_factory, listing[::-1] if reverse else listing, contents=contents)
    answer = _Answer()

    await harness.delivery.sweep(make_inbound(), "sesn_1", append=answer)

    [card] = harness.sender.activities
    consent = _card(harness.sender, 0).content
    assert isinstance(consent, FileConsentCard), "the deck is offered"
    assert consent.description == "Save deck.pptx to your OneDrive?", "the card asks to save"
    [(_, [image])] = answer.edits
    assert image.name == "deck.pptx", "the preview is named after the file it pictures"
    assert (image.content_url or "").startswith("data:image/png;base64,"), "shown inline"
    assert card.attachments and card.attachments[0].name == "deck.pptx", "one card, the deck's"
    assert harness.deletes == ["file_pic"], "the preview output is deleted, the deck awaits"


async def test_channel_preview_output_is_shown_not_uploaded_and_stands_in_for_the_thumbnail(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """In a channel the agent's picture is previewed, never saved, and Graph is not asked."""
    listing, contents = _deck_with_preview()
    asked: list[httpx.Request] = []
    files = _channel_files(_uploaded, _thumbnail_sets(THUMBNAIL_URL, asked=asked))
    harness = _harness(db_session_factory, listing, files, contents)
    answer = _Answer()

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    [(text, _)] = answer.edits
    assert (text or "").count("](https://") == 1, "only the deck is saved"
    assert "preview" not in (text or ""), "the preview output is never uploaded"
    assert answer.previews == [["deck.pptx"]], "the agent's picture is the one preview"
    assert asked == [], "a pictured document needs no Graph thumbnail"
    assert sorted(harness.deletes) == ["file_deck", "file_pic"], "both leave the listing"


def test_previews_first_puts_each_picture_just_before_its_document() -> None:
    """Unpaired files keep their places; a picture of no listed file stays where it was."""
    names = ["a.csv", "Q3 report.pdf", "b.png", "Q3 report.pdf.preview.png", "x.preview.png"]
    order = output_delivery.previews_first(names)
    assert [names[i] for i in order] == [
        "a.csv",
        "Q3 report.pdf.preview.png",
        "Q3 report.pdf",
        "b.png",
        "x.preview.png",
    ], "the picture moves up to its document"


@pytest.mark.parametrize(
    ("document", "mime_type", "picture"),
    [
        ("Q3 report.pdf", "application/pdf", "Q3 report.pdf.preview.png"),
        ("Bericht-Ü.pdf", "application/pdf", "Bericht-Ü.pdf.preview.png"),
        ("report", "application/pdf", "report.preview.png"),
        ("report", "application/pdf", "report.pdf.preview.png"),
    ],
)
async def test_a_preview_output_matches_its_document_by_the_saved_name(
    db_session_factory: async_sessionmaker[AsyncSession],
    document: str,
    mime_type: str,
    picture: str,
) -> None:
    """A space, a non-ASCII letter or an added extension still pairs picture and document."""
    doc = _csv("file_doc", filename=document).model_copy(update={"mime_type": mime_type})
    preview, preview_bytes = _png("file_pic", picture)
    asked: list[httpx.Request] = []
    files = _channel_files(_uploaded, _thumbnail_sets(THUMBNAIL_URL, asked=asked))
    harness = _harness(db_session_factory, [doc, preview], files, {"file_pic": preview_bytes})
    answer = _Answer()

    await harness.delivery.sweep(_in_channel(), "sesn_1", append=answer)

    assert answer.previews == [[picture.removesuffix(".preview.png")]], "the agent's picture alone"
    assert asked == [], "a pictured document needs no Graph thumbnail"
