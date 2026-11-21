"""Slack posts a workflow's Finish result and owner notice through the shared poller."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from daimon.adapters.slack import workflow_delivery
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.delivery import DeliveryOutcome, JobPoster, poll_once
from daimon.core.stores import accounts, tenants, workflow_deliveries
from daimon.core.stores.domain import RoutineRow
from daimon.testing.factories import make_platform_principal
from daimon.testing.platform_surface import SurfaceRecorder
from daimon.testing.slack_surface import OfflineSlackClient
from daimon.testing.slack_surface import _decode as decode  # pyright: ignore[reportPrivateUsage]
from daimon.testing.workflows import NOW, World, make_world
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from sqlalchemy.ext.asyncio import AsyncSession

FINISH: dict[str, Any] = {
    "t": {"kind": "transform", "set": {"n": "${{ inputs.n }}"}, "next": "f"},
    "f": {"kind": "finish", "deliver": {"to": "home", "message": "Done <!channel> for <@U7>"}},
}
FAILS: dict[str, Any] = {"f": {"kind": "finish", "outcome": "failed"}}


class ChannelClient(OfflineSlackClient):
    """The offline surface, plus the reads the channel check makes; thread replies are kept."""

    def __init__(self, recorder: SurfaceRecorder) -> None:
        super().__init__(recorder)
        self.replies: list[dict[str, Any]] = []

    async def api_call(self, api_method: str, **kwargs: Any) -> AsyncSlackResponse:
        body = cast("dict[str, Any]", kwargs.get("json") or kwargs.get("data") or {})
        reads: dict[str, dict[str, Any]] = {
            "conversations.info": {"channel": {"id": "C0HOME", "is_member": True}},
            "users.info": {"user": {"id": "U1"}},
            "chat.getPermalink": {"permalink": "https://slack.example/p"},
        }
        if api_method == "chat.postMessage" and body.get("thread_ts"):
            self.replies.append(body)
            reads[api_method] = {"ts": f"9.{len(self.replies)}"}
        if api_method not in reads:
            return await super().api_call(api_method, **kwargs)
        data = {"ok": True} | reads[api_method]
        return AsyncSlackResponse(
            client=self,
            http_verb="POST",
            api_url="",
            req_args={},
            data=data,
            headers={},
            status_code=200,
        )


async def _world(db_session: AsyncSession, nodes: dict[str, Any]) -> World:
    world = await make_world(db_session, nodes, next(iter(nodes)), platform="slack", home="C0HOME")
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant, account=owner
    )
    await db_session.commit()
    return world


def _client() -> ChannelClient:
    recorder = SurfaceRecorder(
        evidence_id="wf",
        destination_id="C0HOME",
        thread_id=None,
        clock=time.monotonic,
        decode=decode,
    )
    return ChannelClient(recorder)


async def _deliver(
    world: World, monkeypatch: pytest.MonkeyPatch, client: ChannelClient | None = None
) -> SurfaceRecorder:
    client = client or _client()

    async def resolve(runtime: object, *, team_id: str) -> object:
        return client

    monkeypatch.setattr(workflow_delivery, "resolve_web_client", resolve)
    runtime = cast(
        SlackRuntime,
        SimpleNamespace(
            sessionmaker=world.sm,
            settings=world.settings,
            anthropic=MagicMock(),
            deployment_default=MagicMock(),
            resolver_cache=MagicMock(),
        ),
    )

    async def no_routine(row: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("no routine is due")

    poster = JobPoster(no_routine, workflow_delivery.make_slack_workflow_poster(runtime))
    assert await poll_once(world.sm, platform="slack", post=poster, now=NOW) == 1
    return client.recorder


async def test_a_finish_posts_to_home_without_broadcasting(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session, FINISH)
    await world.run(inputs={"n": 1})
    [message] = (await _deliver(world, monkeypatch)).surviving_messages()
    assert message.body_texts == ('Done @channel for <@U7>\nfrom the workflow "T"',), message


async def test_a_failed_run_sends_the_owner_an_error_card(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session, FAILS)
    await world.run()
    [message] = (await _deliver(world, monkeypatch)).surviving_messages()
    assert "Workflow run failed" in message.card_texts[0], message
    assert message.card_texts[1].startswith('Run 1 of "T" failed.'), message


async def test_a_titled_post_goes_under_one_title_post_and_says_where(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    deliver = {"to": "home", "thread": "Weekly", "message": "Run ${{ run.number }}"}
    world = await _world(db_session, {"f": {"kind": "finish", "deliver": deliver}})
    client = _client()
    run = await world.run()
    await _deliver(world, monkeypatch, client)
    await world.run()
    [title] = (await _deliver(world, monkeypatch, client)).surviving_messages()
    assert title.body_texts == ("Weekly",), title
    replies = [(r["thread_ts"], str(r["text"]).split("\n")[0]) for r in client.replies]
    assert replies == [(title.message_id, "Run 1"), (title.message_id, "Run 2")], (
        "both runs post under the one title post"
    )
    async with world.sm() as s:
        assert await workflow_deliveries.opened_threads(s, run.id) == {
            f"C0HOME:{title.message_id}"
        }, "the post says which thread it went to"
