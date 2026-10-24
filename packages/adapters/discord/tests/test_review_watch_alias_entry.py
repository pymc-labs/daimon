"""Callback ingress through the production Discord entry and mux driver, offline."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

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
        "https://daimon.invalid/mcp?extra=1",
        "https://daimon.invalid/mcp?daimon_session=invalid",
        "https://daimon.invalid/mcp?daimon_session=one&daimon_session=two",
        "https://daimon.invalid/mcp?daimon_session=00000000-0000-0000-0000-000000000001&extra=1",
        "https://DAIMON.invalid:443/mcp/?extra=1",
        "https://daimon.invalid/old/../mcp?extra=1",
        "https://daimon.invalid/m%63p?extra=1",
    ],
)
async def test_review_actual_watch_removes_management_alias(db_session_factory, mode, monkeypatch):
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
                "configs": [
                    {
                        "name": "get_issue",
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                ],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            },
        ],
        mcp_servers=[{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}],
    )
    target.tools.append(
        target.tools[-1].model_copy(update={"mcp_server_name": "renamed-management"})
    )
    target.mcp_servers.append(
        target.mcp_servers[0].model_copy(update={"name": "renamed-management", "url": mode})
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
            yield sse_response(
                [
                    {
                        "id": f"evt_watch_report_{len(sent_tools)}",
                        "type": "agent.message",
                        "content": [{"type": "text", "text": "The upload is ready."}],
                        "processed_at": now.isoformat(),
                    },
                    {
                        "id": f"evt_watch_idle_{len(sent_tools)}",
                        "type": "session.status_idle",
                        "stop_reason": {"type": "end_turn"},
                        "processed_at": now.isoformat(),
                    },
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
        public_url="https://daimon.invalid/mcp",
        tool_safety=ToolSafetyPolicy(enabled=True),
    )
    runtime = replace(runtime, anthropic=fake_client, turn_deps=deps)
    bot = make_bot(runtime)
    WatchDiscordRest(bot)
    guild = _make_guild(member=_make_member(admin=False))
    guild.id = 710000099
    thread = _make_thread(guild=guild)
    thread.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages_in_threads=True
    )
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
    assert wake is not None
    assert sent_tools
    assert "renamed-management" not in str(sent_tools[0]), sent_tools[0]
