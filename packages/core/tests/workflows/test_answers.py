"""Chat answers to workflow asks: files a reply carries reach the wait and later steps."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from daimon.core.stores import tenants, web_identities, workflow_deliveries, workflow_waits
from daimon.core.stores.accounts import get_account
from daimon.core.workflows import answers, asks
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


async def test_a_press_decides_only_the_wait_its_button_names_in_its_channel(
    db_session: AsyncSession,
) -> None:
    """Slack ids are unique per channel: two asks posted as one ts stay apart."""
    approve = {k: v for k, v in ASK.items() if k != "answer"} | {"approvers": ["owner"]}
    nodes = {"ok": approve, "f": {"kind": "finish"}}
    world = await make_world(db_session, nodes, "ok", home="C1")
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant, account=owner
    )
    first, second = await world.run(), await world.run()
    waits = [await world.park(first.id, "ok"), await world.park(second.id, "ok")]
    async with world.sm.begin() as s:
        for wait_id, channel in zip(waits, ("C1", "C2"), strict=True):
            await workflow_waits.record_post(
                s,
                tenant_id=world.tenant_id,
                wait_id=wait_id,
                platform="slack",
                channel_id=channel,
                thread_id=None,
                message_id="1700000000.000100",
                addressee_account_id=None,
            )

    def at(wait_id: uuid.UUID, channel: str) -> answers.Pressed:
        return answers.Pressed("slack", (world.tenant_id,), channel, "1700000000.000100", wait_id)

    wrong = await answers.press(
        world.sm, world.settings, at=at(waits[0], "C2"), author="U1", route="approve", now=NOW
    )
    assert wrong.status == "hint", f"no ask of that wait in that channel: {wrong}"
    pressed = await answers.press(
        world.sm, world.settings, at=at(waits[1], "C2"), author="U1", route="approve", now=NOW
    )
    assert pressed.status == "answered", pressed
    assert (await world.get(first.id)).status == "waiting", "the other channel's ask is open"
    assert (await world.get(second.id)).status == "succeeded", "the pressed ask decided"


@pytest.mark.parametrize(
    ("platform", "existing", "message"),
    [
        ("discord", "T9", "M1"),
        ("slack", "C1:1.0", "2.0"),
        ("teams", "C1;messageid=9", "M1"),
    ],
)
async def test_only_a_reply_answers_an_ask_in_a_thread_the_run_did_not_open(
    db_session: AsyncSession, platform: answers.Chat, existing: str, message: str
) -> None:
    ask = {key: value for key, value in ASK.items() if key != "answer"}
    nodes = {"ok": ask, "f": {"kind": "finish"}}
    world = await make_world(db_session, nodes, "ok", platform=platform, home="C1")
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    assert tenant is not None
    await make_platform_principal(db_session, platform=platform, external_id="7", tenant=tenant)
    run = await world.run()
    wait_id = await world.park(run.id, "ok")

    async def post_in(thread: str) -> str | None:
        async with world.sm.begin() as s:
            [row] = await workflow_deliveries.deliveries_of_wait(s, wait_id)
            post = await asks.record_post(
                s, row, platform=platform, channel_id="C1", thread_id=thread,
                message_id=message, at=NOW,
            )  # fmt: skip
        assert post is not None
        return post.thread_id

    async def claim(text: str, reply_to: str | None = None, author: str = "7") -> str | None:
        reply = answers.Reply(
            platform=platform,
            tenant_ids=(world.tenant_id,),
            author=author,
            message_id=f"m{uuid.uuid4().hex[:6]}",
            text=text,
            thread_id=existing,
            reply_to=reply_to,
        )
        claimed = await answers.claim_reply(world.sm, world.settings, reply, now=NOW)
        return None if claimed is None else claimed.status

    assert await post_in(existing) is None, "a thread the run did not open is not the ask's"
    assert await claim("lunch?") is None, "ordinary chat there stays chat: no hint, no refusal"
    assert await claim("lunch?", reply_to=message) == "hint", "a reply to the ask is for it"
    assert await claim("approve", reply_to=message, author="8") == "refused", "not admitted"
    async with world.sm.begin() as s:
        [row] = await workflow_deliveries.claim_deliveries(
            s, platform=platform, owner="w", now=NOW, lease=timedelta(minutes=1)
        )
        await workflow_deliveries.settle_delivery(
            s, row.id, owner="w", status="sent", now=NOW,
            placed={"thread": existing, "opened": True},
        )  # fmt: skip
    assert await post_in(existing) == existing, "a thread this run opened is the ask's"
    assert await claim("lunch?") == "hint", "any message in it is for the ask"
    assert await claim("approve") == "answered"
