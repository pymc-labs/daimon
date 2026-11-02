"""Real-Postgres tests for the web chat store."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core._models import FileUpload
from daimon.core.stores import web_chat as chat
from daimon.core.stores.domain import (
    WebAttachment,
    WebMessageBlocks,
    WebMessageStatus,
    WebOutputFile,
    WebStep,
    WebThreadRow,
)
from daimon.core.stores.file_uploads import create_upload
from daimon.testing.factories import make_account, make_tenant, make_web_person
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 5, 1, tzinfo=UTC)


async def _thread(db_session: AsyncSession, *, title: str = "New chat") -> WebThreadRow:
    tenant = await make_tenant(db_session)
    person = await make_web_person(
        db_session, tenant=tenant, account=await make_account(db_session, tenant=tenant)
    )
    return await chat.create_thread(
        db_session,
        tenant_id=tenant.id,
        person_id=person.id,
        agent_id="ag_1",
        agent_name="daimon",
        title=title,
    )


async def test_a_thread_is_seen_only_by_its_person(db_session: AsyncSession) -> None:
    thread = await _thread(db_session)

    assert await chat.get_thread(db_session, person_id=thread.person_id, thread_id=thread.id)
    assert await chat.get_thread(db_session, person_id=uuid.uuid4(), thread_id=thread.id) is None, (
        "someone else's chat is not found"
    )
    assert [t.id for t in await chat.list_threads(db_session, person_id=thread.person_id)] == [
        thread.id
    ]


async def test_threads_list_most_recent_first(db_session: AsyncSession) -> None:
    first = await _thread(db_session, title="first")
    second = await chat.create_thread(
        db_session,
        tenant_id=first.tenant_id,
        person_id=first.person_id,
        agent_id="ag_1",
        agent_name="daimon",
        title="second",
    )
    await chat.update_thread(db_session, thread_id=first.id, title="renamed", updated_at=_NOW)
    await chat.update_thread(db_session, thread_id=second.id, updated_at=_NOW.replace(year=2025))

    listed = await chat.list_threads(db_session, person_id=first.person_id)

    assert [t.title for t in listed] == ["renamed", "second"], "the active chat comes first"


async def test_a_retried_message_is_stored_once(db_session: AsyncSession) -> None:
    thread = await _thread(db_session)
    message_id = uuid.uuid4()

    async def send() -> bool:
        _, new = await chat.insert_user_message(
            db_session,
            tenant_id=thread.tenant_id,
            thread_id=thread.id,
            message_id=message_id,
            content="hi",
            blocks=WebMessageBlocks(),
        )
        return new

    assert await send() and not await send(), "the second post returns the first"
    assert len(await chat.list_messages(db_session, thread_id=thread.id)) == 1


async def test_a_message_id_from_another_thread_is_refused(db_session: AsyncSession) -> None:
    thread, other = await _thread(db_session), await _thread(db_session)
    message_id = uuid.uuid4()

    async def send(target: WebThreadRow) -> None:
        await chat.insert_user_message(
            db_session,
            tenant_id=target.tenant_id,
            thread_id=target.id,
            message_id=message_id,
            content="hi",
            blocks=WebMessageBlocks(),
        )

    await send(thread)
    with pytest.raises(LookupError):
        await send(other)


async def test_a_reply_is_rewritten_until_it_ends(db_session: AsyncSession) -> None:
    thread = await _thread(db_session)
    reply_id, turn_id = uuid.uuid4(), uuid.uuid4()
    step = WebStep(id="t1", label="Searching", status="running")

    async def write(
        content: str,
        status: WebMessageStatus,
        cost_usd: Decimal | None = None,
        duration_ms: int | None = None,
    ) -> None:
        await chat.upsert_assistant_message(
            db_session,
            tenant_id=thread.tenant_id,
            thread_id=thread.id,
            message_id=reply_id,
            turn_id=turn_id,
            content=content,
            blocks=WebMessageBlocks(steps=(step,)),
            status=status,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )

    await write("Hel", "running")
    await write("Hello", "done", cost_usd=Decimal("0.01"), duration_ms=900)

    [reply] = await chat.list_messages(db_session, thread_id=thread.id)
    assert (reply.content, reply.status, reply.duration_ms) == ("Hello", "done", 900)
    assert reply.blocks.steps == (step,), "steps round-trip through JSON"


async def test_feedback_is_set_and_cleared(db_session: AsyncSession) -> None:
    thread = await _thread(db_session)
    reply = await chat.upsert_assistant_message(
        db_session,
        tenant_id=thread.tenant_id,
        thread_id=thread.id,
        message_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        content="ok",
        blocks=WebMessageBlocks(),
        status="done",
    )

    await chat.set_feedback(db_session, message_id=reply.id, feedback="up")
    found = await chat.get_message(db_session, person_id=thread.person_id, message_id=reply.id)
    assert found is not None and found.feedback == "up"
    await chat.set_feedback(db_session, message_id=reply.id, feedback=None)
    found = await chat.get_message(db_session, person_id=thread.person_id, message_id=reply.id)
    assert found is not None and found.feedback is None


async def test_deleting_a_thread_erases_its_messages(db_session: AsyncSession) -> None:
    thread = await _thread(db_session)
    await chat.insert_user_message(
        db_session,
        tenant_id=thread.tenant_id,
        thread_id=thread.id,
        message_id=uuid.uuid4(),
        content="secret",
        blocks=WebMessageBlocks(),
    )

    await chat.delete_thread(db_session, thread_id=thread.id, now=_NOW)

    assert (
        await chat.get_thread(db_session, person_id=thread.person_id, thread_id=thread.id) is None
    )
    assert await chat.list_messages(db_session, thread_id=thread.id) == []


async def test_running_replies_are_interrupted_on_boot(db_session: AsyncSession) -> None:
    thread = await _thread(db_session)
    reply = await chat.upsert_assistant_message(
        db_session,
        tenant_id=thread.tenant_id,
        thread_id=thread.id,
        message_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        content="partial",
        blocks=WebMessageBlocks(),
        status="running",
    )

    swept = await chat.mark_running_interrupted(db_session)

    assert [r.id for r in swept] == [reply.id] and swept[0].status == "interrupted"


async def test_titles_are_read_for_the_workspace_only(db_session: AsyncSession) -> None:
    thread = await _thread(db_session, title="Plan")
    other = await make_tenant(db_session, workspace_id="other")

    mine = await chat.list_titles(db_session, tenant_id=thread.tenant_id, thread_ids=[thread.id])
    theirs = await chat.list_titles(db_session, tenant_id=other.id, thread_ids=[thread.id])

    assert mine == {thread.id: "Plan"}
    assert theirs == {}, "another workspace's thread has no title here"
    assert await chat.list_titles(db_session, tenant_id=thread.tenant_id, thread_ids=[]) == {}


async def test_a_reply_saved_after_the_chat_was_deleted_is_not_written(
    db_session: AsyncSession,
) -> None:
    thread = await _thread(db_session)
    await chat.delete_thread(db_session, thread_id=thread.id, now=_NOW)

    reply = await chat.upsert_assistant_message(
        db_session,
        tenant_id=thread.tenant_id,
        thread_id=thread.id,
        message_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        content="late",
        blocks=WebMessageBlocks(),
        status="done",
    )

    assert reply is None, "a deleted chat takes no reply"
    assert await chat.list_messages(db_session, thread_id=thread.id) == [], "nothing was kept"


async def test_deleting_a_thread_erases_the_files_only_it_carried(
    db_session: AsyncSession,
) -> None:
    thread = await _thread(db_session)
    other = await chat.create_thread(
        db_session,
        tenant_id=thread.tenant_id,
        person_id=thread.person_id,
        agent_id="ag_1",
        agent_name="daimon",
        title="other",
    )

    async def upload() -> str:
        row, _ = await create_upload(
            db_session,
            tenant_id=thread.tenant_id,
            person_id=thread.person_id,
            title="f",
            display_filename="f.txt",
            content_type="text/plain",
            now=_NOW,
        )
        return row.id

    mine, shared, mine_output = await upload(), await upload(), await upload()
    for target, attached in ((thread, (mine, shared)), (other, (shared,))):
        await chat.insert_user_message(
            db_session,
            tenant_id=thread.tenant_id,
            thread_id=target.id,
            message_id=uuid.uuid4(),
            content="see",
            blocks=WebMessageBlocks(
                attachments=tuple(WebAttachment(id=i, name="f.txt", size=1) for i in attached)
            ),
        )
    await chat.upsert_assistant_message(
        db_session,
        tenant_id=thread.tenant_id,
        thread_id=thread.id,
        message_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        content="made",
        blocks=WebMessageBlocks(
            files=(WebOutputFile(id=mine_output, name="o.csv", size=1, kind="csv"),)
        ),
        status="done",
    )

    await chat.delete_thread(db_session, thread_id=thread.id, now=_NOW)

    async def kept(handle: str) -> bool:
        found = await db_session.get(FileUpload, handle)
        return found is not None

    assert not await kept(mine), "an attachment only this chat carried is erased"
    assert not await kept(mine_output), "a file the chat's reply made is erased"
    assert await kept(shared), "a file another chat still carries stays"
