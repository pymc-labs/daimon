"""Workflow asks on Discord: posted with buttons, answered by a reply, closed by an edit."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from daimon.adapters.discord import workflow_delivery
from daimon.adapters.discord.workflow_asks import claim_message
from daimon.core.delivery import DeliveryOutcome, JobPoster, poll_once
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores import accounts, tenants, workflow_waits
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import RoutineRow
from daimon.core.workflows import runs
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.factories import make_account, make_platform_principal
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy.ext.asyncio import AsyncSession

OWNER, DERICK, STRANGER = 103, 105, 106


async def _world(
    db_session: AsyncSession, harness: DiscordSurfaceHarness, ask: dict[str, Any]
) -> tuple[World, Any]:
    thread = harness.thread
    nodes = {
        "ok": {"kind": "approval", "next": "f", **ask},
        "f": {"kind": "finish", "output": "${{ steps.ok.output }}"},
    }
    world = await make_world(
        db_session, nodes, "ok", home=str(thread.id), workspace_id=str(thread.guild.id)
    )
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    derick = await make_account(db_session, tenant=tenant)
    for user, account in ((OWNER, owner), (DERICK, derick)):
        await make_platform_principal(
            db_session, platform="discord", external_id=str(user), tenant=tenant, account=account
        )
    await set_dm_enabled(db_session, tenant_id=world.tenant_id, enabled=True)
    await db_session.commit()
    return world, derick.id


async def _deliver(world: World, harness: DiscordSurfaceHarness, monkeypatch: Any) -> None:
    thread = harness.thread

    async def fetch(channel_id: int) -> object:
        return thread

    async def open_dm(guild_id: int, user_id: int) -> discord.abc.Messageable:
        assert user_id == DERICK, "the DM goes to the person asked"
        return thread

    async def may_post(channel: object, source: object, user_id: int) -> bool:
        return user_id == OWNER

    async def membership(tenant_id: object, guild_id: object, user_id: object) -> Membership:
        return Membership(is_member=True)

    async def no_routine(row: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("no routine is due")

    async def no_links(*_: object) -> None:
        return None

    monkeypatch.setattr(workflow_delivery, "creator_may_post", may_post)
    poster = workflow_delivery.make_discord_workflow_poster(
        world.sm,
        settings=world.settings,
        links=no_links,
        fetch_channel=fetch,
        open_dm=open_dm,
        membership_resolver=CachedMembershipResolver(membership),
    )
    while await poll_once(
        world.sm, platform="discord", post=JobPoster(no_routine, poster), now=NOW
    ):
        pass


def _message(
    harness: DiscordSurfaceHarness, author: int, text: str, reply_to: str | None
) -> discord.Message:
    state = harness.client._connection  # pyright: ignore[reportPrivateUsage]
    data: dict[str, Any] = {
        "id": str(900 + author),
        "channel_id": str(harness.thread.id),
        "type": 19 if reply_to else 0,
        "content": text,
        "author": {"id": str(author), "username": "p", "discriminator": "0", "avatar": None},
        "timestamp": "2026-10-10T00:00:00+00:00",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "pinned": False,
    }
    if reply_to:
        data["message_reference"] = {"message_id": reply_to, "channel_id": str(harness.thread.id)}
    return discord.Message(state=state, channel=harness.thread, data=cast("Any", data))


async def _claims(
    world: World, harness: DiscordSurfaceHarness, author: int, text: str, reply_to: str | None
) -> bool:
    runtime = cast("Any", SimpleNamespace(sessionmaker=world.sm, settings=world.settings))
    return await claim_message(
        runtime, _message(harness, author, text, reply_to), (world.tenant_id,)
    )


async def test_ask_derick_by_dm_resolves_by_reply_and_closes_the_ask(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = DiscordSurfaceHarness(evidence_id="wf", session_id="s", root_turn_id="r")
    world, derick = await _world(db_session, harness, {"approvers": ["${{ inputs.who }}"]})
    run = await world.run(inputs={"who": str(derick)})
    wait_id = await world.park(run.id, "ok")
    await _deliver(world, harness, monkeypatch)
    [ask] = harness.gateway.surviving_messages()
    assert ask.component_labels == ("Reject", "Approve"), ask
    async with world.sm() as s:
        [post] = await workflow_waits.posts_of_wait(s, wait_id)
    assert (post.message_id, post.thread_id, post.addressee_account_id) == (
        ask.message_id,
        None,
        derick,
    ), post

    assert not await _claims(world, harness, DERICK, "approve", None), "no reply, no answer"
    assert await _claims(world, harness, DERICK, "Approve, looks good", ask.message_id)
    done = await world.get(run.id)
    assert done.status == "succeeded", done
    await _deliver(world, harness, monkeypatch)
    closed = next(m for m in harness.gateway.surviving_messages() if m.message_id == ask.message_id)
    assert "Approved by" in closed.content and closed.component_labels == (), closed


async def test_ask_reviewer_in_a_thread_refuses_strangers_and_leaves_chat_alone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = DiscordSurfaceHarness(evidence_id="wf", session_id="s", root_turn_id="r")
    world, _ = await _world(
        db_session, harness, {"approvers": ["thread"], "deliver": "thread", "answer": "text"}
    )
    run = await world.run()
    wait_id = await world.park(run.id, "ok")
    await _deliver(world, harness, monkeypatch)
    [ask] = harness.gateway.surviving_messages()
    assert ask.component_labels == ("Answer",), ask
    async with world.sm() as s:
        [post] = await workflow_waits.posts_of_wait(s, wait_id)
    assert post.thread_id is None, "a thread the run did not open is not the ask's"

    assert not await _claims(world, harness, DERICK, "lunch?", None), "other chat is a turn"
    assert await _claims(world, harness, STRANGER, "ship it", ask.message_id)
    refusal = harness.gateway.surviving_messages()[-1]
    assert "Ask " in refusal.embed_texts[-1], refusal
    assert (await world.get(run.id)).status != "succeeded", "a stranger cannot answer"
    assert await _claims(world, harness, DERICK, "Ship it", ask.message_id)
    output = (await world.steps(run.id))["f"].output
    assert isinstance(output, dict) and output["text"] == "Ship it", output


async def test_cancelling_the_run_closes_its_posted_ask(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = DiscordSurfaceHarness(evidence_id="wf", session_id="s", root_turn_id="r")
    world, derick = await _world(db_session, harness, {"approvers": ["${{ inputs.who }}"]})
    run = await world.run(inputs={"who": str(derick)})
    await world.park(run.id, "ok")
    await _deliver(world, harness, monkeypatch)
    [ask] = harness.gateway.surviving_messages()
    assert await runs.cancel_run(
        world.sm, world.settings, run_id=run.id, account_id=world.owner, now=NOW
    ), "the run stops"
    await _deliver(world, harness, monkeypatch)
    [closed] = harness.gateway.surviving_messages()
    assert closed.message_id == ask.message_id, "no new post, the ask is edited"
    assert "Cancelled" in closed.content and closed.component_labels == (), closed
    assert not await _claims(world, harness, DERICK, "approve", ask.message_id), (
        "a reply to a closed ask is ordinary chat"
    )
