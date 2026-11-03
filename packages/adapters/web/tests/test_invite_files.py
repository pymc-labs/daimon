"""Whether a person may attach files in web chat: members always, visitors by invite."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from daimon.adapters.web.caller import FILES_OFF
from daimon.core.stores import web_invites
from daimon.core.stores.domain import WebPersonRow
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import build_turns, funded_workspace, signed_in, visitor_of
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_HEADERS = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_COOKIE = "__Host-daimon_web"
_FORM = {"file": ("notes.txt", b"hello", "text/plain")}


async def _as(
    client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession], person: WebPersonRow
) -> None:
    client.cookies.clear()
    client.cookies.set(_COOKIE, await signed_in(factory, person), domain="web.example.com")


async def _send(client: httpx.AsyncClient, attachments: list[dict[str, object]]) -> httpx.Response:
    sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()["id"]
    return await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"id": str(uuid.uuid4()), "content": "see", "attachments": attachments},
    )


async def test_a_visitor_without_files_cannot_upload_or_send_an_earlier_upload(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    guest = await visitor_of(db_session_factory, ws, allow_files=True, agent_ids=(AGENT_ID,))
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    async with web_client(web_settings(), sessionmaker=db_session_factory, turns=turns) as client:
        client.headers.update(_HEADERS)
        await _as(client, db_session_factory, guest)
        held = (await client.post("/api/chat/uploads", files=_FORM)).json()
        async with db_session_factory.begin() as db:
            assert guest.invite_id is not None
            await web_invites.set_allow_files(
                db, tenant_id=ws.tenant_id, invite_id=guest.invite_id, allow=False
            )
        upload = await client.post("/api/chat/uploads", files=_FORM)
        sent = await _send(client, [held])
        plain = await _send(client, [])
        me = (await client.get("/api/me")).json()

    assert (upload.status_code, upload.json()["detail"]) == (403, FILES_OFF)
    assert (sent.status_code, sent.json()["detail"]) == (403, FILES_OFF), (
        "an upload made while files were on cannot be sent once they are off"
    )
    assert plain.status_code == 200, "chatting without files is unaffected"
    assert "files.attach" not in me["capabilities"]


async def test_a_visitor_with_files_uploads_and_sends_them(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    guest = await visitor_of(db_session_factory, ws, allow_files=True, agent_ids=(AGENT_ID,))
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    async with web_client(web_settings(), sessionmaker=db_session_factory, turns=turns) as client:
        client.headers.update(_HEADERS)
        await _as(client, db_session_factory, guest)
        upload = await client.post("/api/chat/uploads", files=_FORM)
        sent = await _send(client, [upload.json()])
        me = (await client.get("/api/me")).json()

    assert upload.status_code == 200, upload.text
    assert sent.status_code == 200, sent.text
    assert me["capabilities"] == ["chat.use", "files.attach"], "the app reads it as a capability"


async def test_a_visitor_whose_invite_was_revoked_has_no_files(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    guest = await visitor_of(db_session_factory, ws, allow_files=True)
    assert guest.invite_id is not None
    async with db_session_factory.begin() as db:
        await web_invites.revoke_invite(
            db, tenant_id=ws.tenant_id, invite_id=guest.invite_id, now=datetime.now(UTC)
        )

    async with web_client(web_settings(), sessionmaker=db_session_factory) as client:
        client.headers.update(_HEADERS)
        await _as(client, db_session_factory, guest)
        upload = await client.post("/api/chat/uploads", files=_FORM)

    assert upload.status_code == 403, "a revoked invite is no files"


async def test_a_member_attaches_files_without_any_setting(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    async with web_client(web_settings(), sessionmaker=db_session_factory, turns=turns) as client:
        client.headers.update(_HEADERS)
        await _as(client, db_session_factory, ws.person)
        upload = await client.post("/api/chat/uploads", files=_FORM)
        sent = await _send(client, [upload.json()])
        me = (await client.get("/api/me")).json()

    assert (upload.status_code, sent.status_code) == (200, 200)
    assert "files.attach" in me["capabilities"]
