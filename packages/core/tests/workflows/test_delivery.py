"""Workflow posts: what a run queues, the shared poller settling them, the rules at post time."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from daimon.core._models import WorkflowDelivery
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import DirectMessagePolicy, WebSettings
from daimon.core.delivery import DeliveryOutcome, JobPoster, poll_once
from daimon.core.outbound.reach import ReachPlan
from daimon.core.stores import accounts, tenants
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.web_links import WebLinks
from daimon.core.workflows.delivery import (
    DM_OFF,
    WorkflowPost,
    clear_post,
    post_refusal,
    target_for,
)
from daimon.testing.factories import make_platform_principal
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

DIGEST: dict[str, Any] = {
    "t": {"kind": "transform", "set": {"n": "${{ inputs.n }}"}, "next": "f"},
    "f": {
        "kind": "finish",
        "output": "${{ steps.t.output }}",
        "deliver": {"to": "home", "message": "Count: ${{ steps.t.output.n }}"},
    },
}
FLAKY: dict[str, Any] = {
    "b": {"kind": "branch", "cases": [{"when": "${{ inputs.ok }}", "to": "good"}], "else": "bad"},
    "good": {"kind": "finish"},
    "bad": {"kind": "finish", "outcome": "failed"},
}


def _digest(to: str) -> dict[str, Any]:
    finish = DIGEST["f"] | {"deliver": DIGEST["f"]["deliver"] | {"to": to}}
    return DIGEST | {"f": finish}


async def _rows(world: World) -> list[DeliveryRow]:
    async with world.sm() as s:
        found = await s.scalars(select(WorkflowDelivery).order_by(WorkflowDelivery.created_at))
        return [DeliveryRow.model_validate(r) for r in found]


async def test_a_finish_queues_its_post_and_the_shared_poller_settles_it(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, _digest("home"), "t", platform="slack", home="C0HOME")
    run = await world.run(inputs={"n": 3})
    [row] = await _rows(world)
    assert (row.kind, row.target, row.payload["text"]) == (
        "finish",
        {"kind": "channel", "channel_id": "C0HOME", "thread_id": None, "home": True},
        "Count: 3",
    ), "the Finish queues its rendered message for the home channel"
    posted: list[DeliveryRow] = []

    async def routine(row: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("no routine is due")

    async def workflow(row: DeliveryRow) -> DeliveryOutcome:
        posted.append(row)
        return DeliveryOutcome(status="delivered")

    poster = JobPoster(routine, workflow)
    assert await poll_once(world.sm, platform="slack", post=poster, now=NOW) == 1
    assert await poll_once(world.sm, platform="slack", post=poster, now=NOW) == 0, "at most once"
    [settled] = await _rows(world)
    assert ([p.id for p in posted], settled.status) == ([row.id], "sent")
    types = await world.verified(run.id)
    assert "delivery.queued" in types and types[-1] == "delivery.sent", types
    assert await poll_once(world.sm, platform="slack", post=JobPoster(routine), now=NOW) == 0, (
        "an adapter without a workflow poster leaves workflow posts alone"
    )


async def test_the_owner_is_told_once_per_streak_of_failures(db_session: AsyncSession) -> None:
    world = await make_world(db_session, FLAKY, "b", home="555")
    for ok in (False, False, True, False):
        await world.run(inputs={"ok": ok})
    notices = [r for r in await _rows(world) if r.kind == "notice"]
    assert [n.payload["run_number"] for n in notices] == [1, 4], (
        "the first failure and the first after a success"
    )
    assert notices[0].target["channel_id"] == "555", "the notice goes to the home channel"


async def test_post_time_rules_and_dms(db_session: AsyncSession) -> None:
    world = await make_world(db_session, _digest("owner"), "t", platform="slack", home="C0HOME")
    await world.run(inputs={"n": 1})
    [dm] = await _rows(world)
    assert dm.target["kind"] == "dm", "owner and run_as deliver by DM"
    assert await clear_post(world.sm, dm, platform="slack", dm_policies={}) == DM_OFF, (
        "Slack DMs start off"
    )
    await set_dm_enabled(db_session, tenant_id=world.tenant_id, enabled=True)
    await db_session.commit()
    assert (
        await clear_post(world.sm, dm, platform="slack", dm_policies={})
        == "recipient_not_on_platform"
    )
    home = dm.model_copy(
        update={"target": {"kind": "channel", "channel_id": "C0HOME", "home": True}}
    )
    assert (
        await clear_post(world.sm, home, platform="slack", dm_policies={})
        == "speaker_not_on_platform"
    )
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant, account=owner
    )
    await db_session.commit()
    to_owner = await clear_post(world.sm, dm, platform="slack", dm_policies={})
    assert isinstance(to_owner, WorkflowPost) and to_owner.dm_to == "U1", to_owner
    assert await post_refusal(to_owner, place_id="", limits=ReachPlan(places=0, people=0)) is None
    shut = {world.tenant_id: DirectMessagePolicy(mode="disabled")}
    assert await clear_post(world.sm, dm, platform="slack", dm_policies=shut) == DM_OFF, (
        "the deprecated env DM policy still decides who may be sent a private message"
    )
    post = await clear_post(world.sm, home, platform="slack", dm_policies={})
    assert isinstance(post, WorkflowPost) and post.speaker == "U1", post
    asked: list[tuple[str | None, str | None]] = []
    on = WebLinks.from_settings(WebSettings(public_url="https://app.example.com"))  # type: ignore[call-arg]
    assert on is not None

    async def links_on(tenant: Any, channel: str | None, thread: str | None) -> Any:
        asked.append((channel, thread))
        return on.for_tenant(tenant, admin=False)

    async def links_off(*_: Any) -> None:
        return None

    linked = await clear_post(world.sm, home, platform="slack", dm_policies={}, links=links_on)
    assert isinstance(linked, WorkflowPost) and linked.link is not None, linked
    assert linked.link.endswith(f"/workflows/{home.workflow_id}/runs/1"), linked.link
    assert asked == [("C0HOME", None)], "the link is asked at the channel the post goes to"
    muted = await clear_post(world.sm, home, platform="slack", dm_policies={}, links=links_off)
    assert isinstance(muted, WorkflowPost) and muted.link is None, "Off at the place: no link"
    tight = ReachPlan(places=0, people=1)
    assert await post_refusal(post, place_id="C0HOME", limits=tight) is None, "home is no reach"
    away = replace(post, row=dm.model_copy(update={"target": {}}))
    assert await post_refusal(away, place_id="C0OTHER", limits=tight) == "reach"
    assert await post_refusal(post, place_id="C0HOME", limits=tight, mentioned=("U2", "U3")) == (
        "reach"
    ), "tagging more people than the limit refuses"


def test_targets_name_threads_on_every_platform() -> None:
    run: dict[str, Any] = {"mode": "live", "run_as": None, "owner": None}
    assert target_for("C01:1717.5", home=None, **run)["thread_id"] == "1717.5"
    teams = target_for("19:a@thread.tacv2;messageid=42", home=None, **run)
    assert (teams["channel_id"], teams["thread_id"]) == ("19:a@thread.tacv2", "42")
    assert target_for("home", home="555", **run)["home"] is True


async def test_a_workflow_made_where_content_stays_posts_nothing_by_dm(
    db_session: AsyncSession,
) -> None:
    """The routine rule (`keeps_routine_inside`) on the workflow's `made_in`, for every DM."""
    from daimon.core._models import Workflow
    from sqlalchemy import update

    world = await make_world(db_session, _digest("owner"), "t", platform="slack", home="C0HOME")
    await world.run(inputs={"n": 1})
    [dm] = await _rows(world)
    await set_dm_enabled(db_session, tenant_id=world.tenant_id, enabled=True)
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant, account=owner
    )
    for known, cleared in ((False, "restricted_source"), (True, WorkflowPost)):
        made = {"known": known, "channel_id": "C0HOME", "thread_id": None}
        await db_session.execute(
            update(Workflow).where(Workflow.id == world.workflow_id).values(made_in=made)
        )
        await db_session.commit()
        got = await clear_post(world.sm, dm, platform="slack", dm_policies={})
        assert got == cleared if isinstance(cleared, str) else isinstance(got, cleared), got
    # A known source whose channel keeps its content inside refuses too.
    await set_access_policy(
        db_session,
        tenant_id=world.tenant_id,
        policy=TenantAccessPolicy(channel_rules={"C0HOME": ChannelRule(readers="inside")}),
    )
    await db_session.commit()
    assert await clear_post(world.sm, dm, platform="slack", dm_policies={}) == "restricted_source"
