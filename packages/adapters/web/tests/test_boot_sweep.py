"""The boot sweep that ends web turns whose process died mid-flight."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from daimon.adapters.web.boot_sweep import (
    sweep_expired_web_sessions,
    sweep_expired_web_sessions_forever,
    sweep_orphaned_web_turns,
)
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import WebMessageBlocks
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    list_orphaned_turns,
    mark_turn_active,
)
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    list_recoverable_turn_card_intents,
)
from daimon.core.web_identity import thread_key
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import funded_workspace, new_thread


async def test_the_sweep_ends_what_a_dead_process_left_running(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    thread, _ = await new_thread(db_session_factory, ws)
    key, reply_id = thread_key(thread.id), uuid.uuid4()
    async with db_session_factory.begin() as db:
        row = await create_thread_session(
            db,
            tenant_id=ws.tenant_id,
            platform="web",
            thread_id=key,
            account_id=ws.person.account_id,
            ma_session_id="sesn_orphan",
        )
        await mark_turn_active(
            db,
            id=row.id,
            active_turn_message_id=str(reply_id),
            active_turn_channel_id=key,
            now=datetime.now(UTC),
        )
        await create_turn_card_intent(
            db,
            tenant_id=ws.tenant_id,
            platform="web",
            thread_id=key,
            turn_token=uuid.uuid4(),
            channel_id=key,
        )
        await chat_store.upsert_assistant_message(
            db,
            tenant_id=ws.tenant_id,
            thread_id=thread.id,
            message_id=reply_id,
            turn_id=uuid.uuid4(),
            content="Half",
            blocks=WebMessageBlocks(),
            status="running",
        )
    interrupted: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        interrupted.extend(e["type"] for e in json.loads(request.content)["events"])
        return httpx.Response(200, json={"data": []})

    await sweep_orphaned_web_turns(build_fake_anthropic(record), db_session_factory)

    assert interrupted == ["user.interrupt"], "the session must stop running"
    async with db_session_factory() as db:
        assert await list_orphaned_turns(db, platform="web") == []
        assert await list_recoverable_turn_card_intents(db, platform="web") == []
        [_, reply] = await chat_store.list_messages(db, thread_id=thread.id)
    assert (reply.status, reply.content) == ("interrupted", "Half")


async def test_the_session_sweep_deletes_only_expired_sign_ins(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    async with db_session_factory.begin() as db:
        for token, ttl in ((b"a" * 32, timedelta(days=1)), (b"b" * 32, timedelta(days=60))):
            await sessions_store.create_session(
                db,
                token_sha256=token,
                provider="oidc",
                issuer="",
                subject="sub",
                people=[ws.person],
                ttl=ttl,
                now=now - timedelta(days=30),
            )

    removed = await sweep_expired_web_sessions(db_session_factory, lambda: now)

    async with db_session_factory() as db:
        kept = await sessions_store.get_live_session(db, token_sha256=b"b" * 32, now=now)
        gone = await sessions_store.get_live_session(
            db, token_sha256=b"a" * 32, now=now - timedelta(days=30)
        )
    assert removed == 1, "only the session past its expiry is swept"
    assert kept is not None, "a session still inside its lifetime survives"
    assert gone is None, "the expired row is deleted, not just ignored"


async def test_the_session_sweep_loop_sweeps_at_once_and_stops_when_cancelled(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await funded_workspace(db_session_factory)
    async with db_session_factory.begin() as db:
        await sessions_store.create_session(
            db,
            token_sha256=b"c" * 32,
            provider="oidc",
            issuer="",
            subject="sub",
            people=[ws.person],
            ttl=timedelta(seconds=1),
            now=datetime.now(UTC) - timedelta(days=1),
        )

    task = asyncio.create_task(sweep_expired_web_sessions_forever(db_session_factory, every_s=3600))
    await asyncio.sleep(0.5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    removed_again = await sweep_expired_web_sessions(db_session_factory)
    assert removed_again == 0, "the loop's first pass already deleted the expired sign-in"
