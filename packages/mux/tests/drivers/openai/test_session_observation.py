"""Accepted session work can precede a visible root; never manufacture idle."""

from __future__ import annotations

import json

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.ids import PageRequest, Revision
from mux.contracts.resources import SessionSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, UnsupportedCapability
from mux.testing.isolated_sdk import build_async_openai

from .conftest import REF, SCOPE, native_session, page, turn


def snapshot(status: str):
    raw = native_session(status)
    raw["environment"] = {"type": "none"}
    raw["agent"] = {"id": "a", "model": "gpt-6-luna", "multi_agent": {"enabled": False}}
    raw["usage"] = None
    return raw


def driver(sdk):
    return OpenAIDriver(
        SDKTransport(sdk),
        account_scope_id="project",
        profile_id="openai.conversation_only",
        authorization=lambda scope, kind, identity: scope == SCOPE,
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        session_controls=SessionControls(model="gpt-6-luna", multi_agent_enabled=False),
    )


@pytest.mark.asyncio
async def test_accepted_create_without_visible_root_remains_busy_until_native_idle():
    # C03 run 0c12e583: create -> turns GET -> cleanup, zero canonical POSTs;
    # final pre-delete inventory had no turns and native session became idle.
    # Private status=null is lossy: its recorder omits native in_progress.
    # Replay the decoder edge, then expose delayed root and terminal snapshots.
    requests = []
    phase = "pending"

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path == "/v1/agents/a":
            return httpx.Response(200, json=snapshot("idle")["agent"])
        if request.method == "POST":
            assert path == "/v1/agents/sessions"
            body = json.loads(request.content)
            assert body["environment"] == {"type": "none"} and body["input"]
            assert request.headers["Idempotency-Key"] == "bootstrap-create"
            return httpx.Response(200, json=snapshot("in_progress"))
        if path == "/v1/agents/sessions/s":
            return httpx.Response(200, json=snapshot("idle" if phase == "done" else "in_progress"))
        if path.endswith("/turns"):
            status = "completed" if phase == "done" else "queued"
            return httpx.Response(200, json=page() if phase == "pending" else page(turn(status)))
        assert path.endswith("/items")
        return httpx.Response(200, json=page())

    async with build_async_openai(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(handle),
    ) as sdk:
        d = driver(sdk)
        created = await d.sessions.create(
            SCOPE,
            SessionSpec(
                agent=REF.model_copy(update={"kind": "agent", "id": "a"}),
                agent_revision=Revision(local=0),
                config_revision=1,
            ),
            key="bootstrap-create",
            initial=UserMessage(content=(TextPart(text="bootstrap"),)),
        )
        assert created.state == "running" and created.active_root_turn is None
        current = await d.sessions.retrieve(SCOPE, REF)
        assert current.state == "running" and current.active_root_turn is None
        history = await d.events.list(SCOPE, REF, page=PageRequest())
        assert history.data == ()
        with pytest.raises(UnsupportedCapability, match="input_mode_precondition"):
            await d.events.send(
                SCOPE, REF, [UserMessage(content=(TextPart(text="next"),))], key="next"
            )
        phase = "visible"
        current = await d.sessions.retrieve(SCOPE, REF)
        assert current.state == "running" and current.active_root_turn == "root"
        phase = "done"
        history = await d.events.list(SCOPE, REF, page=PageRequest())
        terminal = [e for e in history.data if e.type == "session.turn_ended"]
        assert len(terminal) == 1 and terminal[0].payload["outcome"] == "completed"
        current = await d.sessions.retrieve(SCOPE, REF)
        assert current.state == "idle" and current.active_root_turn is None
    assert [r.url.path for r in requests if r.method == "POST"] == ["/v1/agents/sessions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inventory", ["empty", "completed_root", "active_child"])
async def test_running_without_visible_active_root_cannot_accept_new_turn(inventory):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/turns"):
            value = {
                "empty": page(),
                "completed_root": page(turn()),
                "active_child": page(turn("in_progress", child="child")),
            }[inventory]
        else:
            value = snapshot("in_progress")
        return httpx.Response(200, json=value)

    async with build_async_openai(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(handle),
    ) as sdk:
        d = driver(sdk)
        current = await d.sessions.retrieve(SCOPE, REF)
        assert current.state == "running" and current.active_root_turn is None
        with pytest.raises(UnsupportedCapability, match="input_mode_precondition"):
            await d.events.send(
                SCOPE, REF, [UserMessage(content=(TextPart(text="next"),))], key="next"
            )
    assert requests and all(r.method == "GET" for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["requires_action", "foreign_root", "null_status", "missing_status"]
)
async def test_unverified_action_foreign_root_and_malformed_status_still_refuse(kind):
    def handle(request):
        if request.url.path.endswith("/turns"):
            root = turn("in_progress")
            root["session_id"] = "foreign"
            return httpx.Response(200, json=page(root) if kind == "foreign_root" else page())
        value = snapshot("requires_action" if kind == "requires_action" else "in_progress")
        if kind == "null_status":
            value["status"] = None
        if kind == "missing_status":
            del value["status"]
        return httpx.Response(200, json=value)

    async with build_async_openai(
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(handle),
    ) as sdk:
        with pytest.raises(ProviderError) as error:
            await driver(sdk).sessions.retrieve(SCOPE, REF)
    assert error.value.native_code == (
        "active_root_unavailable" if kind == "requires_action" else "malformed_response"
    )
