"""Chat answers to workflow asks: files a reply carries reach the wait and later steps."""

from __future__ import annotations

from daimon.core.stores import tenants, web_identities, workflow_waits
from daimon.core.stores.accounts import get_account
from daimon.core.workflows import answers
from daimon.testing.factories import make_platform_principal, make_web_person
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


async def test_a_role_approver_answers_by_the_groups_of_their_sign_ins(
    db_session: AsyncSession,
) -> None:
    """`role:finance` admits a person whose web sign-in carries the finance group."""
    nodes = {"ok": ASK | {"approvers": ["role:finance"]}, "f": {"kind": "finish"}}
    world = await make_world(db_session, nodes, "ok", home="C1")
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    assert tenant is not None
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="7", tenant=tenant
    )
    account = await get_account(db_session, principal.account_id)
    assert account is not None
    person = await make_web_person(db_session, tenant=tenant, account=account)
    await web_identities.link_identity(
        db_session,
        tenant_id=tenant.id,
        person_id=person.id,
        provider="oidc",
        issuer="https://idp.example.com",
        subject="s7",
        label="seven",
        groups=["finance"],
    )
    await db_session.commit()
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
    reply = answers.Reply(
        platform="discord",
        tenant_ids=(world.tenant_id,),
        author="7",
        message_id="M2",
        text="Ship it",
        thread_id="T1",
    )
    claimed = await answers.claim_reply(world.sm, world.settings, reply, now=NOW)
    assert claimed is not None and claimed.status == "answered", claimed
