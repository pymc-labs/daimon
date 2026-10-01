"""Output delivery: consent cards in 1:1 chats, channel Files links, the delete contract."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import structlog
from anthropic.types.beta import FileMetadata
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.graph import GraphClient, TeamGroups
from daimon.adapters.teams.output_delivery import (
    FILE_CONSENT_CONTENT_TYPE,
    FILE_INFO_CONTENT_TYPE,
    TeamsOutputDelivery,
)
from daimon.adapters.teams.sharepoint import SharePoint
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from microsoft_teams.api import Attachment, FileConsentCard, FileConsentInvokeActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
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
) -> _Harness:
    """MA serves `listing` (a small CSV by default) minus deleted entries."""
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
    router.add(
        "GET", r"/v1/files/([^/]+)/content", lambda r, m: httpx.Response(200, content=b"a,b\n")
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
    action: str, token: str, *, user: str = AAD_OBJECT_ID, upload_url: str = UPLOAD_URL
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
        "context": {"offer": token},
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


async def test_declined_offer_deletes_the_file_and_says_so(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decline is explicit: the output is deleted and the person gets an acknowledgement."""
    harness = _harness(db_session_factory)

    await harness.delivery.sweep(make_inbound(), "sesn_1")
    await harness.delivery.handle_consent(_consent("decline", _offer_token(harness.sender)))
    await harness.settle()

    assert harness.uploads == [] and harness.deletes == ["file_csv"]
    assert harness.sender.activities[-1].text == "Okay, I won't send `data.csv`."


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
    return ChannelFiles(
        SharePoint(GraphClient(http, token), http), TeamGroups(no_lookup), no_lookup
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


async def test_a_refused_channel_upload_is_named_below_the_answer_logged_and_deleted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 403 (grant removed) after `files="available"`: the answer says the file did not land."""

    def denied(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "accessDenied"}})

    harness = _harness(db_session_factory, channel_files=_channel_files(denied))
    appended: list[str] = []

    async def append(text: str) -> bool:
        appended.append(text)
        return True

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(_in_channel(), "sesn_1", append=append)

    assert appended == ["I couldn't save `data.csv` to this channel's files."], (
        "the lost file is named in the answer, not silently dropped"
    )
    assert harness.sender.sent == [], "the note rides on the answer, not a new message"
    failed = [e for e in logs if e["event"] == "teams.channel_output.upload_failed"]
    assert [(e["file_id"], e["status"]) for e in failed] == [("file_csv", 403)]
    assert harness.deletes == ["file_csv"], "the ledger entry goes, as on the skip path"


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


async def test_oversize_output_in_a_dm_gets_the_limit_notice(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The per-file cap is core's: no consent card, the notice, then delete."""
    harness = _harness(db_session_factory, [_csv(size=21 * 1024 * 1024)])

    await harness.delivery.sweep(make_inbound(), "sesn_1")

    assert "over the 20 MiB delivery limit" in (harness.sender.activities[0].text or "")
    assert harness.deletes == ["file_csv"]
