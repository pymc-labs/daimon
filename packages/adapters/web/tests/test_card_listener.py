"""Credential cards: raised from the MCP process's notice, answered in the chat."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from daimon.adapters.web.card_listener import NoticeListener
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.agent_files import get_agent_file
from daimon.core.stores.credential_requests import (
    create_credential_request,
    get_credential_request_by_idempotency_key,
)
from daimon.core.stores.domain import CredentialRequestRow, WebMessageBlocks
from daimon.core.stores.web_notices import WebCredentialNotice, send_web_notice
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .chat_support import (
    Workspace,
    build_turns,
    funded_workspace,
    one_session,
    signed_in,
    sse_frames,
)
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

KEY = "TOGGL_API_TOKEN"
VALUE = "tok-value-1234"


def _text(said: str) -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=said)])


async def _request(
    factory: async_sessionmaker[AsyncSession],
    ws: Workspace,
    thread_id: uuid.UUID,
    requested_work: str | None = "log my hours",
) -> CredentialRequestRow:
    now = datetime.now(UTC)
    async with factory.begin() as session:
        return await create_credential_request(
            session,
            token=mint_request_token(),
            kind="env",
            tenant_id=ws.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=ws.tenant_id, ma_agent_id=AGENT_ID),
            account_id=ws.person.account_id,
            target=KEY,
            mcp_server_url=None,
            requester_platform_user_id=str(ws.person.id),
            channel_id=f"web:{thread_id}",
            expires_at=now + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=AGENT_ID,
            target_name="Daimon",
            requested_work=requested_work,
            responder_name="Daimon",
            platform="web",
            parent_channel_id=f"web:{ws.person.id}",
            origin_thread_id=f"web:{thread_id}",
        )


async def _chat(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    script: Any,
    requested_work: str | None = "log my hours",
) -> tuple[Any, list[str], Workspace]:
    """Run `script(client, sid, turns)`; the first turn raises a credential card."""
    ws = await funded_workspace(factory)
    turns = build_turns(factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(factory, ws.person)
    asked: list[str] = []

    async def turn(*_args: object, **kw: Any) -> SimpleNamespace:
        asked.append(kw["user_message"])
        if len(asked) == 1:
            thread_id = uuid.UUID(kw["thread_id"].removeprefix("web:"))
            row = await _request(factory, ws, thread_id, requested_work)
            notice = WebCredentialNotice(thread_id=kw["thread_id"], request_id=row.idempotency_key)
            await turns.credentials.raise_card(notice)
            said = _text("I need a key.")
        else:
            said = _text("Logged.")
        await kw["lifecycle"].on_render(said)
        return SimpleNamespace(state=said)

    bound: list[Any] = []

    async def bind_once(*args: Any, **kw: Any) -> Any:
        # The saved key would refresh the session's mounts; the follow-up reuses the bind.
        if not bound:
            bound.append(await bind_session(*args, **kw))
        return bound[0]

    with (
        one_session(),
        patch("daimon.adapters.web.turns.run_prepared_turn", turn),
        patch("daimon.adapters.web.turns.bind_session", bind_once),
    ):
        async with web_client(
            web_settings(ws.tenant_id), sessionmaker=factory, turns=turns
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update({"X-Daimon-Web": "1", "Origin": PUBLIC_URL})
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            first = await client.post(
                f"/api/chat/sessions/{sid}/messages",
                json={"id": str(uuid.uuid4()), "content": "log my hours"},
            )
            result = await script(client, sid, first)
            await turns.drain(10)
    return result, asked, ws


async def test_saving_stores_the_value_and_resumes_the_work(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str, first: httpx.Response) -> Any:
        events = [e for _, e in sse_frames(first.text)]
        card = events[-1]["message"]["card"]
        later = await client.post(
            f"/api/chat/sessions/{sid}/cards/{card['id']}", json={"answer": "save", "value": VALUE}
        )
        again = await client.post(
            f"/api/chat/sessions/{sid}/cards/{card['id']}", json={"answer": "save", "value": "x"}
        )
        await asyncio.sleep(0.05)
        detail = (await client.get(f"/api/chat/sessions/{sid}")).json()
        return events, card, later, again, detail

    (events, card, later, again, detail), asked, ws = await _chat(
        web_client, db_session_factory, tmp_path, script
    )

    assert "card" in [e["type"] for e in events[:-1]], "the card shows while the turn runs"
    assert (card["kind"], card["state"], card["key"]) == ("credential", "pending", KEY), card
    assert card["headline"] == f"Daimon needs {KEY}", "the card names the agent and the key"
    assert sse_frames(later.text)[-1][1]["message"]["content"] == "Logged.", later.text
    assert asked[-1].endswith(f"[form] {KEY} was saved.\n\nlog my hours"), "the work resumes"
    assert again.status_code == 409, "a form is used once"
    replies = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert replies[0]["card"]["state"] == "saved", "the stored card reads as saved"
    agent_id = derive_agent_uuid(tenant_id=ws.tenant_id, ma_agent_id=AGENT_ID)
    async with db_session_factory() as session:
        saved = await get_agent_file(session, tenant_id=ws.tenant_id, agent_id=agent_id, key=KEY)
        leaked = await session.scalar(
            text("SELECT count(*) FROM web_messages WHERE content LIKE :v OR blocks::text LIKE :v"),
            {"v": f"%{VALUE}%"},
        )
    assert saved is not None and saved.content == VALUE, "the value is in the agent's vault"
    assert leaked == 0, "the value is never in a message"


async def test_skipping_settles_the_card_and_starts_no_turn(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str, first: httpx.Response) -> Any:
        card = sse_frames(first.text)[-1][1]["message"]["card"]
        answered = await client.post(
            f"/api/chat/sessions/{sid}/cards/{card['id']}", json={"answer": "skip"}
        )
        await asyncio.sleep(0.05)
        return card, answered

    (card, answered), asked, _ = await _chat(web_client, db_session_factory, tmp_path, script)

    assert len(asked) == 1, "a skip starts no billed follow-up turn"
    settled = answered.json()
    assert settled["type"] == "settled", "the answer is the updated message, not a stream"
    assert settled["message"]["card"]["state"] == "skipped", "the card reads as skipped"
    async with db_session_factory() as session:
        row = await get_credential_request_by_idempotency_key(
            session, idempotency_key=uuid.UUID(card["id"])
        )
    assert row is not None and row.outcome == "declined", "the request is spent as declined"


async def test_saving_a_key_nobody_waits_on_starts_no_turn(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str, first: httpx.Response) -> Any:
        card = sse_frames(first.text)[-1][1]["message"]["card"]
        answered = await client.post(
            f"/api/chat/sessions/{sid}/cards/{card['id']}", json={"answer": "save", "value": VALUE}
        )
        await asyncio.sleep(0.05)
        return answered

    answered, asked, ws = await _chat(
        web_client, db_session_factory, tmp_path, script, requested_work=None
    )

    assert len(asked) == 1, "with no work waiting, saving starts no turn"
    assert answered.json()["message"]["card"]["state"] == "saved", "the card reads as saved"
    agent_id = derive_agent_uuid(tenant_id=ws.tenant_id, ma_agent_id=AGENT_ID)
    async with db_session_factory() as session:
        saved = await get_agent_file(session, tenant_id=ws.tenant_id, agent_id=agent_id, key=KEY)
    assert saved is not None and saved.content == VALUE, "the value is still stored"


async def test_only_the_requester_may_answer(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str, first: httpx.Response) -> Any:
        card = sse_frames(first.text)[-1][1]["message"]["card"]
        async with db_session_factory.begin() as session:
            await session.execute(
                text("UPDATE credential_requests SET requester_platform_user_id = 'someone'")
            )
        return await client.post(
            f"/api/chat/sessions/{sid}/cards/{card['id']}", json={"answer": "save", "value": VALUE}
        )

    refused, asked, _ = await _chat(web_client, db_session_factory, tmp_path, script)

    assert refused.status_code == 409, "another person's form is refused"
    assert len(asked) == 1, "no follow-up turn runs"


async def test_a_card_after_the_turn_lands_on_the_last_reply(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    async with db_session_factory.begin() as session:
        thread = await chat_store.create_thread(
            session,
            tenant_id=ws.tenant_id,
            person_id=ws.person.id,
            agent_id=AGENT_ID,
            agent_name="Daimon",
            title="t",
        )
        await chat_store.upsert_assistant_message(
            session,
            tenant_id=ws.tenant_id,
            thread_id=thread.id,
            message_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            content="I need a key.",
            blocks=WebMessageBlocks(),
            status="done",
            duration_ms=1,
        )
    row = await _request(db_session_factory, ws, thread.id)

    await turns.credentials.raise_card(
        WebCredentialNotice(thread_id=f"web:{thread.id}", request_id=row.idempotency_key)
    )

    async with db_session_factory() as session:
        reply = await chat_store.get_last_reply(session, thread_id=thread.id)
    assert reply is not None and reply.blocks.card is not None, "the reply carries the card"
    assert reply.blocks.card.id == str(row.idempotency_key), "the card is the request's"


async def test_the_listener_hands_on_notices_and_ignores_unknown_ones(
    db_nullpool_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    heard: list[WebCredentialNotice] = []

    async def handle(notice: WebCredentialNotice) -> None:
        heard.append(notice)

    listener = NoticeListener(db_nullpool_engine, handle, retry_s=0.05)
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(listener.listening.wait(), 10)
        sent = WebCredentialNotice(thread_id=f"web:{uuid.uuid4()}", request_id=uuid.uuid4())
        async with db_session_factory.begin() as session:
            await session.execute(text("SELECT pg_notify('daimon_web', '{\"kind\":\"other\"}')"))
            await send_web_notice(session, sent)
        for _ in range(100):
            if heard:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
    assert heard == [sent], "the known notice is handled, the unknown one dropped"
