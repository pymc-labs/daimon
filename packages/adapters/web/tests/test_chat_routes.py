"""`/api/chat/*` as the web app calls it, over real turns."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from daimon.adapters.web import caller as caller_module
from daimon.adapters.web.turns import WebTurns
from daimon.core.output_delivery import DeliverableFile
from daimon.core.stores import file_uploads
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import WebMessageBlocks
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.testing.turn_router import AGENT_ID, AGENT_TEXT, build_turn_router
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import (
    Workspace,
    build_turns,
    funded_workspace,
    new_thread,
    one_session,
    signed_in,
    sse_frames,
)
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}


async def _client(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    ws: Workspace,
    turns: WebTurns,
) -> Any:
    client = web_client(web_settings(ws.tenant_id), sessionmaker=factory, turns=turns)
    return client


def _sign(client: httpx.AsyncClient, token: str) -> None:
    client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
    client.headers.update(_OWN)


async def test_a_chat_from_new_to_answered(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(db_session_factory, ws.person)

    with one_session():
        async with await _client(web_client, db_session_factory, ws, turns) as client:
            _sign(client, token)
            [agent] = (await client.get("/api/chat/agents")).json()
            assert (agent["id"], agent["name"]) == (AGENT_ID, DEFAULT_AGENT_NAME)
            session = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()
            assert (session["title"], session["agent"]["id"]) == ("New chat", AGENT_ID)
            sid, mid = session["id"], str(uuid.uuid4())

            res = await client.post(
                f"/api/chat/sessions/{sid}/messages",
                json={"id": mid, "content": "  How did   spend change?", "attachments": []},
            )
            assert res.headers["content-type"].startswith("text/event-stream"), res.text
            frames = sse_frames(res.text)
            again = await client.post(
                f"/api/chat/sessions/{sid}/messages", json={"id": mid, "content": "x"}
            )
            await turns.drain(10)
            detail = (await client.get(f"/api/chat/sessions/{sid}")).json()
            listed = (await client.get("/api/chat/sessions")).json()
            gone = await client.get(f"/api/chat/sessions/{sid}/stream")
            missed = await client.get(
                f"/api/chat/sessions/{sid}/stream", headers={"Last-Event-ID": frames[-2][0]}
            )

    epochs = {i.split(":")[0] for i, _ in frames}
    ids = [int(i.split(":")[1]) for i, _ in frames]
    assert len(epochs) == 1, "every event id carries its turn"
    assert ids == sorted(ids) and frames[-1][1]["type"] == "done", frames
    assert frames[-1][1]["message"]["content"] == AGENT_TEXT
    assert again.status_code == 200 and sse_frames(again.text)[-1] == frames[-1], (
        "a retry re-attaches"
    )
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert "resume_from" not in detail and detail["session"]["running"] is False
    assert [s["title"] for s in listed] == ["How did spend change?"]
    assert gone.status_code == 404, "nothing running"
    assert sse_frames(missed.text) == frames[-1:], "a reader who missed the end gets it"


async def test_a_running_turn_is_resumed_from_the_last_event_seen(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(db_session_factory, ws.person)
    started = asyncio.Event()

    async def run_until_stopped(*_args: object, **kw: Any) -> SimpleNamespace:
        await kw["lifecycle"].on_render(TurnState(content=[TextBlock(kind="text", text="Half")]))
        started.set()
        await kw["cancel"].wait()
        return SimpleNamespace(state=TurnState())

    with one_session(), patch("daimon.adapters.web.turns.run_prepared_turn", run_until_stopped):
        async with await _client(web_client, db_session_factory, ws, turns) as client:
            _sign(client, token)
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            body = {"id": str(uuid.uuid4()), "content": "hi"}
            post = asyncio.create_task(client.post(f"/api/chat/sessions/{sid}/messages", json=body))
            await started.wait()
            busy = await client.post(
                f"/api/chat/sessions/{sid}/messages", json={"id": str(uuid.uuid4()), "content": "b"}
            )
            detail = (await client.get(f"/api/chat/sessions/{sid}")).json()
            newest = (await client.get(f"/api/chat/sessions/{sid}", params={"limit": 1})).json()
            earlier = (
                await client.get(f"/api/chat/sessions/{sid}", params={"before": newest["next"]})
            ).json()
            resumed = asyncio.create_task(
                client.get(
                    f"/api/chat/sessions/{sid}/stream",
                    headers={"Last-Event-ID": detail["resume_from"]},
                )
            )
            foreign = asyncio.create_task(
                client.get(
                    f"/api/chat/sessions/{sid}/stream",
                    headers={"Last-Event-ID": "0123456789ab:1"},
                )
            )
            await asyncio.sleep(0.05)
            stop = await client.post(f"/api/chat/sessions/{sid}/stop")
            first, later, other = await post, await resumed, await foreign
            await turns.drain(10)

    assert busy.status_code == 409, "one turn at a time"
    live = detail["messages"][-1]
    assert (live["content"], live["streaming"]) == ("Half", True)
    assert detail["session"]["running"] is True
    assert [m["content"] for m in earlier["messages"]] == ["hi"] and "resume_from" not in earlier, (
        "an earlier page carries no live reply"
    )
    assert stop.json() == {"ok": True}
    later_frames = sse_frames(later.text)
    assert [e["type"] for _, e in later_frames] == ["done"], "only what came after"
    assert later_frames[-1] == sse_frames(first.text)[-1]
    assert [e["type"] for _, e in sse_frames(other.text)][-1] == "done", (
        "another turn's id is answered with the reply as it stands, not a stray tail"
    )
    assert sse_frames(other.text)[0][1]["type"] != "delta", "it starts from a snapshot"
    assert later_frames[-1][1]["message"]["stopped"] is True


async def test_deleting_a_chat_mid_turn_stops_the_turn_and_drops_its_files(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(db_session_factory, ws.person)
    started = asyncio.Event()
    stopped: list[bool] = []

    async def run_until_stopped(*_args: object, **kw: Any) -> SimpleNamespace:
        tool = ToolUseBlock(
            kind="tool_use",
            id="t1",
            type="agent.tool_use",
            name="bash",
            input={},
            status="complete",
        )
        state = TurnState(content=[tool, TextBlock(kind="text", text="Done")])
        await kw["lifecycle"].on_render(state)
        started.set()
        await kw["cancel"].wait()
        stopped.append(True)
        # The turn ended on its own as the stop landed: its files are swept all the same.
        kw["cancel"].clear()
        return SimpleNamespace(state=state, ma_session_id="sesn_web")

    async def sweep(*_args: object, post: Any, **_kw: Any) -> int:
        file = DeliverableFile(
            file_id="f1", filename="r.csv", mime_type="text/csv", size_bytes=4, content=b"a,b\n"
        )
        await post(file)
        return 1

    with (
        one_session(),
        patch("daimon.adapters.web.turns.run_prepared_turn", run_until_stopped),
        patch("daimon.adapters.web.turns.sweep_session_outputs", sweep),
    ):
        async with await _client(web_client, db_session_factory, ws, turns) as client:
            _sign(client, token)
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            body = {"id": str(uuid.uuid4()), "content": "hi"}
            post = asyncio.create_task(client.post(f"/api/chat/sessions/{sid}/messages", json=body))
            await started.wait()
            deleted = await client.delete(f"/api/chat/sessions/{sid}")
            await post
            await turns.drain(10)
    async with db_session_factory() as db:
        held = await file_uploads.person_upload_bytes(db, person_id=ws.person.id)
    assert deleted.json() == {"ok": True}, "the chat is deleted"
    assert stopped == [True], "the delete stopped the running turn"
    assert held == 0, "a file stored for a reply nobody keeps is deleted"


async def test_someone_elses_chat_is_not_found(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    other = await funded_workspace(db_session_factory)
    mine, theirs = await signed_in(db_session_factory, ws.person), None

    async with await _client(web_client, db_session_factory, ws, turns) as client:
        _sign(client, mine)
        sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()["id"]
        unknown = await client.post("/api/chat/sessions", json={"agent_id": "ag_nope"})
    theirs = await signed_in(db_session_factory, other.person)
    async with await _client(web_client, db_session_factory, other, turns) as client:
        _sign(client, theirs)
        seen = await client.get(f"/api/chat/sessions/{sid}")
        renamed = await client.patch(f"/api/chat/sessions/{sid}", json={"title": "mine"})
        deleted = await client.delete(f"/api/chat/sessions/{sid}")

    assert unknown.status_code == 404
    assert [r.status_code for r in (seen, renamed, deleted)] == [404, 404, 404]


async def test_rename_feedback_and_delete(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(db_session_factory, ws.person)

    with one_session():
        async with await _client(web_client, db_session_factory, ws, turns) as client:
            _sign(client, token)
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            res = await client.post(
                f"/api/chat/sessions/{sid}/messages",
                json={"id": str(uuid.uuid4()), "content": "hi"},
            )
            reply_id = sse_frames(res.text)[-1][1]["message"]["id"]
            await turns.drain(10)
            renamed = await client.patch(f"/api/chat/sessions/{sid}", json={"title": " Q3  spend "})
            up = await client.post(f"/api/chat/messages/{reply_id}/feedback", json={"rating": "up"})
            rated = (await client.get(f"/api/chat/sessions/{sid}")).json()["messages"][-1]
            await client.post(f"/api/chat/messages/{reply_id}/feedback", json={"rating": "none"})
            cleared = (await client.get(f"/api/chat/sessions/{sid}")).json()["messages"][-1]
            deleted = await client.delete(f"/api/chat/sessions/{sid}")
            after = await client.get(f"/api/chat/sessions/{sid}")

    assert renamed.json()["title"] == "Q3 spend"
    assert up.json() == {"ok": True} and rated["feedback"] == "up"
    assert cleared["feedback"] is None
    assert deleted.json() == {"ok": True} and after.status_code == 404


async def test_an_upload_is_kept_for_a_later_message(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(db_session_factory, ws.person)

    async with await _client(web_client, db_session_factory, ws, turns) as client:
        _sign(client, token)
        res = await client.post(
            "/api/chat/uploads", files={"file": ("notes.txt", b"hello", "text/plain")}
        )

    body = res.json()
    assert (body["name"], body["size"]) == ("notes.txt", 5), res.text
    async with db_session_factory() as db:
        row = await file_uploads.get_upload(db, tenant_id=ws.tenant_id, handle_id=body["id"])
    assert row is not None and row.content == b"hello"


async def test_a_long_chat_is_read_from_its_end_and_paged_back(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    thread, _ = await new_thread(db_session_factory, ws, "m0")
    for n in range(1, 5):
        async with db_session_factory.begin() as db:
            await chat_store.insert_user_message(
                db,
                tenant_id=ws.tenant_id,
                thread_id=thread.id,
                message_id=uuid.uuid4(),
                content=f"m{n}",
                blocks=WebMessageBlocks(),
            )
    other, _ = await new_thread(db_session_factory, ws, "other")
    token = await signed_in(db_session_factory, ws.person)

    async with await _client(web_client, db_session_factory, ws, turns) as client:
        _sign(client, token)
        url = f"/api/chat/sessions/{thread.id}"
        newest = (await client.get(url, params={"limit": 2})).json()
        earlier = (await client.get(url, params={"limit": 2, "before": newest["next"]})).json()
        oldest = (await client.get(url, params={"limit": 2, "before": earlier["next"]})).json()
        whole = (await client.get(url)).json()
        chats = await client.get("/api/chat/sessions", params={"limit": 1})
        rest = await client.get(
            "/api/chat/sessions",
            params={"limit": 1, "cursor": chats.headers["X-Next-Cursor"]},
        )

    texts = lambda page: [m["content"] for m in page["messages"]]  # noqa: E731
    assert texts(newest) == ["m3", "m4"], "the newest messages, oldest first"
    assert texts(earlier) == ["m1", "m2"], "the page before follows"
    assert texts(oldest) == ["m0"] and "next" not in oldest, "the first page of a chat has no next"
    assert texts(whole) == ["m0", "m1", "m2", "m3", "m4"] and "next" not in whole, (
        "a chat that fits in a page comes whole, as before"
    )
    assert [c["id"] for c in chats.json()] == [str(other.id)], "chats list newest first"
    assert [c["id"] for c in rest.json()] == [str(thread.id)], "the next page holds the other chat"
    assert "X-Next-Cursor" not in rest.headers, "the last page names no next one"


async def test_a_send_reads_who_the_person_acts_as_once(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(db_session_factory, ws.person)
    asked: list[str] = []
    real = caller_module.principal_of

    async def counting(*args: Any, **kwargs: Any) -> Any:
        asked.append("principal")
        return await real(*args, **kwargs)

    with one_session(), patch.object(caller_module, "principal_of", counting):
        async with await _client(web_client, db_session_factory, ws, turns) as client:
            _sign(client, token)
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            asked.clear()
            sent = await client.post(
                f"/api/chat/sessions/{sid}/messages",
                json={"id": str(uuid.uuid4()), "content": "hi", "attachments": []},
            )
            await turns.drain(10)

    assert sent.status_code == 200, sent.text
    assert len(asked) == 1, f"the chat check and the turn share one lookup, not {len(asked)}"
