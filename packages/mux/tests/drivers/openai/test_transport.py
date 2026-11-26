from __future__ import annotations

import json

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError
from mux.testing.isolated_sdk import build_async_openai

from .conftest import REF, SCOPE, native_session, page, turn


@pytest.mark.asyncio
async def test_actual_pinned_sdk_paths_headers_query_body_and_202() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return (
            httpx.Response(202)
            if request.method == "POST"
            else httpx.Response(200, json={"data": [], "has_more": False})
        )

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        transport = SDKTransport(sdk)
        assert (
            await transport.request(
                "POST",
                "/agents/sessions/s/events",
                body={"events": [{"type": "agent.session.input.cancel"}]},
                key="key",
            )
            == {}
        )
        await transport.request(
            "GET", "/agents/sessions/s/items", query={"after": "item", "order": "asc", "limit": 2}
        )
    assert seen[0].url.path == "/v1/agents/sessions/s/events"
    assert seen[0].headers["OpenAI-Beta"] == "agents=v1"
    assert seen[0].headers["Idempotency-Key"] == "key"
    assert "idempotency_key" not in json.loads(seen[0].content)
    assert dict(seen[1].url.params) == {"after": "item", "order": "asc", "limit": "2"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, "invalid_request"),
        (401, "auth"),
        (403, "permission"),
        (404, "not_found"),
        (408, "transient_network"),
        (409, "conflict"),
        (422, "invalid_request"),
        (429, "rate_limited"),
        (500, "upstream"),
        (503, "overloaded"),
    ],
)
async def test_errors_are_owned_redacted_and_mutations_never_retry(
    status: int, category: str
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, json={"error": {"message": "sensitive-upstream-body", "code": "sensitive-code"}}
        )

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ).with_options(max_retries=5) as sdk:
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(sdk).request("POST", "/agents/sessions/s/events", body={})
    assert exc.value.category == category and calls == 1
    assert "sensitive" not in str(exc.value) and exc.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "code", "wait"),
    [
        ({"retry-after-ms": "1500", "retry-after": "9"}, "rate_limit_exceeded", 1.5),
        ({"retry-after": "12"}, "rate_limit_exceeded", 12.0),
        ({"retry-after": "100000"}, "rate_limit_exceeded", 600.0),
        ({}, "rate_limit_exceeded", 30.0),
        ({"retry-after": "5"}, "insufficient_quota", None),
    ],
)
async def test_a_429_carries_its_bounded_wait_unless_the_quota_never_refills(
    headers: dict[str, str], code: str, wait: float | None
) -> None:
    """`retry-after-ms`, then `retry-after`, else 30 s, at most 10 minutes; billing quota none."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers, json={"error": {"code": code}})

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(sdk).request("POST", "/agents/sessions/s/events", body={})
    assert exc.value.retry_after_s == wait, "wait from the 429's headers"


@pytest.mark.asyncio
async def test_real_sdk_sse_and_close_before_first_event() -> None:
    wire = {"type": "agent.session.idle", "event_id": "idle", "session": {"id": "s"}}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["stream"] == "true"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(wire) + "\n\n",
        )

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        source = await SDKTransport(sdk).open_stream("/agents/sessions/s/events")
        assert [item async for item in source] == [wire]
        unopened = await SDKTransport(sdk).open_stream("/agents/sessions/s/events")
        await unopened.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_malformed_reply_and_network_error() -> None:
    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async with build_async_openai(
        transport=httpx.MockTransport(malformed),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(sdk).request("GET", "/agents")
        assert exc.value.native_code == "malformed_response"

    def disconnect(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("sensitive", request=request)

    async with build_async_openai(
        transport=httpx.MockTransport(disconnect),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(sdk).request("POST", "/agents/sessions", body={})
        assert exc.value.category == "transient_network"


@pytest.mark.asyncio
async def test_sse_error_is_owned_and_redacted() -> None:
    wire = {
        "type": "error",
        "event_id": "error",
        "session_id": "s",
        "error": {"type": "server_error", "code": "rate_limit_exceeded", "message": "sensitive"},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["stream"] == "true"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(wire) + "\n\n",
        )

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        source = await SDKTransport(sdk).open_stream("/agents/sessions/s/events")
        with pytest.raises(ProviderError) as exc:
            [item async for item in source]
        assert exc.value.category == "rate_limited"
        assert "sensitive" not in str(exc.value) and exc.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["send", "steer", "cancel"])
async def test_public_submission_ports_preserve_key_in_real_http_header(mode: str) -> None:
    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path.endswith("/turns"):
                return httpx.Response(200, json=page(turn("in_progress")))
            return httpx.Response(
                200, json=native_session("idle" if mode == "send" else "in_progress")
            )
        posts.append(request)
        return httpx.Response(202)

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ).with_options(max_retries=5) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda s, k, i: s == SCOPE,
        )
        key = "Stable.Logical-Key:1"
        message = UserMessage(content=(TextPart(text="hello"),))
        for _ in range(2):
            if mode == "cancel":
                await driver.events.cancel(SCOPE, REF, turn_id="root", key=key)
            elif mode == "steer":
                await driver.events.steer(SCOPE, REF, message, active_turn="root", key=key)
            else:
                await driver.events.send(SCOPE, REF, (message,), key=key)
    assert len(posts) == 2 and posts[0].content == posts[1].content
    assert all(request.headers["Idempotency-Key"] == key for request in posts)
    assert all("idempotency_key" not in json.loads(request.content) for request in posts)


async def test_native_validation_details_keep_the_cause_but_redact_wire_credentials() -> None:
    secret = "fictional-callers-private-token"

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "agents must not include MCP authorization; credential="
                    + secret
                    + "; key=offline-placeholder",
                    "code": "invalid_request_error",
                    "param": "agent.tools[0].transport.authorization",
                }
            },
        )

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-placeholder",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        with pytest.raises(ProviderError) as caught:
            await SDKTransport(sdk).request(
                "POST",
                "/agents/sessions",
                body={"agent": {"tools": [{"transport": {"authorization": secret}}]}},
            )
    error = caught.value
    assert error.native_code == "400" and error.native_error_code == "invalid_request_error"
    assert error.native_param == "agent.tools[0].transport.authorization"
    assert (
        error.native_message and "agents must not include MCP authorization" in error.native_message
    )
    assert secret not in error.native_message and "offline-placeholder" not in error.native_message
    assert "[redacted]" in error.native_message and secret not in str(error)


def test_mcp_error_details_mask_known_secrets_fragments_and_bearers() -> None:
    """MCP flows keep the provider's reason but never a known credential or part of one."""
    from mux.drivers.openai.transport import suppress_mcp_error_details

    secret = "opaque-user-token-0123456789"
    leaks = (secret, secret[5:13], "Bearer unrelated-bearer-value")
    for leak in leaks:
        error = ProviderError(
            "invalid_request",
            retryable=False,
            native_code="400",
            native_message=f"skill archive mismatch near {leak}",
            native_error_code=leak,
            native_param=f"tools[0].{leak}",
        )
        with pytest.raises(ProviderError), suppress_mcp_error_details([secret]):
            raise error
        fields = (error.native_message, error.native_error_code, error.native_param)
        assert error.native_message and "skill archive mismatch" in error.native_message, (
            "the provider's reason must survive redaction"
        )
        assert not any(part in str(fields) for part in (secret[5:13], "unrelated-bearer")), (
            f"{leak!r} reached a native field: {fields}"
        )
