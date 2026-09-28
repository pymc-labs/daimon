"""Output delivery: consent cards in 1:1 chats, notes in channels, the delete contract."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.output_delivery import (
    FILE_CONSENT_CONTENT_TYPE,
    FILE_INFO_CONTENT_TYPE,
    TeamsOutputDelivery,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from microsoft_teams.api import Attachment, FileConsentCard, FileConsentInvokeActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_ACCOUNT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    THREAD_ID,
    FakeSender,
    build_teams_runtime,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)
UPLOAD_URL = "https://contoso-my.sharepoint.com/personal/u/_api/v2.0/uploadSession?guid=1"
CONTENT_URL = "https://contoso-my.sharepoint.com/personal/u/Documents/data.csv"


def _inbound(kind: str = "dm") -> TeamsInbound:
    conversation = CONVERSATION_ID if kind == "dm" else THREAD_ID
    return TeamsInbound(
        kind=kind,  # type: ignore[arg-type]
        entra_tenant_id=ENTRA_TENANT_ID,
        user_id=AAD_OBJECT_ID,
        conversation_id=conversation,
        channel_id=conversation,
        activity_id=str(uuid.uuid4()),
        text="make a csv",
        service_url=SERVICE_URL,
    )


def _ma(listing: list[FileMetadata], deletes: list[str]) -> AsyncAnthropic:
    """MA serving `listing` (minus deleted entries) and recording deletes."""

    def on_list(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return list_response([f.model_dump(mode="json") for f in listing if f.id not in deletes])

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deletes.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router = MARouter()
    router.add("GET", r"/v1/files", on_list)
    router.add(
        "GET", r"/v1/files/([^/]+)/content", lambda r, m: httpx.Response(200, content=b"a,b\n")
    )
    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    return build_fake_anthropic(router.dispatch)


def _delivery(
    db_factory: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    sender: FakeSender,
    uploads: list[httpx.Request],
    tasks: list[asyncio.Task[None]],
) -> TeamsOutputDelivery:
    def on_upload(request: httpx.Request) -> httpx.Response:
        uploads.append(request)
        return httpx.Response(201, json={"id": "item-1"})

    def spawn(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    async def no_sleep(_: float) -> None:
        return None

    runtime = build_teams_runtime(
        db_factory,
        anthropic=anthropic,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(on_upload)),
    )
    return TeamsOutputDelivery(runtime=runtime, sender=sender, spawn=spawn, sleep=no_sleep)


def _consent(
    action: str, token: str, *, user: str = AAD_OBJECT_ID, upload_url: str = UPLOAD_URL
) -> Any:
    """A Teams `fileConsent/invoke` as the SDK hands it to the handler."""
    activity = FileConsentInvokeActivity.model_validate(
        {
            "type": "invoke",
            "name": "fileConsent/invoke",
            "id": "invoke-1",
            "channelId": "msteams",
            "serviceUrl": SERVICE_URL,
            "from": {"id": "29:user", "aadObjectId": user},
            "conversation": {
                "id": CONVERSATION_ID,
                "conversationType": "personal",
                "tenantId": ENTRA_TENANT_ID,
            },
            "recipient": {"id": BOT_ACCOUNT_ID},
            "value": {
                "type": "fileUpload",
                "action": action,
                "context": {"offer": token},
                "uploadInfo": {
                    "name": "data.csv",
                    "uploadUrl": upload_url,
                    "contentUrl": CONTENT_URL,
                    "uniqueId": "unique-1",
                    "fileType": "csv",
                },
            },
        }
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


@pytest.mark.asyncio
async def test_accepted_offer_uploads_then_deletes_and_shows_the_file(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sweep offers a card and defers; Accept PUTs the bytes, deletes, shows a file card."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    deletes: list[str] = []
    uploads: list[httpx.Request] = []
    tasks: list[asyncio.Task[None]] = []
    sender = FakeSender()
    delivery = _delivery(db_session_factory, _ma([_csv()], deletes), sender, uploads, tasks)

    await delivery.sweep(_inbound(), "sesn_1")
    card = _card(sender, 0)
    assert card.content_type == FILE_CONSENT_CONTENT_TYPE and card.name == "data.csv"
    assert deletes == [], "an offered file stays listed until the person decides"

    await delivery.handle_consent(_consent("accept", _offer_token(sender)))
    await asyncio.gather(*tasks)

    assert [(r.method, str(r.url)) for r in uploads] == [("PUT", UPLOAD_URL)]
    assert uploads[0].headers["content-range"] == "bytes 0-3/4" and uploads[0].content == b"a,b\n"
    assert deletes == ["file_csv"], "the output is deleted only after the upload succeeded"
    info = _card(sender, -1)
    assert (info.content_type, info.content_url) == (FILE_INFO_CONTENT_TYPE, CONTENT_URL)


@pytest.mark.asyncio
async def test_declined_offer_deletes_the_file_and_says_so(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decline is explicit: the output is deleted and the person gets an acknowledgement."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    deletes: list[str] = []
    uploads: list[httpx.Request] = []
    tasks: list[asyncio.Task[None]] = []
    sender = FakeSender()
    delivery = _delivery(db_session_factory, _ma([_csv()], deletes), sender, uploads, tasks)

    await delivery.sweep(_inbound(), "sesn_1")
    await delivery.handle_consent(_consent("decline", _offer_token(sender)))
    await asyncio.gather(*tasks)

    assert uploads == [] and deletes == ["file_csv"]
    assert sender.activities[-1].text == "Okay, I won't send `data.csv`."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "user"), [("forged", AAD_OBJECT_ID), (None, OTHER_AAD_OBJECT_ID)]
)
async def test_unknown_token_or_another_person_cannot_claim_an_offer(
    db_session_factory: async_sessionmaker[AsyncSession], token: str | None, user: str
) -> None:
    """The round-tripped context is untrusted: no upload, no delete, the offer survives."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    deletes: list[str] = []
    uploads: list[httpx.Request] = []
    tasks: list[asyncio.Task[None]] = []
    sender = FakeSender()
    delivery = _delivery(db_session_factory, _ma([_csv()], deletes), sender, uploads, tasks)
    await delivery.sweep(_inbound(), "sesn_1")
    real = _offer_token(sender)

    await delivery.handle_consent(_consent("accept", token or real, user=user))
    await asyncio.gather(*tasks)
    assert uploads == [] and deletes == [], "nothing happens for a stranger's click"
    assert "expired" in (sender.activities[-1].text or "")

    await delivery.handle_consent(_consent("accept", real))
    await asyncio.gather(*tasks)
    assert deletes == ["file_csv"], "the owner can still accept afterwards"


