"""Output delivery: consent cards in 1:1 chats, log lines in channels, the delete contract."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import structlog
from anthropic.types.beta import FileMetadata
from daimon.adapters.teams.output_delivery import (
    FILE_CONSENT_CONTENT_TYPE,
    FILE_INFO_CONTENT_TYPE,
    TeamsOutputDelivery,
)
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from microsoft_teams.api import Attachment, FileConsentCard, FileConsentInvokeActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
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
    db_factory: async_sessionmaker[AsyncSession], listing: list[FileMetadata] | None = None
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
    delivery = TeamsOutputDelivery(runtime=runtime, sender=sender, spawn=spawn, sleep=no_sleep)
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


def _csv(file_id: str = "file_csv", size: int = 4) -> FileMetadata:
    return FileMetadata(
        id=file_id,
        created_at=NOW,
        filename="data.csv",
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
    """Bots cannot upload into channels and the agent says so itself: no note, a log, a delete."""
    harness = _harness(db_session_factory)

    with structlog.testing.capture_logs() as logs:
        await harness.delivery.sweep(make_inbound(kind="channel", conversation=THREAD_ID), "sesn_1")

    assert harness.sender.sent == [], "a per-file note would clutter the channel thread"
    skipped = [e for e in logs if e["event"] == "teams.channel_output.skipped"]
    assert [e["file_id"] for e in skipped] == ["file_csv"], "each skipped file is logged"
    assert "filename" not in skipped[0], "the log line carries no file content or name"
    assert harness.deletes == ["file_csv"], "the listing entry goes, so later sweeps skip it"


async def test_oversize_output_in_a_dm_gets_the_limit_notice(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The per-file cap is core's: no consent card, the notice, then delete."""
    harness = _harness(db_session_factory, [_csv(size=21 * 1024 * 1024)])

    await harness.delivery.sweep(make_inbound(), "sesn_1")

    assert "over the 20 MiB delivery limit" in (harness.sender.activities[0].text or "")
    assert harness.deletes == ["file_csv"]
