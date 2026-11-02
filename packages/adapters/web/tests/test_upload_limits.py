"""Uploads stay bounded and personal: capped before parsing, per-person quota, erased with the chat."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from daimon.adapters.web import chat, upload_limit
from daimon.adapters.web.boot_sweep import sweep_stale_web_uploads
from daimon.adapters.web.files import UNREFERENCED_UPLOAD_TTL
from daimon.core.stores import file_uploads
from daimon.core.stores.file_uploads import create_upload, store_upload_content
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from .chat_support import build_turns, funded_workspace, signed_in, visitor_of
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_HEADERS = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_COOKIE = "__Host-daimon_web"
_FORM = {"file": ("notes.txt", b"hello", "text/plain")}


async def _echo(scope: Scope, receive: Receive, send: Send) -> None:
    body = await Request(scope, receive).body()
    await JSONResponse({"read": len(body)})(scope, receive, send)


def _limited(monkeypatch: Any, cap: int) -> httpx.AsyncClient:
    monkeypatch.setattr(upload_limit, "MAX_BODY_BYTES", cap)
    app = upload_limit.UploadLimitMiddleware(_echo)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_a_declared_length_over_the_cap_is_refused_unread(monkeypatch: Any) -> None:
    async with _limited(monkeypatch, 100) as client:
        res = await client.post(upload_limit.UPLOAD_PATH, content=b"x" * 101)
    assert res.status_code == 413, "a body that declares more than the cap is refused"


async def test_a_body_without_a_length_is_cut_off_at_the_cap(monkeypatch: Any) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * 30

    async with _limited(monkeypatch, 100) as client:
        res = await client.post(upload_limit.UPLOAD_PATH, content=chunks())
        small = await client.post(upload_limit.UPLOAD_PATH, content=b"x" * 100)
        other = await client.post("/elsewhere", content=b"x" * 500)
    assert res.status_code == 413, "a streamed body is refused once it passes the cap"
    assert (small.status_code, small.json()) == (200, {"read": 100}), (
        "a body at the cap is replayed"
    )
    assert other.status_code == 200, "only the upload path is capped"


async def test_the_app_refuses_an_oversized_form_before_parsing_it(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: Any,
) -> None:
    ws = await funded_workspace(db_session_factory)
    monkeypatch.setattr(upload_limit, "MAX_BODY_BYTES", 200)
    async with web_client(web_settings(), sessionmaker=db_session_factory) as client:
        client.cookies.set(_COOKIE, await signed_in(db_session_factory, ws.person))
        client.headers.update(_HEADERS)
        res = await client.post(
            "/api/chat/uploads", files={"file": ("a.bin", b"x" * 500, "text/plain")}
        )
    assert res.status_code == 413, res.text


async def test_a_file_only_its_owner_can_attach_and_a_quota_bounds_them(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    ws = await funded_workspace(db_session_factory)
    guest = await visitor_of(db_session_factory, ws)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    monkeypatch.setattr(chat, "PERSON_UPLOAD_QUOTA_BYTES", 8)

    async with web_client(web_settings(), sessionmaker=db_session_factory, turns=turns) as client:
        client.headers.update(_HEADERS)
        client.cookies.set(_COOKIE, await signed_in(db_session_factory, guest))
        upload = (await client.post("/api/chat/uploads", files=_FORM)).json()
        over = await client.post("/api/chat/uploads", files=_FORM)
        client.cookies.clear()
        client.cookies.set(_COOKIE, await signed_in(db_session_factory, ws.person))
        sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()["id"]
        taken = await client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"id": str(uuid.uuid4()), "content": "see", "attachments": [upload]},
        )
        mine = await client.post("/api/chat/uploads", files=_FORM)

    assert over.status_code == 413, "a second file past the person's quota is refused"
    assert taken.status_code == 422, "another person's handle cannot be attached"
    assert mine.status_code == 200, "the quota is per person"


async def test_deleting_a_chat_erases_its_files_and_the_sweep_takes_the_unsent(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    async with web_client(web_settings(), sessionmaker=db_session_factory, turns=turns) as client:
        client.headers.update(_HEADERS)
        client.cookies.set(_COOKIE, await signed_in(db_session_factory, ws.person))
        sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()["id"]
        sent = (await client.post("/api/chat/uploads", files=_FORM)).json()
        loose = (await client.post("/api/chat/uploads", files=_FORM)).json()
        await client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"id": str(uuid.uuid4()), "content": "see", "attachments": [sent]},
        )
        await turns.drain(10)

        async def held(handle: str) -> bool:
            async with db_session_factory() as db:
                found = await file_uploads.get_upload(db, tenant_id=ws.tenant_id, handle_id=handle)
            return found is not None

        before = await held(sent["id"])
        removed = await sweep_stale_web_uploads(
            db_session_factory, lambda: datetime.now(UTC) + UNREFERENCED_UPLOAD_TTL * 2
        )
        after_sweep = (await held(sent["id"]), await held(loose["id"]))
        await client.delete(f"/api/chat/sessions/{sid}")
        after_delete = await held(sent["id"])

    assert before, "the sent file is kept"
    assert removed >= 1 and after_sweep == (True, False), (
        "the sweep takes the file nothing carries and keeps the sent one"
    )
    assert not after_delete, "deleting the chat erases the files it carried"


async def test_a_swept_file_is_only_a_web_file(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        row, token = await create_upload(
            db,
            tenant_id=ws.tenant_id,
            title="t",
            display_filename="t.txt",
            content_type="text/plain",
            now=now,
        )
        await store_upload_content(db, upload_token=token, data=b"x", now=now)

    removed = await sweep_stale_web_uploads(
        db_session_factory, lambda: now + UNREFERENCED_UPLOAD_TTL * 2
    )

    async with db_session_factory() as db:
        kept = await file_uploads.get_upload(db, tenant_id=ws.tenant_id, handle_id=row.id)
    assert removed == 0 and kept is not None, "a file no person owns is not the sweep's"


async def test_a_large_body_spools_to_disk_and_reaches_the_app_whole(monkeypatch: Any) -> None:
    monkeypatch.setattr(upload_limit, "SPOOL_MEMORY_BYTES", 10)
    monkeypatch.setattr(upload_limit, "REPLAY_CHUNK_BYTES", 7)
    async with _limited(monkeypatch, 1000) as client:
        res = await client.post(upload_limit.UPLOAD_PATH, content=b"x" * 100)
    assert (res.status_code, res.json()) == (200, {"read": 100}), "spooled body replays whole"


async def test_uploads_over_the_in_flight_cap_get_503(monkeypatch: Any) -> None:
    monkeypatch.setattr(upload_limit, "MAX_IN_FLIGHT", 0)
    async with _limited(monkeypatch, 100) as client:
        res = await client.post(upload_limit.UPLOAD_PATH, content=b"x")
        other = await client.post("/elsewhere", content=b"x")
    assert res.status_code == 503, "no slot free: asked to retry"
    assert other.status_code == 200, "other paths are not counted"


async def test_a_body_that_never_finishes_is_cut_off_and_frees_its_slot(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(upload_limit, "READ_TIMEOUT_SECONDS", 0.05)
    sent: list[Message] = []

    async def receive() -> Message:
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": upload_limit.UPLOAD_PATH,
        "headers": [],
    }
    await upload_limit.UploadLimitMiddleware(_echo)(scope, receive, send)
    assert sent[0]["status"] == 408, "a stalled body is refused"
    assert upload_limit._in_flight == 0, "the slot is released"  # pyright: ignore[reportPrivateUsage]