@pytest.mark.asyncio
async def test_upload_url_off_sharepoint_is_refused_and_the_file_stays_listed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bytes only go to SharePoint; a refused upload keeps the output for the next sweep."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    deletes: list[str] = []
    uploads: list[httpx.Request] = []
    tasks: list[asyncio.Task[None]] = []
    sender = FakeSender()
    delivery = _delivery(db_session_factory, _ma([_csv()], deletes), sender, uploads, tasks)
    await delivery.sweep(_inbound(), "sesn_1")

    upload_url = "https://uploads.example.com/session"
    await delivery.handle_consent(_consent("accept", _offer_token(sender), upload_url=upload_url))
    await asyncio.gather(*tasks)

    assert uploads == [] and deletes == []
    assert sender.activities[-1].text == "I couldn't upload `data.csv`. Ask me again to retry."


@pytest.mark.asyncio
async def test_a_pending_offer_is_not_sent_twice(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A later turn's sweep sees the file still listed but does not offer it again."""
    deletes: list[str] = []
    sender = FakeSender()
    delivery = _delivery(db_session_factory, _ma([_csv()], deletes), sender, [], [])

    await delivery.sweep(_inbound(), "sesn_1")
    await delivery.sweep(_inbound(), "sesn_1")

    assert len(sender.activities) == 1 and deletes == []


@pytest.mark.asyncio
async def test_channel_outputs_get_a_note_and_are_deleted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bots cannot upload into channels: one note per file, then the output is deleted."""
    deletes: list[str] = []
    sender = FakeSender()
    delivery = _delivery(db_session_factory, _ma([_csv()], deletes), sender, [], [])

    await delivery.sweep(_inbound("channel"), "sesn_1")

    assert [(c, a.text) for c, a, _ in sender.sent] == [
        (
            THREAD_ID,
            "I made `data.csv`, but I can't attach files in channels. Ask me in a 1:1 "
            "chat when you need a file.",
        )
    ]
    assert deletes == ["file_csv"]


@pytest.mark.asyncio
async def test_oversize_output_in_a_dm_gets_the_limit_notice(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The per-file cap is core's: no consent card, the notice, then delete."""
    deletes: list[str] = []
    sender = FakeSender()
    listing = [_csv(size=21 * 1024 * 1024)]
    delivery = _delivery(db_session_factory, _ma(listing, deletes), sender, [], [])

    await delivery.sweep(_inbound(), "sesn_1")

    assert "over the 20 MiB delivery limit" in (sender.activities[0].text or "")
    assert deletes == ["file_csv"]
