"""Callback ingress through the production Discord entry and mux driver, offline."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.core.continuity.wakes import poll_wakes_once
from daimon.core.stores import watches
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn.prepare import PreparedTurn
from daimon.testing import ma_agent
from mux.testing.drivers.anthropic.ma import (
    build_fake_anthropic,
    list_response,
    send_events_response,
    sse_response,
)

from .harness import make_bot
from .test_continuation_dispatch_entry import _make_guild, _make_member, _make_runtime, _make_thread
from .watch_fixtures import (
    WatchDiscordRest,
    cached_watch_session,
    enrolled_page_watch,
    watch_admission_patches,
    watch_router,
)


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    "mode",
    [
        "revoked_during_turn",
        "private_removed_during_turn",
        "history_revoked_during_turn",
        "category_moved_during_turn",
    ],
)
async def test_review_revocation_before_final_delivery(db_session_factory, mode, monkeypatch):
    db_session_factory, now, tenant, principal, mapping, watch, key = await enrolled_page_watch(
        db_session_factory, monkeypatch
    )
    target = ma_agent(
        id="ag_target",
        name="target-agent",
        tenant_id=tenant.id,
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            },
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            },
        ],
        mcp_servers=[{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}],
    )
    snapshot = await cached_watch_session(db_session_factory, target, mapping)
    sent = []
    user_sent = asyncio.Event()
    updates = []
    sent_tools = []
    router = watch_router(target, tenant.id, lambda: snapshot)

    def update(request, _match):
        nonlocal snapshot
        body = json.loads(request.content)
        updates.append(body)
        if "agent" in body:
            merged = snapshot.agent.model_dump(mode="json") | body["agent"]
            snapshot = snapshot.model_copy(
                update={"agent": type(snapshot.agent).model_validate(merged)}
            )
        return httpx.Response(200, json=snapshot.model_dump(mode="json"))

    def send(request, _match):
        sent.extend(json.loads(request.content)["events"])
        sent_tools.append(snapshot.agent.tools)
        user_sent.set()
        return send_events_response()

    router.add("POST", r"/v1/sessions/sess_watch", update)
    router.add("POST", r"/v1/sessions/sess_watch/events", send)
    router.add("GET", r"/v1/sessions/sess_watch/events", lambda *_: list_response([]))

    class AfterSend(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.wait_for(user_sent.wait(), timeout=5)
            if mode == "private_removed_during_turn":
                rest.membership_error = discord.NotFound(
                    MagicMock(status=404, reason="gone"), "Unknown Member"
                )
            elif mode == "history_revoked_during_turn":
                rest.permissions.read_message_history = False
            elif mode == "category_moved_during_turn":
                from daimon.core.access_policy import TenantAccessPolicy
                from daimon.core.stores.access_policy import set_access_policy

                rest.category_id = "900"
                async with db_session_factory.begin() as db:
                    await set_access_policy(
                        db,
                        tenant_id=tenant.id,
                        policy=TenantAccessPolicy(category_rules={"900": {"writers": "none"}}),
                    )
            yield sse_response(
                [
                    {
                        "id": "evt_review_text",
                        "type": "agent.message",
                        "content": [{"type": "text", "text": "The upload is ready."}],
                        "processed_at": now.isoformat(),
                    }
                ]
            ).content
            await asyncio.sleep(0.1)
            if mode == "revoked_during_turn":
                async with db_session_factory.begin() as db:
                    await watches.revoke_capabilities(db, watch.id, now=datetime.now(UTC))
                thread.send.reset_mock()
                thread.send.return_value.edit.reset_mock()
            yield sse_response(
                [
                    {
                        "id": "evt_review_idle",
                        "type": "session.status_idle",
                        "stop_reason": {"type": "end_turn"},
                        "processed_at": now.isoformat(),
                    }
                ]
            ).content

    router.add(
        "GET",
        r"/v1/sessions/sess_watch/events/stream",
        lambda *_: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=AfterSend()
        ),
    )
    transport_failures = []
    requests = []

    def dispatch(request):
        requests.append((request.method, request.url.path))
        try:
            return router.dispatch(request)
        except Exception as exc:
            transport_failures.append(repr(exc))
            raise

    fake_client = build_fake_anthropic(dispatch)
    runtime = _make_runtime(db_session_factory, agent=target)
    deps = replace(
        runtime.turn_deps,
        anthropic=fake_client,
        turn_path="mux",
        tool_safety=ToolSafetyPolicy(enabled=True),
    )
    runtime = replace(runtime, anthropic=fake_client, turn_deps=deps)
    bot = make_bot(runtime)
    rest = WatchDiscordRest(bot)
    guild = _make_guild(member=_make_member(admin=False))
    guild.id = 710000099
    thread = _make_thread(guild=guild)
    thread.locked = False
    thread.permissions_for.return_value = discord.Permissions(
        view_channel=True, read_message_history=True, send_messages_in_threads=True
    )
    rest.private = True
    thread.type = discord.ChannelType.private_thread
    thread.fetch_member = AsyncMock(return_value=MagicMock())
    bot.get_channel = MagicMock(return_value=thread)

    bound = []

    async def bind(_deps, admission, **_kwargs):
        async def record(**_event):
            pass

        prepared = PreparedTurn(
            admission=admission,
            ma_session_id="sess_watch",
            mapping_id=mapping.id,
            watermark=None,
            reused=True,
            session_account_id=principal.account_id,
            _record=record,
        )
        bound.append(prepared)
        return prepared

    with watch_admission_patches(bind):
        for _ in range(2):
            await poll_wakes_once(
                db_session_factory,
                platform="discord",
                open_thread=bot._open_wake_thread,
                now=datetime.now(UTC),
            )
            for task in list(bot._bg_tasks):
                await task
    async with db_session_factory() as db:
        wake = await get_continuation(db, idempotency_key=key)
        assert len(await watches.list_evidence(db, watch.id)) == 1
        current_watch = await watches.get_watch(db, watch.id)
        if mode != "revoked_during_turn":
            assert current_watch.status == "paused"
            assert current_watch.source_state["reason"].startswith("Watch paused:")
    assert wake is not None
    outgoing = str(thread.send.await_args_list) + str(thread.send.return_value.mock_calls)
    assert "The upload is ready." not in outgoing, outgoing
