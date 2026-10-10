"""Exact Agents browser-origin shape; authentication remains a native action."""

from __future__ import annotations

import json

import httpx
import pytest
from mux.contracts.actions import UserToolConfirmation
from mux.contracts.events import RequiresActionPayload
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.normalize import EventNormalizer
from mux.drivers.openai.transport import Object, SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, UnsupportedCapability
from openai import AsyncOpenAI

from .conftest import REF, SCOPE, native_session, page, turn


def action() -> Object:
    return {
        "type": "computer_use_approval_request",
        "request_id": "approval",
        "turn_id": "root",
        "request": {
            "type": "browser_origin_access",
            "origin": "https://example.com",
            "reason": "Read the requested page.",
        },
    }


def test_origin_is_typed_confirmation_but_authentication_remains_native() -> None:
    session = native_session("requires_action")
    origin = action()
    authentication = {
        **action(),
        "request_id": "authentication",
        "request": {"type": "browser_authentication"},
    }
    session["required_actions"] = [origin, authentication]
    event = EventNormalizer("s").normalize(
        {"type": "agent.session.requires_action", "event_id": "pending", "session": session}
    )
    assert event is not None and event.turn_id == "root"
    payload = event.typed_payload()
    assert isinstance(payload, RequiresActionPayload)
    assert [a.kind for a in payload.actions] == ["tool_confirmation", "native"]
    assert payload.actions[0].payload == origin
    assert payload.actions[1].payload == authentication


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_sdk_posts_documented_origin_response(decision: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/turns"):
            return httpx.Response(200, json=page(turn("waiting")))
        if request.method == "GET":
            raw = native_session("requires_action")
            raw["required_actions"] = [action()]
            return httpx.Response(200, json=raw)
        return httpx.Response(202)

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
        receipt = await driver.events.send(
            SCOPE,
            REF,
            (UserToolConfirmation.model_validate({"action_id": "approval", "decision": decision}),),
            key="origin-answer",
        )
    assert receipt.status == "queued" and receipt.turn_id is None
    assert requests[-1].url.path == "/v1/agents/sessions/s/events"
    assert requests[-1].headers["Idempotency-Key"] == "origin-answer"
    assert json.loads(requests[-1].content) == {
        "events": [
            {
                "type": "agent.session.input.computer_use_approval_request_result",
                "request_id": "approval",
                "response": {
                    "type": "browser_origin_access",
                    "decision": "approve" if decision == "allow" else "deny",
                },
            }
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["authentication", "absent", "duplicate", "foreign_root", "deny_message", "no_turn"]
)
async def test_ambiguous_secret_or_stale_actions_never_post(fault: str) -> None:
    requests: list[httpx.Request] = []
    pending = action()
    if fault == "authentication":
        pending["request"] = {"type": "browser_authentication"}
    elif fault == "foreign_root":
        pending["turn_id"] = "child"
    elif fault == "no_turn":
        pending.pop("turn_id")

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/turns"):
            return httpx.Response(200, json=page(turn("waiting")))
        assert request.method == "GET"
        raw = native_session("requires_action")
        raw["required_actions"] = [pending, pending] if fault == "duplicate" else [pending]
        return httpx.Response(200, json=raw)

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
        with pytest.raises(UnsupportedCapability if fault != "no_turn" else ProviderError):
            await driver.events.send(
                SCOPE,
                REF,
                (
                    UserToolConfirmation(
                        action_id="absent" if fault == "absent" else "approval",
                        decision="deny",
                        deny_message="unsupported by native response"
                        if fault == "deny_message"
                        else None,
                    ),
                ),
                key="refuse",
            )
    assert requests and all(r.method == "GET" for r in requests)
