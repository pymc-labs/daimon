"""Tenant capacity is reserved before spawning and released on every task exit."""

import asyncio
import dataclasses
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from daimon.adapters.teams.app import DirectCoreTurnDispatcher
from daimon.core.config import SlackSettings, TeamsSettings

from .conftest import make_dispatch_target
from .test_dispatch import _settings


@pytest.mark.parametrize("cap", [1, 3])
async def test_tenant_cap_rejects_only_excess_activity(db_session_factory: Any, cap: int) -> None:
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)
    dispatcher.settings = _settings().model_copy(update={"max_concurrent_turns_per_tenant": cap})
    release = asyncio.Event()
    started: list[str] = []

    async def blocked(self: Any, ctx: Any, activity: Any) -> None:
        started.append(activity.activity_id)
        await release.wait()

    with patch.object(DirectCoreTurnDispatcher, "_run_turn", blocked):
        try:
            await asyncio.gather(
                *(
                    dispatcher.dispatch(ctx, dataclasses.replace(activity, activity_id=str(i)))
                    for i in range(cap + 1)
                )
            )
            assert dispatcher.in_flight == cap
            assert len(started) == cap
            assert ctx.sent == ["Too many chats are in progress. Please try again in a moment."]
        finally:
            release.set()
            await dispatcher.drain()
    assert dispatcher._inflight.get(activity.tenant_id, 0) == 0


@pytest.mark.parametrize("outcome", ["success", "exception", "cancel", "cancel_before_start"])
async def test_tenant_capacity_is_released(db_session_factory: Any, outcome: str) -> None:
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)
    dispatcher.settings = _settings().model_copy(update={"max_concurrent_turns_per_tenant": 1})
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run(self: Any, ctx: Any, activity: Any) -> None:
        entered.set()
        await release.wait()
        if outcome == "exception":
            raise RuntimeError("test turn failed")

    with patch.object(DirectCoreTurnDispatcher, "_run_turn", run):
        await dispatcher.dispatch(ctx, activity)
        if outcome == "cancel_before_start":
            for task in tuple(dispatcher._tasks):
                task.cancel()
        else:
            await asyncio.wait_for(entered.wait(), 5)
        if outcome in ("success", "exception"):
            release.set()
        await dispatcher.drain(timeout=0 if outcome.startswith("cancel") else 5)
        assert dispatcher.in_flight == 0
        assert dispatcher._inflight.get(activity.tenant_id, 0) == 0
        release.set()
        await dispatcher.dispatch(ctx, dataclasses.replace(activity, activity_id="next"))
        await dispatcher.drain()
    assert not ctx.sent, "a completed task must leave room for another activity"
    assert dispatcher._inflight.get(activity.tenant_id, 0) == 0


async def test_tenants_have_independent_capacity(db_session_factory: Any) -> None:
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)
    dispatcher.settings = _settings().model_copy(update={"max_concurrent_turns_per_tenant": 1})
    other = dataclasses.replace(activity, tenant_id=uuid.uuid4(), activity_id="other")
    release = asyncio.Event()

    async def blocked(self: Any, ctx: Any, activity: Any) -> None:
        await release.wait()

    with patch.object(DirectCoreTurnDispatcher, "_run_turn", blocked):
        try:
            await dispatcher.dispatch(ctx, activity)
            await dispatcher.dispatch(ctx, other)
            assert dispatcher.in_flight == 2
            assert not ctx.sent
        finally:
            release.set()
            await dispatcher.drain()
    assert dispatcher._inflight.get(activity.tenant_id, 0) == 0
    assert dispatcher._inflight.get(other.tenant_id, 0) == 0


def test_teams_capacity_setting_matches_slack_default() -> None:
    teams = TeamsSettings.model_fields["max_concurrent_turns_per_tenant"]
    slack = SlackSettings.model_fields["max_concurrent_turns_per_tenant"]
    assert teams.annotation is slack.annotation is int
    assert teams.default == slack.default == 3
