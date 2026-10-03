"""Run frozen main dispatchers and adapter callers over identical Postgres rows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import discord
import httpx
import pytest
from daimon.adapters.discord import continuation_dispatch as discord_dispatch
from daimon.adapters.slack import continuation_dispatch as slack_dispatch
from daimon.core._models import TaskContinuation
from daimon.core.continuity import dispatch as core_dispatch
from daimon.core.continuity import wakes
from daimon.core.continuity.continuation import ContinuationDecision, ResponderChanged
from daimon.core.errors import DaimonError
from daimon.core.stores.task_continuations import get_continuation, record_continuation
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError, SessionPreparationFailed
from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_engine,
    db_schema,
    db_session,
    db_session_factory,
)
from daimon.testing.factories import make_tenant
from slack_sdk.errors import SlackApiError
from sqlalchemy import delete, update

_NOW = datetime(2026, 10, 3, tzinfo=UTC)
_BASE = Path(__file__).with_name("continuation_base")
_NEW = {"discord": discord_dispatch, "slack": slack_dispatch, "core": core_dispatch}


class ProcessDeath(BaseException):
    """Uncaught termination leaves lease recovery to the real store."""


def _base(platform: str) -> ModuleType:
    module = ModuleType(f"base_{platform}")
    path = _BASE / f"{platform}.txt"
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("platform", ["discord", "slack", "core"])
@pytest.mark.parametrize("wake", [False, True])
@pytest.mark.parametrize(
    "scenario",
    [
        "deliver",
        "slow_delivery",
        "history_boundary",
        "decision_boundary",
        "platform_boundary",
        "anthropic_boundary",
        "save_only",
        "skip",
        "active",
        "missing_seed",
        "preparation",
        "changed",
        "denied",
        "busy",
        "boundary",
        "unexpected",
        "post_failure",
        "changed_post_failure",
        "protected",
        "unknown_protection",
        "history_death",
        "run_death",
        "claim_lost",
        "start_lost",
    ],
)
async def test_dispatch_matches_main(db_session_factory, monkeypatch, platform, wake, scenario):  # noqa: F811
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord" if platform == "core" else platform)
    keys = [uuid.uuid4() for _ in range(3)]
    account_id = uuid.uuid4()
    chat_platform = "teams" if platform == "core" else platform

    async def seed():
        async with db_session_factory.begin() as session:
            await session.execute(delete(TaskContinuation))
            for index, key in enumerate(keys):
                await record_continuation(
                    session,
                    tenant_id=tenant.id,
                    platform=chat_platform,
                    parent_channel_id="parent",
                    thread_id="42",
                    requester_account_id=account_id,
                    requester_external_user_id="user",
                    target_ma_agent_id="agent",
                    target_name="target",
                    requested_work=f"work-{index}",
                    reason="task_handoff",
                    idempotency_key=key,
                    available_at=(_NOW + timedelta(days=1) if index == 2 else _NOW)
                    if wake
                    else None,
                )
                await session.execute(
                    update(TaskContinuation)
                    .where(TaskContinuation.idempotency_key == key)
                    .values(created_at=_NOW - timedelta(minutes=3 - index))
                )

    async def exercise(module):
        events = []
        ticks = 0

        def clock():
            nonlocal ticks
            value = _NOW + timedelta(seconds=ticks)
            ticks += 1
            events.append(("clock", value))
            return value

        async def snapshot():
            async with db_session_factory() as session:
                rows = [await get_continuation(session, idempotency_key=key) for key in keys]
            # Random IDs and lease owner values are opaque. Compare owner presence and all
            # transition fields, including lease expiry, attempts, start and delivery times.
            return [
                row.model_dump(exclude={"id", "lease_owner"})
                | {"lease_owner": row.lease_owner is not None}
                for row in rows
            ]

        def trace(name):
            real = getattr(wakes, name)

            async def wrapped(*args, **kwargs):
                # An omitted skip reason and the explicit default None have the same write.
                call = {k: v for k, v in kwargs.items() if k != "skip_reason" or v is not None}
                events.append((name, call))
                if scenario == "claim_lost" and name == "claim_wake":
                    return None
                if scenario == "start_lost" and name == "start_wake":
                    return False
                result = await real(*args, **kwargs)
                events.append(("writes", await snapshot()))
                return result

            return wrapped

        async def latest(*args, **kwargs):
            events.append(("history",))
            if scenario == "history_boundary":
                raise DaimonError("history failed")
            if scenario == "history_death":
                raise ProcessDeath()
            return None

        async def live(*args, **kwargs):
            events.append(("live", kwargs))
            return SimpleNamespace(active_turn_message_id="busy" if scenario == "active" else None)

        async def decide(*args, **kwargs):
            events.append(("decision", kwargs))
            if scenario == "decision_boundary":
                raise DaimonError("decision failed")
            if scenario == "save_only":
                return ContinuationDecision(action="skip_save_only")
            if scenario in {"skip", "post_failure", "protected", "unknown_protection"}:
                return ContinuationDecision(action="skip_target_changed", message="skip notice")
            if kwargs["active_turn"]:
                return ContinuationDecision(action="skip_turn_running", message="busy notice")
            return ContinuationDecision(
                action="dispatch",
                seed_user_message=(None if scenario == "missing_seed" else "seed"),
            )

        async def post(*args, **kwargs):
            events.append(("post", args, kwargs, await snapshot()))
            if scenario in {"post_failure", "changed_post_failure"}:
                raise RuntimeError("post failed")

        async def may_post():
            events.append(("may_post",))
            return scenario not in {"protected", "unknown_protection"}

        async def protection(*args, **kwargs):
            events.append(("protection", kwargs))
            return SimpleNamespace(
                may_post=await may_post(),
                value="unknown" if scenario == "unknown_protection" else "protected",
            )

        async def run(row, seed_or_decision):
            nonlocal ticks
            if scenario == "slow_delivery":
                ticks += 600
            events.append(("run", row.idempotency_key, seed_or_decision, await snapshot()))
            failures = {
                "preparation": SessionPreparationFailed(
                    reasons=("identity",), stage="ready", retry_after=_NOW
                ),
                "changed": ResponderChanged(target_name="target", current_name="other"),
                "changed_post_failure": ResponderChanged(
                    target_name="target", current_name="other"
                ),
                "denied": AdmissionDenied(reason="channel_protected"),
                "busy": SessionBusyError(
                    pending_reasons=("responder",), retry_after=_NOW + timedelta(minutes=1)
                ),
                "boundary": DaimonError("boundary failed"),
                "unexpected": RuntimeError("unexpected"),
                "run_death": ProcessDeath(),
            }
            if scenario == "platform_boundary":
                if platform == "discord":
                    raise discord.HTTPException(
                        SimpleNamespace(status=500, reason="failed"), "failed"
                    )
                if platform == "slack":
                    raise SlackApiError("failed", {"error": "failed"})
                raise ValueError("platform failed")
            if scenario == "anthropic_boundary":
                raise anthropic.APIError(
                    "failed", httpx.Request("GET", "https://example.test"), body=None
                )
            if scenario in failures:
                raise failures[scenario]

        class DateTime:
            @staticmethod
            def now(tz):
                return clock()

        with monkeypatch.context() as patch:
            # The new adapter calls the real shared ladder. Only external decisions and
            # I/O are injected; every queue operation below uses the real base store.
            ladder = module if module is not _NEW[platform] else core_dispatch
            for name in [
                "list_dispatchable_wakes",
                "claim_wake",
                "start_wake",
                "release_wake",
                "settle_wake",
            ]:
                patch.setattr(ladder, name, trace(name))
            patch.setattr(ladder, "decide_continuation", decide)
            patch.setattr(module, "_latest_human_message_at", latest, raising=False)
            patch.setattr(module, "get_live_thread_session", live, raising=False)
            patch.setattr(ladder, "get_live_thread_session", live, raising=False)
            patch.setattr(module, "protection_state", protection, raising=False)
            patch.setattr(ladder, "protection_state", protection, raising=False)
            patch.setattr(module, "datetime", DateTime)
            thread = MagicMock(spec=discord.Thread, id=42)
            thread.send = post
            web = SimpleNamespace(chat_postMessage=post)
            kwargs = dict(tenant_id=tenant.id, run_follow_up=run)
            if platform == "discord":
                kwargs.update(thread=thread, may_post=may_post, now=clock)
            elif platform == "slack":
                kwargs.update(
                    channel="parent", thread_id="42", active_turn=scenario == "active", now=clock
                )
            else:
                kwargs.update(
                    platform="teams",
                    thread_id="42",
                    post_notice=post,
                    latest_user_message_at=latest,
                    dispatch_errors=(ValueError,),
                )
                if module is core_dispatch:
                    kwargs["now"] = clock
            for _attempt in range(2):
                try:
                    args = [db_session_factory, MagicMock()]
                    if platform == "slack":
                        args.append(web)
                    await module.dispatch_pending_continuations(*args, **kwargs)
                except (Exception, ProcessDeath) as error:
                    events.append(("raised", type(error).__name__, str(error)))
                events.append(("final", await snapshot()))
                if scenario in {
                    "history_death",
                    "run_death",
                    "post_failure",
                    "changed_post_failure",
                    "unexpected",
                }:
                    ticks += int(wakes.WAKE_RUN_LEASE.total_seconds()) + 1
                    await wakes.abandon_interrupted_wakes(
                        db_session_factory,
                        platform=chat_platform,
                        now=_NOW + timedelta(seconds=ticks),
                    )
            return events

    await seed()
    old = await exercise(_base(platform))
    await seed()
    new = await exercise(_NEW[platform])
    assert any(event[0] == "claim_wake" for event in old)
    if scenario in {"deliver", "slow_delivery"}:
        assert any(event[0] == "run" for event in old)
        assert not any(event[0] == "raised" for event in old)
    assert new == old
