"""Discord posts a workflow's Finish result and owner notice through the shared poller."""

from __future__ import annotations

import json
from typing import Any

import discord
import pytest
from daimon.adapters.discord import workflow_delivery
from daimon.core.delivery import DeliveryOutcome, JobPoster, poll_once
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores import accounts, tenants
from daimon.core.stores.domain import RoutineRow
from daimon.testing.discord_surface import DiscordMessageCapture, DiscordSurfaceHarness
from daimon.testing.factories import make_platform_principal
from daimon.testing.workflows import NOW, make_world
from sqlalchemy.ext.asyncio import AsyncSession


async def _delivered(
    db_session: AsyncSession, nodes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> DiscordMessageCapture:
    harness = DiscordSurfaceHarness(evidence_id="wf", session_id="s", root_turn_id="r")
    thread = harness.thread
    world = await make_world(
        db_session,
        nodes,
        next(iter(nodes)),
        home=str(thread.id),
        workspace_id=str(thread.guild.id),
    )
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform="discord", external_id="103", tenant=tenant, account=owner
    )
    await db_session.commit()
    await world.run()

    async def fetch(channel_id: int) -> object:
        assert channel_id == thread.id, "only the home thread is fetched"
        return thread

    async def may_post(channel: object, source: object, user_id: int) -> bool:
        return user_id == 103

    async def membership(tenant_id: object, guild_id: object, user_id: object) -> Membership:
        return Membership(is_member=True)

    async def no_dm(guild_id: int, user_id: int) -> discord.abc.Messageable:
        raise AssertionError("these posts go to the home thread")

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
        open_dm=no_dm,
        membership_resolver=CachedMembershipResolver(membership),
    )
    assert await poll_once(
        world.sm, platform="discord", post=JobPoster(no_routine, poster), now=NOW
    )
    [message] = harness.gateway.surviving_messages()
    return message


async def test_a_finish_posts_to_home_pinging_only_who_it_tags(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "Done @everyone, <@104>"
    nodes = {"f": {"kind": "finish", "deliver": {"to": "home", "message": message}}}
    posted = await _delivered(db_session, nodes, monkeypatch)
    assert posted.content == 'Done @\u200beveryone, <@104>\n-# from the workflow "T"', posted
    pings = json.loads(posted.payload_json)["allowed_mentions"]
    assert (pings["users"], pings["parse"]) == ([104], []), "only the tagged person is pinged"


async def test_a_failed_run_sends_the_owner_a_notice_card(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes = {"f": {"kind": "finish", "outcome": "failed"}}
    posted = await _delivered(db_session, nodes, monkeypatch)
    assert posted.embed_texts[0] == "Workflow run failed", posted
    assert posted.embed_texts[1].startswith('Run 1 of "T" failed.'), posted
