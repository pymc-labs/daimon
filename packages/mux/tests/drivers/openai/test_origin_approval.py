"""Exact Agents browser-origin shape; authentication remains a native action."""

from __future__ import annotations

import httpx
import pytest
from mux.contracts.actions import UserToolConfirmation
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import Object, SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, UnsupportedCapability
from mux.testing.isolated_sdk import build_async_openai

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

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
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
