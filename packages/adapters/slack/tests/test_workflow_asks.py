"""Workflow asks on Slack: posted with buttons, answered in the thread under them, then closed."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from daimon.adapters.slack import workflow_asks, workflow_delivery
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.delivery import DeliveryOutcome, JobPoster, poll_once
from daimon.core.stores import accounts, tenants, workflow_waits
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import RoutineRow
from daimon.core.workflows import inbox
from daimon.core.workflows.access import Who
from daimon.core.workflows.service import Env
from daimon.testing.factories import make_account, make_platform_principal
from daimon.testing.platform_surface import SurfaceRecorder
from daimon.testing.slack_surface import OfflineSlackClient
from daimon.testing.slack_surface import _decode as decode  # pyright: ignore[reportPrivateUsage]
from daimon.testing.workflows import NOW, World, make_world
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from sqlalchemy.ext.asyncio import AsyncSession

HOME = "C0HOME"


class AskClient(OfflineSlackClient):
    """The offline surface; the DM opens on the home channel and thread replies are kept."""

    def __init__(self, recorder: SurfaceRecorder) -> None:
        super().__init__(recorder)
        self.replies: list[dict[str, Any]] = []

    async def api_call(self, api_method: str, **kwargs: Any) -> AsyncSlackResponse:
        body = cast("dict[str, Any]", kwargs.get("json") or kwargs.get("data") or {})
        reads: dict[str, dict[str, Any]] = {
            "conversations.info": {"channel": {"id": HOME, "is_member": True}},
            "conversations.open": {"channel": {"id": HOME}},
            "users.info": {"user": {"id": "U1"}},
        }
        if api_method == "chat.postMessage" and body.get("thread_ts"):
            self.replies.append(body)
            reads[api_method] = {"ts": "9.9"}
        if api_method not in reads:
            return await super().api_call(api_method, **kwargs)
        return AsyncSlackResponse(
            client=self,
            http_verb="POST",
            api_url="",
            req_args={},
            data={"ok": True} | reads[api_method],
            headers={},
            status_code=200,
        )


async def _world(db_session: AsyncSession, ask: dict[str, Any]) -> tuple[World, Any]:
    nodes = {
        "ok": {"kind": "approval", "next": "f", **ask},
        "f": {"kind": "finish", "output": "${{ steps.ok.output }}"},
    }
    world = await make_world(db_session, nodes, "ok", platform="slack", home=HOME)
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    derick = await make_account(db_session, tenant=tenant)
    for user, account in (("U1", owner), ("U2", derick)):
        await make_platform_principal(
            db_session, platform="slack", external_id=user, tenant=tenant, account=account
        )
    await set_dm_enabled(db_session, tenant_id=world.tenant_id, enabled=True)
    await db_session.commit()
    return world, derick.id


def _surface(world: World, monkeypatch: pytest.MonkeyPatch) -> tuple[AskClient, SlackRuntime]:
    recorder = SurfaceRecorder(
        evidence_id="wf", destination_id=HOME, thread_id=None, clock=time.monotonic, decode=decode
    )
    client = AskClient(recorder)

    async def resolve(runtime: object, *, team_id: str) -> object:
        return client

    monkeypatch.setattr(workflow_delivery, "resolve_web_client", resolve)
    monkeypatch.setattr(workflow_asks, "resolve_web_client", resolve)
    runtime = SimpleNamespace(sessionmaker=world.sm, settings=world.settings, anthropic=MagicMock())
    return client, cast(SlackRuntime, runtime)


async def _deliver(world: World, runtime: SlackRuntime) -> None:
    async def no_routine(row: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("no routine is due")

    poster = JobPoster(no_routine, workflow_delivery.make_slack_workflow_poster(runtime))
    while await poll_once(world.sm, platform="slack", post=poster, now=NOW):
        pass


async def _claims(runtime: SlackRuntime, world: World, user: str, text: str, ts: str) -> bool:
    async with world.sm() as s:
        tenant = await tenants.get_tenant(s, world.tenant_id)
    assert tenant is not None
    event = {"type": "message", "channel": HOME, "user": user, "text": text, "ts": "5.5"}
    if ts:
        event["thread_ts"] = ts
    return await workflow_asks.claim_event(runtime, event, team_id=tenant.external_id)


async def test_ask_derick_by_dm_resolves_in_the_thread_under_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, derick = await _world(db_session, {"approvers": ["${{ inputs.who }}"]})
    client, runtime = _surface(world, monkeypatch)
    run = await world.run(inputs={"who": str(derick)})
    wait_id = await world.park(run.id, "ok")
    await _deliver(world, runtime)
    [ask] = client.recorder.surviving_messages()
    async with world.sm() as s:
        [post] = await workflow_waits.posts_of_wait(s, wait_id)
    assert (post.thread_id, post.addressee_account_id) == (f"{HOME}:{post.message_id}", derick)
    assert "Approve" in str(ask), ask
    env = Env(world.sm, world.settings, Who(world.tenant_id, derick), NOW)
    [sent] = (await inbox.item(env, wait_id)).sent
    assert (sent["platform"], sent["where"][:18]) == ("slack", "Direct message to "), sent

    assert not await _claims(runtime, world, "U2", "approve", ""), "no thread, no answer"
    assert await _claims(runtime, world, "U2", "approve", post.message_id)
    assert (await world.get(run.id)).status == "succeeded"
    answer = (await inbox.item(env, wait_id)).answer
    assert answer is not None and answer["via"] == "slack", answer
    await _deliver(world, runtime)
    [closed] = client.recorder.surviving_messages()
    assert "Approved by" in str(closed) and "actions" not in str(closed), closed


async def test_ask_reviewer_in_a_thread_refuses_strangers(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _ = await _world(
        db_session, {"approvers": ["thread"], "deliver": "thread", "answer": "text"}
    )
    client, runtime = _surface(world, monkeypatch)
    run = await world.run()
    wait_id = await world.park(run.id, "ok")
    await _deliver(world, runtime)
    async with world.sm() as s:
        [post] = await workflow_waits.posts_of_wait(s, wait_id)
    assert post.addressee_account_id is None, post

    assert not await _claims(runtime, world, "U2", "lunch?", "1.1"), "other threads are chat"
    assert await _claims(runtime, world, "U9", "ship it", post.message_id)
    assert "Ask " in str(client.replies[-1]), client.replies
    assert await _claims(runtime, world, "U2", "Ship it", post.message_id)
    output = (await world.steps(run.id))["f"].output
    assert isinstance(output, dict) and output["text"] == "Ship it", output
