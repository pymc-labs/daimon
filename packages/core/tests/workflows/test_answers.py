"""Chat answers to workflow asks: files a reply carries reach the wait and later steps."""

from __future__ import annotations

from daimon.core.stores import tenants, workflow_waits
from daimon.core.workflows import answers
from daimon.testing.factories import make_platform_principal
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.workflows import NOW, make_world
from mux.testing.drivers.anthropic.ma import FakeMAState, build_fake_anthropic
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

ASK = {
    "kind": "approval",
    "approvers": ["thread"],
    "deliver": "thread",
    "answer": "text",
    "next": "f",
}


async def test_a_thread_answer_keeps_its_files_on_the_wait_and_the_step(
    db_session: AsyncSession,
) -> None:
    nodes = {"ok": ASK, "f": {"kind": "finish", "output": "${{ steps.ok.files }}"}}
    world = await make_world(db_session, nodes, "ok", home="C1")
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    assert tenant is not None
    await make_platform_principal(db_session, platform="discord", external_id="7", tenant=tenant)
    run = await world.run()
    wait_id = await world.park(run.id, "ok")
    async with world.sm.begin() as s:
        await workflow_waits.record_post(
            s,
            tenant_id=world.tenant_id,
            wait_id=wait_id,
            platform="discord",
            channel_id="C1",
            thread_id="T1",
            message_id="M1",
            addressee_account_id=None,
        )
    state = FakeSessionsState(ma=FakeMAState())
    client = build_fake_anthropic(make_fake_sessions_handler(state))

    async def ingest() -> list[dict[str, JsonValue]]:
        return await answers.ingest_files(
            client, [answers.Attachment("notes.txt", "text/plain", b"hello")]
        )

    reply = answers.Reply(
        platform="discord",
        tenant_ids=(world.tenant_id,),
        author="7",
        message_id="M2",
        text="Ship it",
        thread_id="T1",
        files=ingest,
    )
    claimed = await answers.claim_reply(world.sm, world.settings, reply, now=NOW)
    assert claimed is not None and claimed.status == "answered", claimed
    [(file_id, (meta, content))] = state.files.items()
    assert (meta.filename, content) == ("notes.txt", b"hello"), "the file is uploaded once"
    async with world.sm() as s:
        wait = await workflow_waits.get_wait(s, wait_id)
    assert wait is not None and wait.answer_files, "the wait keeps the answer's files"
    assert wait.answer_files[0]["file_id"] == file_id, wait.answer_files
    output = (await world.steps(run.id))["f"].output
    assert isinstance(output, list) and output == wait.answer_files, (
        "later steps read the answer's files"
    )
