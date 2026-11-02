"""`/api/chat/*`: the person's chats, their messages, and the turns that answer them."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web import directory
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.cards import CardChoice
from daimon.adapters.web.chat_json import agent_json, message_json, session_json
from daimon.adapters.web.credential_cards import CredentialRefused
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.files import PERSON_UPLOAD_QUOTA_BYTES, download, open_attachment
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.stream import OTHER_TURN, Event, sse_response
from daimon.adapters.web.turns import WebTurns
from daimon.core.cursor import MAX_PAGE
from daimon.core.stores import file_uploads
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores import web_workspace_settings as workspace_settings
from daimon.core.stores.domain import (
    Role,
    WebAttachment,
    WebMessageBlocks,
    WebMessageRow,
    WebThreadRow,
)
from daimon.core.stores.message_feedback import record_vote
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.web_identity import WebPrincipal, place_of, thread_key
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

NEW_TITLE = "New chat"
DEFAULT_MESSAGES = 200
#: A follow-up turn's unsaved message sorts after every saved one, for its replay.
FOLLOW_UP_SEQ = 2**62
_TITLE_CHARS = 56


class NewSession(BaseModel):
    agent_id: str


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class NewMessage(BaseModel):
    id: uuid.UUID
    content: str = Field(max_length=100_000)
    attachments: list[WebAttachment] = Field(default_factory=list[WebAttachment], max_length=10)


class CardAnswer(BaseModel):
    answer: CardChoice | Literal["save", "skip"]
    value: str | None = Field(default=None, max_length=100_000)


class Rating(BaseModel):
    rating: Literal["up", "down", "none"]


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="This chat no longer exists.")


Caller = Annotated[WebCaller, Depends(requires("chat.use"))]


def _title_of(content: str, attachments: list[WebAttachment]) -> str:
    text = " ".join(content.split()) or (attachments[0].name if attachments else "")
    return text[:_TITLE_CHARS] or NEW_TITLE


def _live_message(reply_id: uuid.UUID, events: list[Event], created_at: str) -> dict[str, Any]:
    """The running reply as the browser would have it after `events`."""
    steps: dict[str, Any] = {}
    text = ""
    for event in events:
        if event["type"] == "step":
            steps[event["step"]["id"]] = event["step"]
        elif event["type"] == "text":
            text = event["text"]
    return {
        "id": str(reply_id),
        "role": "assistant",
        "content": text,
        "created_at": created_at,
        "steps": list(steps.values()),
        "streaming": True,
    }


def chat_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/chat")

    def turns() -> WebTurns:
        if runtime.turns is None:
            raise HTTPException(status_code=503, detail="Chat is not available right now.")
        return runtime.turns

    async def chattable(
        caller: WebCaller, *, only: str | None = None
    ) -> list[BetaManagedAgentsAgent]:
        """The agents this person may chat with on the web, the workspace's default first;
        with `only`, just that agent if they may."""
        deps, tenant_id = turns().deps, caller.person.tenant_id
        listed = await runtime.agents.agents(deps.anthropic, tenant_id)
        if only is not None:
            listed = [a for a in listed if a.id == only]
        async with runtime.sessionmaker() as db:
            principal = await caller.principal(db)
            allowed = await directory.chattable(
                db, listed, person=caller.person, role=caller.role, principal=principal
            )
            chosen = await workspace_settings.get_settings(db, tenant_id=tenant_id)
        name = deps.deployment_default.agent_name

        def first(agent: BetaManagedAgentsAgent) -> bool:
            if chosen.default_agent is not None:
                return agent.id == chosen.default_agent
            return agent.name == name

        return sorted(allowed, key=lambda a: (not first(a), a.name.lower()))

    async def thread_of(db: AsyncSession, caller: WebCaller, session_id: uuid.UUID) -> WebThreadRow:
        thread = await chat_store.get_thread(db, person_id=caller.person.id, thread_id=session_id)
        if thread is None:
            raise _not_found()
        return thread

    async def require_own_uploads(caller: WebCaller, attachments: list[WebAttachment]) -> None:
        """Every attachment is a landed upload of this person's own."""
        person = caller.person
        async with runtime.sessionmaker() as db:
            for attachment in attachments:
                held = await file_uploads.get_person_upload(
                    db, tenant_id=person.tenant_id, person_id=person.id, handle_id=attachment.id
                )
                if held is None:
                    raise HTTPException(status_code=422, detail="A file can't be attached.")

    async def stop_turn(caller: WebCaller, thread: WebThreadRow) -> None:
        if runtime.turns is None:
            return
        async with runtime.sessionmaker() as db:
            principal = await caller.principal(db)
        await runtime.turns.stop(thread.id, principal.external_id)

    def is_running(thread_id: uuid.UUID) -> bool:
        stream = runtime.turns.streams.running(thread_id) if runtime.turns else None
        return stream is not None and not stream.waiting

    def agent_of(caller: WebCaller, agent_id: str) -> BetaManagedAgentsAgent | None:
        return runtime.agents.cached(caller.person.tenant_id, agent_id)

    async def faces(
        caller: WebCaller, agents: list[BetaManagedAgentsAgent | None]
    ) -> dict[str, str]:
        """The avatar of each of these agents that has one, by agent id."""
        known = [a for a in agents if a is not None]
        return await avatars_of(runtime, caller.person.tenant_id, known)

    @router.get("/agents")
    async def agents(caller: Caller) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        chosen = await chattable(caller)
        avatars = await faces(caller, list(chosen))
        return [agent_json(agent, avatar=avatars.get(agent.id, "")) for agent in chosen]

    @router.get("/sessions")
    async def sessions(  # pyright: ignore[reportUnusedFunction]
        caller: Caller,
        response: Response,
        page: Annotated[PageRequest, Depends(page_of(100))],
    ) -> list[dict[str, Any]]:
        async with runtime.sessionmaker() as db:
            found = await chat_store.list_threads(
                db, person_id=caller.person.id, limit=page.limit + 1, before=page.cursor
            )
        threads = cut(found, page, response, lambda t: (t.updated_at, t.id))
        listed = {t.id: agent_of(caller, t.agent_id) for t in threads}
        avatars = await faces(caller, list(listed.values()))
        return [
            session_json(
                t, listed[t.id], running=is_running(t.id), avatar=avatars.get(t.agent_id, "")
            )
            for t in threads
        ]

    @router.post("/sessions")
    async def create(body: NewSession, caller: Caller) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        agent = next((a for a in await chattable(caller) if a.id == body.agent_id), None)
        if agent is None:
            raise HTTPException(status_code=404, detail="This agent isn't available to you.")
        async with runtime.sessionmaker.begin() as db:
            thread = await chat_store.create_thread(
                db,
                tenant_id=caller.person.tenant_id,
                person_id=caller.person.id,
                agent_id=agent.id,
                agent_name=agent.name,
                title=NEW_TITLE,
            )
            await create_binding(
                db,
                tenant_id=thread.tenant_id,
                platform="web",
                parent_channel_id=place_of(caller.person.id),
                thread_id=thread_key(thread.id),
                responder_ma_agent_id=agent.id,
                responder_name=agent.name,
                creator_account_id=caller.person.account_id,
                kind="web",
            )
        return session_json(thread, agent, avatar=(await faces(caller, [agent])).get(agent.id, ""))

    @router.get("/sessions/{session_id}")
    async def detail(  # pyright: ignore[reportUnusedFunction]
        session_id: uuid.UUID,
        caller: Caller,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_MESSAGES,
        before: Annotated[int | None, Query(ge=0)] = None,
    ) -> dict[str, Any]:
        """The newest `limit` messages, oldest first; `next` is the `before` for earlier ones."""
        async with runtime.sessionmaker() as db:
            thread = await thread_of(db, caller, session_id)
            rows = await chat_store.list_messages(
                db, thread_id=thread.id, limit=limit + 1, before_seq=before
            )
        earlier = len(rows) > limit
        rows = rows[1:] if earlier else rows
        messages = [message_json(row) for row in rows]
        out: dict[str, Any] = {"messages": messages}
        if earlier:
            out["next"] = rows[0].seq
        stream = runtime.turns.streams.running(thread.id) if runtime.turns else None
        if (
            before is None
            and stream is not None
            and not stream.waiting
            and stream.reply_id is not None
        ):
            # The live reply and the event it stands at, read together; an earlier page has none.
            reply_id = str(stream.reply_id)
            created = next((m["created_at"] for m in messages if m["id"] == reply_id), None)
            live = _live_message(
                stream.reply_id, stream.snapshot(), created or runtime.now().isoformat()
            )
            messages[:] = [m for m in messages if m["id"] != reply_id] + [live]
            out["resume_from"] = stream.event_id(stream.last_id)
        agent = agent_of(caller, thread.agent_id)
        out["session"] = session_json(
            thread,
            agent,
            running="resume_from" in out,
            avatar=(await faces(caller, [agent])).get(thread.agent_id, ""),
        )
        return out

    @router.patch("/sessions/{session_id}")
    async def rename(session_id: uuid.UUID, body: Rename, caller: Caller) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        title = " ".join(body.title.split())
        if not title:
            raise HTTPException(status_code=422, detail="Give the chat a name.")
        async with runtime.sessionmaker.begin() as db:
            thread = await thread_of(db, caller, session_id)
            await chat_store.update_thread(db, thread_id=thread.id, title=title)
        thread = thread.model_copy(update={"title": title})
        agent = agent_of(caller, thread.agent_id)
        return session_json(
            thread,
            agent,
            running=is_running(thread.id),
            avatar=(await faces(caller, [agent])).get(thread.agent_id, ""),
        )

    @router.delete("/sessions/{session_id}")
    async def delete(session_id: uuid.UUID, caller: Caller) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker.begin() as db:
            thread = await thread_of(db, caller, session_id)
            await chat_store.delete_thread(db, thread_id=thread.id, now=runtime.now())
        await stop_turn(caller, thread)
        return {"ok": True}

    @router.post("/sessions/{session_id}/messages")
    async def send(session_id: uuid.UUID, body: NewMessage, caller: Caller) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        live = turns()
        if live.draining:
            raise HTTPException(status_code=503, detail="Restarting. Try again in a moment.")
        if not body.content.strip() and not body.attachments:
            raise HTTPException(status_code=422, detail="Write a message first.")
        async with runtime.sessionmaker() as db:
            thread = await thread_of(db, caller, session_id)
        current = live.streams.get(thread.id)
        if current is not None and current.message_id == body.id:
            return sse_response(current)  # a retried post re-attaches
        if not await chattable(caller, only=thread.agent_id):
            raise HTTPException(status_code=403, detail="This agent isn't available to you.")
        if live.streams.running(thread.id) is not None:
            raise HTTPException(status_code=409, detail="Wait for the answer to finish.")
        await require_own_uploads(caller, body.attachments)
        async with runtime.sessionmaker.begin() as db:
            try:
                message, new = await chat_store.insert_user_message(
                    db,
                    tenant_id=thread.tenant_id,
                    thread_id=thread.id,
                    message_id=body.id,
                    content=body.content,
                    blocks=WebMessageBlocks(attachments=tuple(body.attachments)),
                )
            except LookupError:
                raise HTTPException(
                    status_code=409, detail="This message was already sent."
                ) from None
            if not new:
                raise HTTPException(status_code=409, detail="This message was already sent.")
            if thread.title == NEW_TITLE:
                await chat_store.update_thread(
                    db, thread_id=thread.id, title=_title_of(body.content, body.attachments)
                )
            principal = await caller.principal(db)
        try:
            stream = live.start(
                person=caller.person, principal=principal, thread=thread, message=message
            )
        except RuntimeError:
            raise HTTPException(status_code=409, detail="Wait for the answer to finish.") from None
        return sse_response(stream)

    @router.get("/sessions/{session_id}/stream")
    async def resume(  # pyright: ignore[reportUnusedFunction]
        session_id: uuid.UUID,
        caller: Caller,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        async with runtime.sessionmaker() as db:
            thread = await thread_of(db, caller, session_id)
        stream = runtime.turns.streams.get(thread.id) if runtime.turns else None
        if stream is None:
            raise HTTPException(status_code=404, detail="Nothing is running in this chat.")
        # Another turn's id gets a snapshot while this one runs, and nothing once it ended.
        after = stream.position(last_event_id)
        # A finished turn is still readable by someone who missed its end.
        idle = stream.closed or stream.waiting
        if idle and (after in (0, OTHER_TURN) or after >= stream.last_id):
            raise HTTPException(status_code=404, detail="Nothing is running in this chat.")
        return sse_response(stream, after)

    @router.post("/sessions/{session_id}/stop")
    async def stop(session_id: uuid.UUID, caller: Caller) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            thread = await thread_of(db, caller, session_id)
        await stop_turn(caller, thread)
        return {"ok": True}

    async def answer_credential(
        caller: WebCaller,
        thread: WebThreadRow,
        principal: WebPrincipal,
        card_id: str,
        body: CardAnswer,
    ) -> Response:
        """Save or skip a credential card. A key with work waiting carries the chat on in a
        follow-up turn; anything else only settles the card."""
        live = turns()
        if live.draining:
            raise HTTPException(status_code=503, detail="Restarting. Try again in a moment.")
        if live.streams.running(thread.id) is not None:
            raise HTTPException(status_code=409, detail="Wait for the answer to finish.")
        try:
            answered = await live.credentials.answer(
                thread=thread,
                card_id=card_id,
                clicker=principal.external_id,
                clicker_platform=principal.platform,
                is_admin=principal.role is Role.ADMIN,
                value=(body.value or "") if body.answer == "save" else None,
            )
        except CredentialRefused as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from None
        if answered.follow_up is None:
            settled = None if answered.message is None else message_json(answered.message)
            return JSONResponse({"type": "settled", "message": settled})
        # Not saved: the follow-up's message is the agent's cue, not the person's words.
        message = WebMessageRow(
            id=uuid.uuid4(),
            tenant_id=thread.tenant_id,
            thread_id=thread.id,
            role="user",
            content=answered.follow_up,
            blocks=WebMessageBlocks(),
            turn_id=None,
            status="done",
            cost_usd=None,
            duration_ms=None,
            feedback=None,
            seq=FOLLOW_UP_SEQ,
            created_at=runtime.now(),
        )
        try:
            stream = live.start(
                person=caller.person, principal=principal, thread=thread, message=message
            )
        except RuntimeError:
            raise HTTPException(status_code=409, detail="Wait for the answer to finish.") from None
        return sse_response(stream)

    @router.post("/sessions/{session_id}/cards/{card_id}")
    async def answer(  # pyright: ignore[reportUnusedFunction]
        session_id: uuid.UUID, card_id: str, body: CardAnswer, caller: Caller
    ) -> Response:
        live = turns()
        async with runtime.sessionmaker() as db:
            thread = await thread_of(db, caller, session_id)
            principal = await caller.principal(db)
        clicker = principal.external_id
        if body.answer in ("save", "skip"):
            return await answer_credential(caller, thread, principal, card_id, body)
        stream = live.streams.running(thread.id)
        if stream is None:
            raise HTTPException(status_code=409, detail=live.cards.missing_message(card_id))
        after = stream.last_id  # the answer's reply follows the card
        refusal = await live.cards.answer(
            thread_id=thread.id, card_id=card_id, clicker=clicker, choice=body.answer
        )
        if refusal is not None:
            raise HTTPException(status_code=409, detail=refusal)
        return sse_response(stream, after)

    @router.post("/messages/{message_id}/feedback")
    async def feedback(message_id: uuid.UUID, body: Rating, caller: Caller) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        vote = None if body.rating == "none" else body.rating
        async with runtime.sessionmaker.begin() as db:
            row = await chat_store.get_message(
                db, person_id=caller.person.id, message_id=message_id
            )
            if row is None or row.role != "assistant":
                raise HTTPException(status_code=404, detail="This message no longer exists.")
            await chat_store.set_feedback(db, message_id=row.id, feedback=vote)
            if vote is not None:
                principal = await caller.principal(db)
                await record_vote(
                    db,
                    tenant_id=row.tenant_id,
                    platform="web",
                    message_id=str(row.id),
                    channel_id=thread_key(row.thread_id),
                    platform_user_id=principal.external_id,
                    account_id=caller.person.account_id,
                    ma_session_id=None,
                    vote=vote,
                )
        return {"ok": True}

    @router.post("/uploads")
    async def upload(file: Annotated[UploadFile, File()], caller: Caller) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        data = await file.read(file_uploads.MAX_UPLOAD_BYTES + 1)
        if len(data) > file_uploads.MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="This file is too large.")
        name = (file.filename or "file").rsplit("/", 1)[-1][:200] or "file"
        now = runtime.now()
        async with runtime.sessionmaker.begin() as db:
            await file_uploads.lock_person_uploads(db, person_id=caller.person.id)
            held = await file_uploads.person_upload_bytes(db, person_id=caller.person.id)
            if held + len(data) > PERSON_UPLOAD_QUOTA_BYTES:
                raise HTTPException(
                    status_code=413, detail="You've reached your file limit. Delete a chat."
                )
            row, token = await file_uploads.create_upload(
                db,
                tenant_id=caller.person.tenant_id,
                person_id=caller.person.id,
                title=name,
                display_filename=name,
                content_type=file.content_type or "application/octet-stream",
                now=now,
            )
            await file_uploads.store_upload_content(db, upload_token=token, data=data, now=now)
        return {"id": row.id, "name": name, "size": len(data)}

    @router.get("/attachments/{sealed}")
    async def attachment(sealed: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        """A shared upload for the agent's sandbox: the sealed link is the only key."""
        # Opened with the key the turn sealed it with.
        fernet = runtime.turns.deps.fernet if runtime.turns is not None else None
        opened = open_attachment(fernet, sealed) if fernet is not None else None
        if opened is None:
            raise HTTPException(status_code=404, detail="This link has expired.")
        tenant_id, handle = opened
        async with runtime.sessionmaker() as db:
            row = await file_uploads.get_upload(db, tenant_id=tenant_id, handle_id=handle)
        if row is None or row.content is None:
            raise HTTPException(status_code=404, detail="This file no longer exists.")
        return download(row.display_filename, row.content)

    @router.get("/files/{file_id}")
    async def output_file(file_id: str, caller: Caller) -> Response:  # pyright: ignore[reportUnusedFunction]
        """A file one of the person's replies carries."""
        async with runtime.sessionmaker() as db:
            held = await chat_store.holds_output_file(
                db, person_id=caller.person.id, file_id=file_id
            )
            row = (
                await file_uploads.get_upload(
                    db, tenant_id=caller.person.tenant_id, handle_id=file_id
                )
                if held
                else None
            )
        if row is None or row.content is None:
            raise HTTPException(status_code=404, detail="This file no longer exists.")
        return download(row.display_filename, row.content)

    return router
