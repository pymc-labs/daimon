"""Workflow asks on Teams: a card with buttons, pressed in a DM or answered in its thread."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from daimon.adapters.teams import workflow_asks
from daimon.adapters.teams.direct_chats import SdkDirectChats
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import TeamsSender
from daimon.adapters.teams.workflow_delivery import make_teams_workflow_poster
from daimon.core.delivery import DeliveryOutcome, JobPoster, poll_once
from daimon.core.stores import accounts, tenants, workflow_waits
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.workflow_waits import WaitPost
from daimon.core.teams_bot_framework import SERVICE_URL
from daimon.testing.factories import make_account, make_platform_principal
from daimon.testing.platform_surface import SurfaceRecorder
from daimon.testing.teams_surface import OfflineTeamsSender
from daimon.testing.teams_surface import _decode as decode  # pyright: ignore[reportPrivateUsage]
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy.ext.asyncio import AsyncSession

HOME = "19:home@thread.tacv2"
ROSTER = {"aad-1": "29:owner", "aad-2": "29:derick"}


class RosterChats(SdkDirectChats):
    """The real posting code over the offline sender; a DM opens on the home channel."""

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        return ROSTER.get(aad_object_id)

    async def open_chat(self, member_id: str) -> str:
        assert member_id == "29:derick", "the DM goes to the person asked"
        return HOME


async def _world(db_session: AsyncSession, ask: dict[str, Any]) -> tuple[World, Any]:
    nodes = {
        "ok": {"kind": "approval", "next": "f", **ask},
        "f": {"kind": "finish", "output": "${{ steps.ok.output }}"},
    }
    world = await make_world(db_session, nodes, "ok", platform="teams", home=HOME)
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    derick = await make_account(db_session, tenant=tenant)
    for user, account in (("aad-1", owner), ("aad-2", derick)):
        await make_platform_principal(
            db_session, platform="teams", external_id=user, tenant=tenant, account=account
        )
    await db_session.commit()
    return world, derick.id


async def _deliver(world: World, recorder: SurfaceRecorder) -> None:
    sender = OfflineTeamsSender(recorder, service_url=SERVICE_URL)
    chats = RosterChats(MagicMock(), cast(TeamsSender, sender), entra_tenant_id="entra")

    async def no_links(*_: object) -> None:
        return None

    poster = make_teams_workflow_poster(
        world.sm, chats, tenant_id=world.tenant_id, settings=world.settings, links=no_links
    )

    async def no_routine(row: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("no routine is due")

    while await poll_once(world.sm, platform="teams", post=JobPoster(no_routine, poster), now=NOW):
        pass


def _recorder() -> SurfaceRecorder:
    return SurfaceRecorder(
        evidence_id="wf", destination_id=HOME, thread_id=None, clock=time.monotonic, decode=decode
    )


def _runtime(world: World) -> Any:
    return SimpleNamespace(sessionmaker=world.sm, settings=world.settings)


async def _press(
    world: World, monkeypatch: pytest.MonkeyPatch, user: str, post: WaitPost, custom_id: str
) -> str:
    async def actor(runtime: object, activity: object) -> object:
        return SimpleNamespace(
            user_id=user, tenant_id=world.tenant_id, conversation_id=post.channel_id
        )

    monkeypatch.setattr(workflow_asks, "card_actor", actor)
    data = {"action": workflow_asks.VERB, "custom_id": custom_id}
    activity = SimpleNamespace(
        value=SimpleNamespace(action=SimpleNamespace(data=data)), reply_to_id=post.message_id
    )
    reply = await workflow_asks.AskActions(_runtime(world)).on_action(
        cast("Any", SimpleNamespace(activity=activity))
    )
    return str(cast("Any", reply).value)


def _reply(user: str, thread: str, text: str) -> TeamsInbound:
    return TeamsInbound(
        kind="channel",
        entra_tenant_id="entra",
        user_id=user,
        conversation_id=thread,
        channel_id=HOME,
        activity_id=f"r-{user}",
        text=text,
        service_url=SERVICE_URL,
    )


async def test_ask_derick_by_dm_resolves_by_its_button_and_closes_the_card(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, derick = await _world(db_session, {"approvers": ["${{ inputs.who }}"]})
    recorder = _recorder()
    run = await world.run(inputs={"who": str(derick)})
    wait_id = await world.park(run.id, "ok")
    await _deliver(world, recorder)
    [ask] = recorder.surviving_messages()
    assert "Approve" in str(ask), ask
    async with world.sm() as s:
        [post] = await workflow_waits.posts_of_wait(s, wait_id)
    assert (post.message_id, post.thread_id, post.addressee_account_id) == (
        ask.message_id,
        None,
        derick,
    ), post

    approve = f"wfa:{wait_id}:approve"
    assert "Ask " in await _press(world, monkeypatch, "aad-9", post, approve)
    await _press(world, monkeypatch, "aad-2", post, approve)
    assert (await world.get(run.id)).status == "succeeded"
    await _deliver(world, recorder)
    [closed] = recorder.surviving_messages()
    assert "Approved by" in str(closed) and "Action.Execute" not in str(closed), closed


async def test_ask_reviewer_in_a_thread_refuses_strangers(db_session: AsyncSession) -> None:
    world, _ = await _world(
        db_session, {"approvers": ["thread"], "deliver": "thread", "answer": "text"}
    )
    recorder = _recorder()
    run = await world.run()
    wait_id = await world.park(run.id, "ok")
    await _deliver(world, recorder)
    async with world.sm() as s:
        [post] = await workflow_waits.posts_of_wait(s, wait_id)
    thread = f"{HOME};messageid={post.message_id}"
    assert (post.thread_id, post.addressee_account_id) == (thread, None), post

    sender = OfflineTeamsSender(
        SurfaceRecorder(
            evidence_id="wf",
            destination_id=thread,
            thread_id=None,
            clock=time.monotonic,
            decode=decode,
        ),
        service_url=SERVICE_URL,
    )

    async def claims(user: str, where: str, text: str) -> bool:
        reply = _reply(user, where, text)
        return await workflow_asks.claim_inbound(
            _runtime(world), reply, world.tenant_id, cast(TeamsSender, sender)
        )

    assert not await claims("aad-2", f"{HOME};messageid=1", "lunch?"), "other threads are chat"
    assert await claims("aad-9", thread, "ship it")
    [refusal] = sender.recorder.surviving_messages()
    assert "Ask " in str(refusal), refusal
    assert await claims("aad-2", thread, "Ship it")
    output = (await world.steps(run.id))["f"].output
    assert isinstance(output, dict) and output["text"] == "Ship it", output
